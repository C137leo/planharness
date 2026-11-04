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


def test_avoid_food_phrasing_replaces_with_same_group() -> None:
    """「不吃红薯」不是"换个说法"，它必须从餐里拿掉，并补同等热量的主食。"""
    out, changes = apply_explicit_edits(_payload(), "不吃红薯")
    assert changes
    names = [item["name"] for item in out["meals"][0]["items"]]
    assert "红薯" not in names
    # 热量不能因此明显下降（替换而非删除）
    assert out["meals"][0]["kcal"] >= 200


def test_extract_avoided_foods() -> None:
    from app.domain.draft_edits import extract_avoided_foods

    assert extract_avoided_foods("我不吃红薯，也别吃虾") == ["红薯", "虾"]
    assert extract_avoided_foods("把红薯换成土豆") == []


def _week_payload() -> dict:
    meals = []
    for day in range(1, 8):
        for slot, title in (("breakfast", "早餐"), ("lunch", "午餐"), ("dinner", "晚餐")):
            meals.append({
                "day_index": day, "slot": slot, "title": title,
                "items": [{"name": "红薯", "grams": 300, "kcal": 258,
                           "protein": 4.8, "fat": 0.3, "carb": 60}],
                "kcal": 258, "protein_g": 4.8, "fat_g": 0.3, "carb_g": 60,
            })
    return {"days": [], "meals": meals, "summary": ""}


def test_avoid_carb_uses_meal_appropriate_substitute() -> None:
    """第一版把 7 天的红薯全换成米饭，早上也吃米饭——这是不合理的。

    现在按餐次选替代：早餐 燕麦/玉米/土豆，午餐 米饭/玉米/土豆，晚餐 土豆/玉米/米饭。
    """
    out, _ = apply_explicit_edits(_week_payload(), "不吃红薯")
    breakfasts = [m["items"][0]["name"] for m in out["meals"] if m["slot"] == "breakfast"]
    lunches = [m["items"][0]["name"] for m in out["meals"] if m["slot"] == "lunch"]
    assert "米饭" not in breakfasts, f"早餐不该是米饭：{breakfasts}"
    assert {"燕麦", "玉米", "土豆"} <= set(breakfasts), "早餐应在谷薯之间轮换"
    assert "米饭" in lunches, "午餐出现米饭是合理的"
    assert len(set(breakfasts)) > 1, "不能七天都一样"


def test_avoid_protein_prefers_meal_context() -> None:
    """不吃虾时，早餐的蛋白源应是鸡蛋/酸奶，而不是鸡胸肉。"""
    payload = {
        "days": [],
        "meals": [
            {"day_index": 1, "slot": "breakfast", "title": "早餐",
             "items": [{"name": "虾", "grams": 150, "kcal": 149, "protein": 36, "fat": 0.5, "carb": 0},
                       {"name": "燕麦", "grams": 45, "kcal": 175, "protein": 7.6, "fat": 3.1, "carb": 30}],
             "kcal": 324, "protein_g": 43.6, "fat_g": 3.6, "carb_g": 30},
            {"day_index": 1, "slot": "dinner", "title": "晚餐",
             "items": [{"name": "虾", "grams": 150, "kcal": 149, "protein": 36, "fat": 0.5, "carb": 0}],
             "kcal": 149, "protein_g": 36, "fat_g": 0.5, "carb_g": 0},
        ],
        "summary": "",
    }
    out, _ = apply_explicit_edits(payload, "不吃虾")
    names = {m["slot"]: [i["name"] for i in m["items"]] for m in out["meals"]}
    assert "虾" not in names["breakfast"]
    assert names["breakfast"][0] in {"鸡蛋", "希腊酸奶", "北豆腐"}
