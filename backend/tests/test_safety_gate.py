"""安全门：确定性规则到底拦什么、放什么。

这里不测模型：安全门的全部判定都由代码完成，
所以每条规则都能写成"输入 → 期望的拒绝理由"。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent.today_agent import validate as validate_today
from app.agent.today_tools import safety_screen
from app.domain.draft_edits import extract_allergy_terms  # noqa: F401  (同一套忌口口径)
from app.domain.patches import _safety_check, is_knee_risky


def action(ref: str, *, slot: str = "strength", day: int = 1, meta: dict | None = None) -> dict:
    return {
        "slot_key": f"{day}:{slot}:{ref}",
        "day_index": day,
        "slot": slot,
        "seq": 1,
        "action_type": "exercise",
        "action_ref": ref,
        "sets": 3,
        "reps": 12,
        "duration_min": 12,
        "intensity": "moderate",
        "portion_g": None,
        "meta": meta or {},
        "evidence_ids": [],
    }


# ---------------------------------------------------------------- 膝痛禁忌

@pytest.mark.parametrize("ref", [
    "squat",            # 显式禁忌集
    "goblet_squat",     # 下划线写法
    "goblet-squat",     # 连字符写法（skill 用 slug）
    "jump-squat",
    "walking-lunge",
    "run",
])
def test_knee_risky_by_ref_variants(ref: str) -> None:
    """同名动作的两种写法必须都认：否则换一个计划来源就静默漏检。"""
    assert is_knee_risky(action(ref)) is True


def test_knee_risky_by_catalog_metadata() -> None:
    """目录里标注 knee_risky 的动作，即使名字里没有 squat/lunge 也要拦。"""
    from app.domain.exercise_catalog import KNEE_RISKY_SLUGS

    assert KNEE_RISKY_SLUGS, "目录里必须有膝痛风险标注，否则这道门只剩关键词"
    sample = sorted(KNEE_RISKY_SLUGS)[0]
    assert is_knee_risky(action(sample)) is True


def test_knee_risky_by_movement_mode() -> None:
    assert is_knee_risky(action("custom-move", meta={"movement_mode": "knee_dominant"})) is True


def test_safe_movements_pass() -> None:
    for ref in ("glute_bridge", "stretch", "banded-face-pull", "hip-thrust"):
        assert is_knee_risky(action(ref)) is False


def test_unknown_ref_is_not_blocked_by_name_rule() -> None:
    """已知缺口（写进文档）：完全没登记过的动作名不会因为"名字不像"被拦，
    这类要靠目录元数据 + 人工审批兜底，而不是指望关键词表穷举。
    """
    assert is_knee_risky(action("brand-new-move")) is False


# ---------------------------------------------------------------- 结构性规则

def test_safety_check_blocks_knee_risk_with_name() -> None:
    errors = _safety_check([action("goblet-squat", meta={"name_zh": "高脚杯深蹲"})],
                           available_days=3)
    assert errors == ["膝痛禁忌动作: 高脚杯深蹲"]


def test_safety_check_can_skip_knee_for_rest_patch() -> None:
    """「今天想休息」这类补丁保留原动作是合理的，不能拿膝痛禁忌去卡它。"""
    actions = [action("goblet-squat"), action("glute-bridge", day=2)]
    assert _safety_check(actions, available_days=3, check_knee=False) == []


def test_safety_check_blocks_too_many_strength_days() -> None:
    actions = [action("glute-bridge", day=1), action("hip-thrust", day=2),
               action("banded-row", day=3)]
    errors = _safety_check(actions, available_days=2, check_knee=False)
    assert errors == ["力量训练日 3 超过可用 2 天"]


def test_safety_check_ignores_non_exercise_slots() -> None:
    meal = {**action("rice", slot="meal"), "action_type": "meal"}
    assert _safety_check([meal], available_days=0, check_knee=False) == []


# ---------------------------------------------------------------- 红旗症状（输入侧）

def test_red_flag_blocks_planning(client: TestClient) -> None:
    with client.app.state.session_factory() as db:
        result = safety_screen({"text": "今天胸痛，还能跑步吗"}, {}, db)
    assert result["data"]["blocked"] is True
    assert "胸痛" in result["data"]["reason"]


def test_normal_text_passes_screening(client: TestClient) -> None:
    with client.app.state.session_factory() as db:
        result = safety_screen({"text": "今天想练腿"}, {}, db)
    assert result["data"]["blocked"] is False


# ---------------------------------------------------------------- 输出侧校验

def _ctx(allergies=()) -> dict:
    return {"allergies": list(allergies), "meals": {}}


def _final(**overrides) -> dict:
    payload = {
        "summary": "今天按计划训练",
        "meals": [{
            "slot": "lunch",
            "target_kcal": 600,
            "options": [
                {"title": f"选项{i}", "kcal": 600, "protein_g": 40,
                 "links": {"jd": "https://search.jd.com/x",
                           "taobao": "https://s.taobao.com/x",
                           "meituan": "https://www.meituan.com/x"}}
                for i in range(3)
            ],
        }],
        "tips": [],
        "nutrition_notes": [],
        "citations": [],
    }
    payload.update(overrides)
    return payload


def test_validate_passes_clean_output(client: TestClient) -> None:
    with client.app.state.session_factory() as db:
        assert validate_today(_final(), _ctx(), db) == []


def test_validate_flags_allergen_in_title(client: TestClient) -> None:
    final = _final()
    final["meals"][0]["options"][0]["title"] = "虾仁沙拉"
    with client.app.state.session_factory() as db:
        problems = validate_today(final, _ctx(["虾"]), db)
    assert any("虾" in item for item in problems)


def test_validate_flags_unlisted_link_host(client: TestClient) -> None:
    final = _final()
    final["meals"][0]["options"][1]["links"]["jd"] = "https://evil.example.com/x"
    with client.app.state.session_factory() as db:
        problems = validate_today(final, _ctx(), db)
    assert any("链接不在白名单" in item for item in problems)


def test_validate_flags_missing_citation(client: TestClient) -> None:
    final = _final(citations=[{"id": "no-such-chunk", "title": "", "source_url": ""}])
    with client.app.state.session_factory() as db:
        problems = validate_today(final, _ctx(), db)
    assert any("引用不存在" in item for item in problems)


def test_validate_flags_medical_advice(client: TestClient) -> None:
    final = _final(tips=[{"text": "可以先吃点布洛芬", "source_ids": []}])
    with client.app.state.session_factory() as db:
        problems = validate_today(final, _ctx(), db)
    assert any("用药" in item for item in problems)


def test_validate_requires_three_options(client: TestClient) -> None:
    final = _final()
    final["meals"][0]["options"] = final["meals"][0]["options"][:2]
    with client.app.state.session_factory() as db:
        problems = validate_today(final, _ctx(), db)
    assert any("选项数不是 3" in item for item in problems)
