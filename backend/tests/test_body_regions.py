"""通用部位安全：膝 / 踝 / 腿 / 髋 / 腰 / 背 / 胸 / 肩 / 肘 / 腕 / 手臂。

以前只有膝盖是硬编码特例，其余部位只能靠十几条老动作映射猜。
这里验证「新增一个部位只要往规则表加一行」这条路是真的走得通。
"""

from __future__ import annotations

import pytest

from app.domain import safety_rules as rules
from app.domain.planner import parse_intent, trim_unrelated_ops, violates_constraints
from app.domain.patches import _safety_check


def action(ref: str, *, day: int = 1, meta: dict | None = None) -> dict:
    return {
        "slot_key": f"{day}:strength:{ref}",
        "day_index": day,
        "slot": "strength",
        "seq": 1,
        "action_type": "exercise",
        "action_ref": ref,
        "sets": 3, "reps": 12, "duration_min": 12,
        "intensity": "moderate", "portion_g": None,
        "meta": meta or {}, "evidence_ids": [],
    }


# ---------------------------------------------------------------- 识别

@pytest.mark.parametrize("text,expect", [
    ("膝盖疼", "knee"),
    ("脚踝扭到了", "ankle"),
    ("腿有点疼", "legs"),
    ("髋部不舒服", "hip"),
    ("腰疼", "lower_back"),
    ("后背拉伤", "back"),
    ("胸肌拉伤", "chest"),
    ("肩膀不舒服", "shoulder"),
    ("手肘疼", "elbow"),
    ("手腕疼", "wrist"),
    ("手臂酸", "arm"),
])
def test_detect_region(text: str, expect: str) -> None:
    assert expect in rules.detect_regions(text)


def test_lower_back_and_back_are_distinguished() -> None:
    assert rules.detect_regions("腰疼") == ["lower_back"]
    assert rules.detect_regions("背部拉伤") == ["back"]


# ---------------------------------------------------------------- 风险判定

@pytest.mark.parametrize("ref,region", [
    ("goblet-squat", "knee"),
    ("walking-lunge", "ankle"),
    ("leg-press", "legs"),
    ("hip-thrust", "hip"),
    ("romanian-deadlift", "lower_back"),
    ("seated-row", "back"),
    ("incline-bench-press", "chest"),
    ("dumbbell-bench-press", "shoulder"),
    ("barbell-curl", "elbow"),
    ("push-up", "wrist"),
    ("dumbbell-bench-press", "arm"),
])
def test_risky_moves_per_region(ref: str, region: str) -> None:
    assert rules.is_risky_for(ref, region) is True


def test_cross_limb_is_not_flagged() -> None:
    """肩痛不该因为 leg-press 里有 press 就误伤下肢动作。"""
    assert rules.is_risky_for("leg-press", "shoulder") is False
    assert rules.is_risky_for("goblet-squat", "chest") is False


def test_safe_move_passes_every_region() -> None:
    for region in rules.REGIONS:
        assert rules.is_risky_for("stretch", region) is False
        assert rules.is_risky_for("dead-bug", region) is False


# ---------------------------------------------------------------- 意图

@pytest.mark.parametrize("text,kind,part", [
    ("脚踝扭到了，别做跳跃", "injury_swap", "ankle"),
    ("腿有点疼", "injury_swap", "legs"),
    ("腰疼", "injury_swap", "lower_back"),
    ("胸肌拉伤", "injury_swap", "chest"),
    ("手臂酸", "injury_swap", "arm"),
])
def test_pain_defaults_to_avoid_not_stop(text: str, kind: str, part: str) -> None:
    """说了部位但没说"停止"，语义应该是"避开它"，而不是把整份计划停掉。"""
    intent = parse_intent(text)
    assert intent.kind == kind
    assert part in intent.body_parts


def test_explicit_stop_still_means_stop() -> None:
    assert parse_intent("膝盖疼，先别练了").kind == "injury_stop"
    assert parse_intent("受伤了", "injury_stop").kind == "injury_stop"


# ---------------------------------------------------------------- 最小化

def test_trim_drops_ops_unrelated_to_injured_part() -> None:
    """模型容易"受伤=全删"：把跟受伤部位无关的改动剪掉。"""
    intent = parse_intent("脚踝扭到了")
    # walking-lunge 有 lunge（脚踝有负荷），bench-press 跟脚踝无关
    base = [action("walking-lunge"), action("dumbbell-bench-press")]
    operations = [
        {"op": "remove_action", "slot_key": "1:strength:walking-lunge",
         "before_action_ref": "walking-lunge", "reason": "x"},
        {"op": "remove_action", "slot_key": "1:strength:dumbbell-bench-press",
         "before_action_ref": "dumbbell-bench-press", "reason": "x"},
    ]
    kept, dropped = trim_unrelated_ops(intent, base, operations)
    assert dropped == 1
    assert [op["before_action_ref"] for op in kept] == ["walking-lunge"]


def test_trim_keeps_everything_when_no_injury() -> None:
    intent = parse_intent("今天想休息", "rest")
    base = [action("goblet-squat")]
    operations = [{"op": "remove_action", "slot_key": "1:strength:goblet-squat",
                   "before_action_ref": "goblet-squat", "reason": "x"}]
    assert trim_unrelated_ops(intent, base, operations) == (operations, 0)


# ---------------------------------------------------------------- 安全门

@pytest.mark.parametrize("region,ref,label", [
    ("ankle", "walking-lunge", "脚踝"),
    ("shoulder", "dumbbell-bench-press", "肩"),
    ("chest", "push-up", "胸"),
    ("lower_back", "romanian-deadlift", "腰"),
    ("arm", "barbell-curl", "手臂"),
])
def test_gate_blocks_by_region(region: str, ref: str, label: str) -> None:
    errors = _safety_check([action(ref)], available_days=3, regions=[region])
    assert errors and errors[0].startswith(f"{label}禁忌动作")


def test_gate_without_regions_checks_nothing() -> None:
    assert _safety_check([action("goblet-squat")], available_days=3,
                         regions=[]) == []


def test_violates_constraints_reports_chinese_name() -> None:
    intent = parse_intent("后背拉伤")
    problems = violates_constraints([action("romanian-deadlift")], intent)
    assert problems and "背" in problems[0]

@pytest.mark.parametrize("ref,region", [
    ("push-up", "shoulder"),
    ("pushup", "shoulder"),
    ("plank", "shoulder"),
    ("push-up", "elbow"),
    ("push-up", "arm"),
    ("mountain-climber", "shoulder"),
])
def test_pushing_moves_load_shoulder_too(ref: str, region: str) -> None:
    """俯卧撑/平板主练肌肉写的是 Chest，但肩前束和肩袖一样在发力。

    只按 primary_muscle 判断会把"肩痛还留着俯卧撑"这种事漏掉——这是实际踩到的 bug。
    """
    assert rules.is_risky_for(ref, region) is True


def test_shoulder_patch_replaces_pushup() -> None:
    """肩痛补丁必须把俯卧撑也换掉，而不是只换卧推。"""
    from app.domain.planner import cover_injured_slots, parse_intent

    intent = parse_intent("肩膀不舒服，先避开它")
    base = [
        {**action("push-up"), "slot_key": "1:strength:push-up"},
        {**action("dumbbell-bench-press"), "slot_key": "1:strength:dumbbell-bench-press"},
    ]
    extra = cover_injured_slots(intent, base, [])
    touched = {op["slot_key"] for op in extra}
    assert "1:strength:push-up" in touched
    for op in extra:
        assert op["op"] == "replace_action"
        assert rules.is_risky_for(op["after_action"]["action_ref"], "shoulder") is False
