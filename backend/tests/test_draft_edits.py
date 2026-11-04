"""草案上的「具体编辑」：换食材、加动作、删动作、换动作。

用户的原话往往很具体（"把红薯换成土豆，再新增一个练背动作"），
如果只处理模糊反馈，就会变成"摘要说改了、计划没动"——这是最坏的情况。
"""

from __future__ import annotations

from app.domain.draft_edits import apply_explicit_edits


def _payload() -> dict:
    return {
        "days": [
            {"day_index": 1, "actions": [
                {"slot": "strength", "seq": 1, "action_ref": "goblet-squat", "action_type": "exercise",
                 "sets": 3, "reps": 12, "meta": {"name_zh": "高脚杯深蹲", "movement_mode": "knee_dominant"}},
            ]},
            {"day_index": 2, "actions": [
                {"slot": "strength", "seq": 1, "action_ref": "goblet-squat", "action_type": "exercise",
                 "sets": 3, "reps": 12, "meta": {"name_zh": "高脚杯深蹲", "movement_mode": "knee_dominant"}},
            ]},
        ],
        "meals": [
            {"day_index": 1, "slot": "dinner", "title": "晚餐",
             "items": [{"name": "红薯", "grams": 300, "kcal": 258, "protein": 4.8, "fat": 0.3, "carb": 60}],
             "kcal": 258, "protein_g": 4.8, "fat_g": 0.3, "carb_g": 60},
        ],
        "summary": "原计划",
    }


def test_food_swap_keeps_calories_close() -> None:
    out, changes = apply_explicit_edits(_payload(), "把红薯换成土豆")
    assert changes
    item = out["meals"][0]["items"][0]
    assert item["name"] == "土豆"
    # 热量基本对齐（±30 kcal 内），否则等于偷偷改了配比
    assert abs(item["kcal"] - 258) <= 30
    assert out["meals"][0]["kcal"] == item["kcal"]


def test_add_back_exercise() -> None:
    out, changes = apply_explicit_edits(_payload(), "新增一个练背动作")
    assert changes
    for day in out["days"]:
        assert any("row" in action["action_ref"] for action in day["actions"]), day


def test_add_exercise_is_idempotent() -> None:
    """同一句说两遍不应该加两个一样的动作。"""
    once, _ = apply_explicit_edits(_payload(), "新增一个练背动作")
    twice, changes = apply_explicit_edits(once, "再加一个练背动作")
    assert changes == []
    assert len(twice["days"][0]["actions"]) == 2


def test_remove_exercise_by_name() -> None:
    out, changes = apply_explicit_edits(_payload(), "去掉高脚杯深蹲")
    assert changes
    assert out["days"][0]["actions"] == []


def test_swap_exercise_by_name() -> None:
    out, changes = apply_explicit_edits(_payload(), "把高脚杯深蹲换成臀桥")
    assert changes
    assert out["days"][0]["actions"][0]["action_ref"] == "glute-bridge"


def test_combined_request_applies_both_parts() -> None:
    """用户的真实句子是复合的：换食材 + 加动作。"""
    out, changes = apply_explicit_edits(_payload(), "把红薯换成土豆，并新增一个练背动作")
    assert out["meals"][0]["items"][0]["name"] == "土豆"
    assert any("row" in action["action_ref"] for action in out["days"][0]["actions"])
    assert len(changes) >= 2


def test_unrecognized_request_changes_nothing() -> None:
    out, changes = apply_explicit_edits(_payload(), "随便改改")
    assert changes == []
    assert out["days"] == _payload()["days"]
    assert out["meals"] == _payload()["meals"]


def test_natural_sentence_with_padding_words() -> None:
    """真实句子会有多余的字：「主食把红薯换成土豆即可，其余结构不变」。

    第一版正则把目标抓成「土豆即可」，查不到食材就判定"没有可执行调整"，
    于是用户点了再改改、红薯没换。
    """
    out, changes = apply_explicit_edits(_payload(), "主食把红薯换成土豆即可，其余结构不变")
    assert changes
    assert out["meals"][0]["items"][0]["name"] == "土豆"


def test_colloquial_remove_and_replace() -> None:
    """「不要红薯，换成土豆」这种口语也要认。"""
    out, changes = apply_explicit_edits(_payload(), "不要红薯，换成土豆")
    assert changes
    assert out["meals"][0]["items"][0]["name"] == "土豆"
