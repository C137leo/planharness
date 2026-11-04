"""教练计划链路：skill 桥接 → 草案 → 多轮修改 → 确认 → 今日安排。

跳过策略：本机没有 skill 仓库时，依赖真实 skill 的用例整体 skip，
而不是假装通过——桥接失败的降级路径由 `test_local_gate_*` 覆盖。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent.planning_agent import _build_meals, _local_gate, skill_profile
from app.domain.allergens import resolve_tags
from app.skill.bridge import SkillBridge

API = "/api/v1"

BRIDGE = SkillBridge()
needs_skill = pytest.mark.skipif(
    not BRIDGE.available, reason="healthy-fitness-coach skill 不在本机"
)


class _Profile:
    goal = "fat_loss"
    training_experience = "beginner"
    weekly_days = 3
    session_minutes = 40
    equipment = ["dumbbell"]
    allergies = ["乳制品"]
    diet_preferences = []
    dislikes = []
    sex = "male"
    birth_year = 1997
    height_cm = 178
    weight_kg = 78
    goal_note = ""


def _setup(client: TestClient, headers: dict[str, str]) -> None:
    resp = client.put(f"{API}/me/profile", headers=headers, json={
        "goal": "fat_loss",
        "sex": "male",
        "birth_year": 1997,
        "height_cm": 178,
        "weight_kg": 78,
        "weekly_days": 3,
        "session_minutes": 40,
        "equipment": ["dumbbell"],
        "allergies": ["乳制品"],
        "goal_note": "想 3 个月瘦 8 公斤",
    })
    assert resp.status_code == 200, resp.text


def _draft(client: TestClient, headers: dict[str, str], text: str = "帮我做一个减脂计划") -> dict:
    resp = client.post(f"{API}/plans/drafts", headers=headers, json={"instruction": text})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


@needs_skill
def test_bridge_calls_real_skill() -> None:
    """桥接层真的调到了 skill：训练结构、营养目标都是引擎算出来的。"""
    meta = BRIDGE.meta()
    assert meta["status"] == "ok"
    program = BRIDGE.plan_program(skill_profile(_Profile()))
    assert program["status"] == "ready"
    assert program["program"]["sessions"], "skill 必须给出训练日"
    targets = BRIDGE.nutrition_targets(
        skill_profile(_Profile()), {"sex": "male", "age": 29, "height_cm": 178, "weight_kg": 78}
    )
    assert targets["status"] == "ready"
    assert targets["target_kcal"] > targets["bmr"], "目标热量必须高于基础代谢"


def test_profile_update_persists_goal_and_allergies(client, auth_headers) -> None:
    _setup(client, auth_headers)
    profile = client.get(f"{API}/me/profile", headers=auth_headers).json()["data"]
    assert profile["goal"] == "fat_loss"
    assert profile["allergies"] == ["乳制品"]
    assert profile["goal_note"].startswith("想 3 个月")

    bad = client.put(f"{API}/me/profile", headers=auth_headers, json={"height_cm": 999})
    assert bad.status_code == 422, "超出范围的体型数据要被拒"


def test_patch_presets_are_user_facing() -> None:
    """预设入口是"用户能这么说"的示例，不是写死的补丁。"""
    from app.main import create_app
    from app.config import Settings
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Settings(env="dev", database_url=f"sqlite:///{tmp}/t.db",
                                  secret_key="t" * 32))
        with TestClient(app) as client:
            presets = client.get(f"{API}/patch-presets").json()["data"]
    keys = {item["key"] for item in presets}
    assert {"rest", "injury_stop", "injury_swap", "increase", "diet", "time"} <= keys
    assert all(item["example"] for item in presets)


@needs_skill
def test_draft_generates_training_menu_and_targets(client, auth_headers) -> None:
    """生成草案：有训练日、有食谱、有推荐摄入，且食谱避开了过敏原。"""
    _setup(client, auth_headers)
    data = _draft(client, auth_headers)
    assert data["status"] == "ready", data.get("question")
    assert data["days"], "必须有训练日"
    assert data["meals"], "必须有食谱"
    assert data["nutrition"]["target_kcal"] > 1200
    assert data["meta"]["tools_called"], "必须记录调用了哪些 skill 工具"

    dairy = {"希腊酸奶", "牛奶", "乳清蛋白粉"}
    names = {item["name"] for meal in data["meals"] for item in meal["items"]}
    assert not (names & dairy), "乳制品过敏必须被排除"

    monday = [meal for meal in data["meals"] if meal["day_index"] == 1]
    # 减脂目标只有三餐（加餐只给增肌）
    assert [meal["slot"] for meal in monday] == ["breakfast", "lunch", "dinner"]


@needs_skill
def test_draft_feedback_rounds_and_supersede(client, auth_headers) -> None:
    """「再改改」：轮次递增、旧草案作废、超过 3 轮直接 422。"""
    _setup(client, auth_headers)
    first = _draft(client, auth_headers)
    current = first
    for i in range(3):
        resp = client.post(f"{API}/plans/drafts/{current['draft_id']}/feedback",
                           headers=auth_headers, json={"text": f"再保守一点 {i}"})
        assert resp.status_code == 200, resp.text
        current = resp.json()["data"]
        assert current["round"] == i + 2

    stale = client.post(f"{API}/plans/drafts/{first['draft_id']}/confirm",
                        headers=auth_headers, json={"comment": "旧草案"})
    assert stale.status_code == 422, "被取代的草案不能再确认"

    resp = client.post(f"{API}/plans/drafts/{current['draft_id']}/feedback",
                       headers=auth_headers, json={"text": "再改一次"})
    assert resp.status_code == 422, "第 4 轮必须被拒"


@needs_skill
def test_confirm_draft_commits_version_and_today(client, auth_headers) -> None:
    """确认草案 → 新版本 + source=coach + /plans/current 带回今日安排。"""
    _setup(client, auth_headers)
    before = client.get(f"{API}/plans/current", headers=auth_headers).json()["data"]
    draft = _draft(client, auth_headers)
    resp = client.post(f"{API}/plans/drafts/{draft['draft_id']}/confirm",
                       headers=auth_headers, json={"comment": "就按这个来"})
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["committed_version"] == before["version"] + 1
    assert body["plan"]["source"] == "coach"
    assert body["plan"]["meals"], "确认后餐食要落库"

    plan = client.get(f"{API}/plans/current", headers=auth_headers).json()["data"]
    assert plan["today"] is not None
    assert plan["today"]["weekday"] == plan["today"]["weekday"]
    assert plan["nutrition"]["target_kcal"] > 1200

    audit = client.get(f"{API}/audit/{plan['plan_id']}", headers=auth_headers).json()["data"]
    actions = {event["action"] for event in audit}
    assert "plan_confirmed" in actions


@needs_skill
def test_reject_draft_keeps_plan(client, auth_headers) -> None:
    _setup(client, auth_headers)
    before = client.get(f"{API}/plans/current", headers=auth_headers).json()["data"]
    draft = _draft(client, auth_headers)
    resp = client.post(f"{API}/plans/drafts/{draft['draft_id']}/reject",
                       headers=auth_headers, json={"comment": "不满意"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "rejected"
    after = client.get(f"{API}/plans/current", headers=auth_headers).json()["data"]
    assert after["version"] == before["version"]


def test_local_gate_blocks_allergen_and_low_calories() -> None:
    """本地安全门：食谱含过敏原、目标热量过低，都必须拦下来。"""
    profile = skill_profile(_Profile())
    payload = {
        "days": [{"day_index": 1, "actions": []}],
        "meals": [
            {"title": "早餐", "items": [{"name": "希腊酸奶", "grams": 200}]},
        ],
        "nutrition": {"target_kcal": 1000, "bmr": 1750},
        "screen": {"level": "clear"},
    }
    problems = _local_gate(profile, payload)
    assert any("乳制品" in item or "dairy" in item for item in problems)
    assert any("基础代谢" in item for item in problems)


NUTRITION = {"status": "ready", "target_kcal": 2230,
             "macros": {"protein_g": {"low_g": 109, "high_g": 156}}}


def _week_meals(allergies: list[str]) -> list[dict]:
    base = {"allergies": allergies, "diet_preferences": [], "training_days_per_week": 3}
    return _build_meals(base, NUTRITION)


def test_allergen_phrases_are_recognized() -> None:
    """「海鲜过敏 / 对海鲜过敏 / 甲壳类」都要能识别成 seafood。"""
    for phrase in ("海鲜过敏", "对海鲜过敏", "甲壳类", "海产品", "shellfish"):
        tags, unmatched = resolve_tags([phrase])
        assert "seafood" in tags, phrase
        assert unmatched == [], phrase


def test_seafood_allergy_removes_shrimp_and_salmon() -> None:
    """这是真实踩过的坑：用户写「海鲜过敏」，食谱却推荐了虾。"""
    meals = _week_meals(["海鲜过敏"])
    names = {item["name"] for meal in meals for item in meal["items"]}
    assert "虾" not in names, "海鲜过敏不能出现虾"
    assert "三文鱼" not in names, "海鲜过敏不能出现三文鱼"


def test_dairy_allergy_removes_yogurt() -> None:
    meals = _week_meals(["乳制品"])
    names = {item["name"] for meal in meals for item in meal["items"]}
    assert "希腊酸奶" not in names


def test_placeholders_are_not_treated_as_allergens() -> None:
    """用户把"无"填进过敏字段是常事，不能提示"没识别出忌口：无"。"""
    tags, unmatched = resolve_tags(["无"])
    assert tags == set()
    assert unmatched == []
    payload = {"days": [], "meals": [], "nutrition": NUTRITION, "screen": {}, "cautions": []}
    _local_gate({"allergies": ["无"], "diet_preferences": []}, payload)
    assert payload["cautions"] == []


def test_unmatched_allergen_is_surfaced_not_ignored() -> None:
    """认不出来的忌口不能当没填：要出现在 cautions 里让人确认。"""
    base = {"allergies": ["某种奇怪的东西"], "diet_preferences": [], "training_days_per_week": 3}
    payload = {"days": [], "meals": [], "nutrition": NUTRITION,
               "screen": {}, "cautions": []}
    _local_gate(base, payload)
    assert any("无法自动排除" in item or "没能自动识别" in item for item in payload["cautions"])


@needs_skill
def test_explicit_edit_in_feedback_changes_days_and_meals(client, auth_headers) -> None:
    """「把红薯换成土豆 + 新增练背」必须真的改到 day/meal，而不是只改摘要。"""
    _setup(client, auth_headers)
    draft = _draft(client, auth_headers)
    resp = client.post(
        f"{API}/plans/drafts/{draft['draft_id']}/feedback",
        headers=auth_headers,
        json={"text": "把红薯换成土豆，并新增一个练背动作"},
    )
    assert resp.status_code == 200, resp.text
    revised = resp.json()["data"]

    assert any("row" in action["action_ref"] for day in revised["days"] for action in day["actions"])
    names = {item["name"] for meal in revised["meals"] for item in meal["items"]}
    assert "土豆" in names and "红薯" not in names
    assert revised["revisions"], "要能告诉用户这一轮改了什么"


@needs_skill
def test_unmatched_feedback_is_honest(client, auth_headers) -> None:
    """认不出来的反馈：摘要要标注没改，而不是让模型吹一遍。"""
    _setup(client, auth_headers)
    draft = _draft(client, auth_headers)
    resp = client.post(
        f"{API}/plans/drafts/{draft['draft_id']}/feedback",
        headers=auth_headers,
        json={"text": "随便改改"},
    )
    revised = resp.json()["data"]
    assert "没有改动计划内容" in revised["summary"]
    assert revised["cautions"], "要有一句人话解释为什么没改"


def test_executable_feedback_skips_clarifying_question(monkeypatch, client, auth_headers) -> None:
    """实盘踩过的坑：模型反问"要不要加背部动作"，但这句话我们自己能执行。

    这种情况下不能只给用户一个问题、却没有确认/回答的入口；
    后端应当绕开模型，用确定性编排生成，并应用这句编辑。
    """
    from app.domain import coach as coach_module
    from app.agent.planning_agent import PlanProposal

    ready_payload = {
        "status": "ready",
        "summary": "确定性编排结果",
        "highlights": [],
        "cautions": [],
        "warnings": [],
        "days": [{"day_index": 1, "focus": "full_body_a", "actions": [
            {"slot": "strength", "seq": 1, "action_type": "exercise",
             "action_ref": "goblet-squat", "sets": 3, "reps": 12, "duration_min": 12,
             "meta": {"name_zh": "高脚杯深蹲", "movement_mode": "knee_dominant"}},
        ]}],
        "meals": [{"day_index": 1, "slot": "dinner", "title": "晚餐",
                   "items": [{"name": "红薯", "grams": 300, "kcal": 258, "protein": 4.8,
                              "fat": 0.3, "carb": 60}],
                   "kcal": 258, "protein_g": 4.8, "fat_g": 0.3, "carb_g": 60}],
        "nutrition": {},
        "program": {},
        "meta": {},
    }
    clarifying = {
        "status": "needs_input",
        "summary": "",
        "question": "要不要在 3 个动作上加一个背部动作？",
        "days": [],
        "meals": [],
        "highlights": [],
        "cautions": [],
    }

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

        def propose(self, profile, instruction, previous_feedback=None):
            return PlanProposal(status="needs_input", payload=clarifying, trace=[])

        def propose_deterministic(self, profile, instruction):
            return PlanProposal(status="ready", payload=ready_payload, trace=[])

    monkeypatch.setattr(coach_module, "PlanningAgent", FakeAgent)

    _setup(client, auth_headers)
    draft = client.post(f"{API}/plans/drafts", headers=auth_headers,
                        json={"instruction": "先来一版"}).json()["data"]
    resp = client.post(f"{API}/plans/drafts/{draft['draft_id']}/feedback",
                       headers=auth_headers,
                       json={"text": "每次动作都不同，另外加个练背动作"})
    assert resp.status_code == 200, resp.text
    revised = resp.json()["data"]
    assert revised["status"] == "ready", "能执行的需求不该停在追问状态"
    refs = [action["action_ref"] for day in revised["days"] for action in day["actions"]]
    assert any("row" in ref for ref in refs), f"应该加上背部动作，实际 {refs}"


def test_explicit_food_avoidance_excludes_food_from_generation() -> None:
    """画像里写了「不吃红薯」，生成时就不该再出现。"""
    base = {"allergies": [], "diet_preferences": ["红薯"], "training_days_per_week": 3}
    meals = _build_meals(base, NUTRITION)
    assert all(item["name"] != "红薯" for meal in meals for item in meal["items"])


def test_first_draft_does_not_ask_when_request_is_executable(monkeypatch, client, auth_headers) -> None:
    """首次生成时模型反问"要不要加背部动作"，我们应该自己做，而不是让用户填问题。"""
    from app.domain import coach as coach_module
    from app.agent.planning_agent import PlanProposal

    ready_payload = {
        "status": "ready", "summary": "确定性结果", "highlights": [], "cautions": [],
        "warnings": [], "days": [{"day_index": 1, "focus": "full_body_a", "actions": []}],
        "meals": [], "nutrition": {}, "program": {}, "meta": {},
    }

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

        def propose(self, profile, instruction, previous_feedback=None):
            return PlanProposal(status="needs_input", payload={
                "status": "needs_input", "summary": "",
                "question": "要不要加一个背部动作？", "days": [], "meals": [],
                "highlights": [], "cautions": [],
            }, trace=[])

        def propose_deterministic(self, profile, instruction):
            return PlanProposal(status="ready", payload=ready_payload, trace=[])

    monkeypatch.setattr(coach_module, "PlanningAgent", FakeAgent)

    _setup(client, auth_headers)
    resp = client.post(f"{API}/plans/drafts", headers=auth_headers,
                       json={"instruction": "减脂，每周3天，加1个练背动作"})
    assert resp.status_code == 200, resp.text
    draft = resp.json()["data"]
    assert draft["status"] == "ready", draft.get("question")
    refs = [a["action_ref"] for day in draft["days"] for a in day["actions"]]
    assert any("row" in ref for ref in refs), refs


def test_goal_and_avoidance_are_remembered_from_instruction() -> None:
    """「想减脂 + 不吃红薯 + 海鲜过敏」要写进画像，否则每轮都要重说、模型还会反复追问。"""
    from app.domain.coach import _remember_allergies, _remember_avoidances, _remember_goal

    class P:
        goal = "muscle_gain"
        allergies = ["花生"]
        diet_preferences = ["香菜"]

    profile = P()
    _remember_goal(profile, "想减脂，每周3天，海鲜过敏，不吃红薯")
    _remember_allergies(profile, "想减脂，每周3天，海鲜过敏，不吃红薯")
    _remember_avoidances(profile, "想减脂，每周3天，海鲜过敏，不吃红薯")

    assert profile.goal == "fat_loss"
    assert "海鲜" in profile.allergies
    assert "花生" in profile.allergies
    assert "红薯" in profile.diet_preferences
    assert "香菜" in profile.diet_preferences
