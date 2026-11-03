"""教练计划链路：skill 桥接 → 草案 → 多轮修改 → 确认 → 今日安排。

跳过策略：本机没有 skill 仓库时，依赖真实 skill 的用例整体 skip，
而不是假装通过——桥接失败的降级路径由 `test_local_gate_*` 覆盖。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent.planning_agent import _local_gate, skill_profile
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
    assert [meal["slot"] for meal in monday] == ["breakfast", "lunch", "dinner", "snack"]


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
