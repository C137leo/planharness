"""草案反馈 → 具体改动（`app/domain/revisions.py`）。

这是「再改改点了没变化」的根因修复：反馈必须改变计划内容，而不只是解释文案。
"""

from __future__ import annotations

from app.domain.revisions import apply_feedback


def _payload() -> dict:
    return {
        "status": "ready",
        "summary": "基础计划",
        "days": [
            {"day_index": 1, "focus": "full_body_a", "actions": [
                {"slot": "strength", "action_ref": "goblet-squat", "action_type": "exercise",
                 "sets": 3, "meta": {"name_zh": "高脚杯深蹲", "movement_mode": "knee_dominant"}},
                {"slot": "strength", "action_ref": "glute-bridge", "action_type": "exercise",
                 "sets": 3, "meta": {"name_zh": "臀桥", "movement_mode": "hip_dominant"}},
                {"slot": "strength", "action_ref": "dumbbell-bench-press", "action_type": "exercise",
                 "sets": 3, "meta": {"name_zh": "哑铃卧推", "movement_mode": "horizontal_push"}},
            ]},
            {"day_index": 2, "focus": "full_body_a", "actions": [
                {"slot": "strength", "action_ref": "goblet-squat", "action_type": "exercise",
                 "sets": 3, "meta": {"name_zh": "高脚杯深蹲", "movement_mode": "knee_dominant"}},
            ]},
        ],
        # 每天蛋白源不同，才看得出「顺延一天」真的换了食材
        "meals": [
            {"day_index": day, "slot": "lunch", "title": "午餐",
             "items": [{"name": ["鸡胸肉", "北豆腐", "瘦牛肉", "三文鱼", "鸡蛋", "虾", "希腊酸奶"][day - 1],
                        "grams": 150}],
             "kcal": 700, "protein_g": 40, "fat_g": 10, "carb_g": 80}
            for day in range(1, 8)
        ],
    }


def test_variety_changes_every_session() -> None:
    """「动作要有区别」必须让每一场都变，包括第一天。"""
    out, changes = apply_feedback(_payload(), "每次动作都要有区别")
    assert changes
    before = [a["action_ref"] for a in _payload()["days"][0]["actions"]]
    after = [a["action_ref"] for a in out["days"][0]["actions"]]
    assert after != before, "第一天也必须变，否则用户会觉得没改"
    assert len(set(after)) == len(after), "同一天里不能出现重复动作"


def test_knee_hint_replaces_knee_dominant() -> None:
    """「膝盖不舒服」→ 膝主导动作换成髋/核心。"""
    out, changes = apply_feedback(_payload(), "膝盖不舒服，少点蹲")
    modes = [a["meta"]["movement_mode"] for a in out["days"][0]["actions"]]
    assert "knee_dominant" not in modes
    assert any("避膝" in item for item in changes)


def test_harder_increases_sets_and_easier_drops_one() -> None:
    harder, harder_changes = apply_feedback(_payload(), "想加强度")
    assert all(a["sets"] == 4 for a in harder["days"][0]["actions"] if a["action_type"] == "exercise")
    assert harder_changes

    easier, easier_changes = apply_feedback(_payload(), "太累了，轻松点")
    assert len(easier["days"][0]["actions"]) == 2, "减量应当去掉每场最后一个动作"
    assert easier_changes


def test_protein_feedback_rotates_meals() -> None:
    out, changes = apply_feedback(_payload(), "蛋白质来源丰富一点，别总吃鸡胸肉")
    assert changes
    day_before = sorted(m["day_index"] for m in _payload()["meals"])
    day_after = sorted(m["day_index"] for m in out["meals"])
    assert day_before == day_after, "只是顺延，不能丢餐"
    assert out["meals"] != _payload()["meals"]


def test_unmatched_feedback_is_reported_not_silently_ignored() -> None:
    """匹配不到任何规则时，changes 为空，由调用方给出提示。"""
    out, changes = apply_feedback(_payload(), "随便说点什么")
    assert changes == []
    assert out["days"][0]["actions"][0]["action_ref"] == "goblet-squat"
