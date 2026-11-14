"""多任务拆解/动作序号的纯函数测试。"""

from __future__ import annotations

from app.agent.legacy_coach_agent import _ordinal_indexes
from app.agent.intent_router import _normalize_task
from app.agent.task_models import AtomicTask, TaskPlan


def test_ordinal_indexes_keep_user_order() -> None:
    text = "今天不练第一个动作了，第三个动作改为拉伸，最后一个直接删掉"
    assert _ordinal_indexes(text) == [1, 3, -1]


def test_task_normalization_keeps_operations() -> None:
    skip = _normalize_task({
        "task_id": "T2", "intent": "adjust_today", "operation": "skip_action",
        "scope": "today", "task_order": 2, "user_goal": "跳过第一个动作",
    }, 2)
    replace = _normalize_task({
        "task_id": "T3", "intent": "adjust_today", "operation": "replace_action",
        "scope": "today", "task_order": 3, "user_goal": "第三个换成拉伸",
    }, 3)
    assert skip["operation"] == "skip_action"
    assert replace["operation"] == "replace_action"
    assert skip["task_id"] == "T2"


def test_task_plan_holds_multiple_atomic_tasks() -> None:
    plan = TaskPlan(tasks=[
        AtomicTask("T1", "meal", "mcd_order", "order_meal", task_order=1),
        AtomicTask("T2", "training", "adjust_today", "skip_action", task_order=2),
    ])
    assert len(plan.tasks) == 2
    assert plan.tasks[1].operation == "skip_action"


def test_skip_and_replace_use_same_snapshot(monkeypatch) -> None:
    import datetime as dt

    from app.agent import legacy_coach_agent as coach_agent

    actions = [
        {"slot": "warmup", "name": "动态热身", "action_ref": "dynamic_warmup",
         "sets": 1, "reps": None, "duration_min": 5},
        {"slot": "strength", "name": "深蹲", "action_ref": "goblet-squat",
         "sets": 2, "reps": 10, "duration_min": 8},
        {"slot": "strength", "name": "划船", "action_ref": "one-arm-dumbbell-row",
         "sets": 2, "reps": 10, "duration_min": 8},
        {"slot": "strength", "name": "硬拉", "action_ref": "romanian-deadlift",
         "sets": 2, "reps": 10, "duration_min": 8},
    ]
    day = dt.date(2026, 11, 11)
    saved = {}

    def fake_effective_day(db, user, day, fallback_next=False):
        return {"actions": [dict(item) for item in actions], "plan_version": 3,
                "borrowed_from": None, "summary": ""}

    def fake_save_override(db, user, day, **kwargs):
        saved.update(kwargs)
        return {"id": "adj-1"}

    monkeypatch.setattr(coach_agent, "effective_day", fake_effective_day)
    monkeypatch.setattr(coach_agent, "save_override", fake_save_override)

    tasks = [
        {"task_id": "T2", "intent": "adjust_today", "operation": "skip_action",
         "slots": {"target_index": 1}},
        {"task_id": "T3", "intent": "adjust_today", "operation": "replace_action",
         "slots": {"target_index": 3, "replacement_category": "stretching"}},
    ]
    result = coach_agent._apply_today_operations(
        None, type("U", (), {"id": "u1"})(), day,
        "第一个不练，第三个改成拉伸", tasks, [],
    )
    names = [(item["slot"], item["name"]) for item in saved["actions"]]
    assert ("warmup", "动态热身") in names
    assert ("strength", "深蹲") not in names
    assert ("strength", "划船") in names
    assert ("mobility", "全身拉伸") in names
    assert saved["constraints"]["operations"] == ["skip_action", "replace_action"]
    assert len(result["changes"]) == 2


class _FakeTasksClient:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = 0

    def available(self):
        return True

    def complete(self, messages, **kwargs):
        self.calls += 1
        content = self.contents.pop(0) if self.contents else ""
        return {"message": {"content": content}}


def test_understand_tasks_splits_with_parameters_in_one_call() -> None:
    import json
    from app.agent import intent_router
    tasks = [
        {"task_id": "T1", "intent": "adjust_today", "operation": "skip_action",
         "scope": "today", "slots": {"target_index": 1}},
        {"task_id": "T2", "intent": "adjust_today", "operation": "replace_action",
         "scope": "today", "slots": {"target_index": 3, "replacement_category": "stretching"}},
    ]
    client = _FakeTasksClient([json.dumps({"tasks": tasks})])
    actual, _, _, error = intent_router.understand_tasks(
        client, "第一个不练，第三个换拉伸", context={})
    assert len(actual) == 2
    assert actual[1]["slots"]["target_index"] == 3
    assert client.calls == 1 and error == ""


def test_nutrition_slots_survive_cleaning() -> None:
    from app.agent.legacy_coach_agent import _clean_model_constraints

    slots = _clean_model_constraints({
        "avoid_foods": ["鸡胸肉"], "meal_slot": "lunch",
        "from_food": "鸡胸肉", "to_food": "牛肉", "foods": ["汉堡"],
    })
    assert slots["avoid_foods"] == ["鸡胸肉"]
    assert slots["meal_slot"] == "lunch"
    assert slots["to_food"] == "牛肉"


def test_nutrition_task_keeps_operation() -> None:
    task = _normalize_task({
        "task_id": "T1", "domain": "nutrition", "intent": "nutrition_advice",
        "operation": "modify_meal_plan", "action_type": "update",
        "scope": "today",
    }, 1)
    assert task["operation"] == "modify_meal_plan"
    assert task["action_type"] == "update"
    assert task["domain"] == "nutrition"


def test_recalc_meal_uses_items() -> None:
    from app.agent.legacy_coach_agent import _recalc_meal

    meal = _recalc_meal({"items": [
        {"name": "牛肉", "kcal": 250, "protein": 26, "fat": 15, "carb": 0},
        {"name": "米饭", "kcal": 116, "protein": 2.6, "fat": 0.3, "carb": 25.9},
    ]})
    assert meal["kcal"] == 366
    assert meal["protein_g"] == 28.6


def test_food_preference_candidates() -> None:
    from app.agent.legacy_coach_agent import _slot_candidates

    items = _slot_candidates({
        "liked_foods": ["牛肉"], "disliked_foods": ["鸡胸肉"],
        "allergen_foods": ["花生"],
    })
    categories = {item["category"]: item for item in items}
    assert categories["food_like"]["value"] == "喜欢吃牛肉"
    assert categories["food_dislike"]["value"] == "不喜欢吃鸡胸肉"
    assert categories["food_allergy"]["structured"]["food"] == "花生"


def test_add_two_actions_preserves_existing(monkeypatch) -> None:
    import datetime as dt
    from types import SimpleNamespace

    from app.agent import legacy_coach_agent as coach_agent

    actions = [{"slot": "strength", "name": "深蹲", "action_ref": "goblet-squat",
                "sets": 2, "reps": 10, "duration_min": 8}]
    saved = {}

    def fake_effective_day(db, user, day, fallback_next=False):
        return {"actions": [dict(item) for item in actions], "plan_version": 3,
                "meals": [], "borrowed_from": None, "summary": ""}

    def fake_save_override(db, user, day, **kwargs):
        saved.update(kwargs)
        return {"id": "adj-add"}

    def fake_pick(profile, muscle, used):
        names = {"chest": ("push-up", "俯卧撑"), "back": ("inverted-row", "反向划船")}
        ref, name = names[muscle]
        return {"ref": ref, "name": name, "equipment": "Bodyweight",
                "movement_mode": "", "primary_muscle": muscle,
                "meta": {"name_zh": name}}

    monkeypatch.setattr(coach_agent, "effective_day", fake_effective_day)
    monkeypatch.setattr(coach_agent, "save_override", fake_save_override)
    monkeypatch.setattr(coach_agent, "_pick_action_for_muscle", fake_pick)

    tasks = [
        {"task_id": "T1", "intent": "adjust_today", "operation": "add_action",
         "slots": {"target_muscle": "chest"}},
        {"task_id": "T2", "intent": "adjust_today", "operation": "add_action",
         "slots": {"target_muscle": "back"}},
    ]
    result = coach_agent._apply_today_operations(
        None, SimpleNamespace(id="u1"), dt.date(2026, 11, 11),
        "加两个动作，一个练胸一个练背", tasks, [],
    )
    refs = [item["action_ref"] for item in saved["actions"]]
    assert "goblet-squat" in refs
    assert "push-up" in refs and "inverted-row" in refs
    assert len([item for item in result["task_results"] if item["status"] == "success"]) == 2


def test_fallback_decompose_explicit_operations() -> None:
    from app.agent.intent_router import _fallback_decompose

    text = ("今天加两个动作一个练胸一个练背再去掉拉伸,"
            "然后我喜欢吃牛肉,把晚餐替换成牛肉")
    tasks = _fallback_decompose(text, [])
    ops = [(task["operation"], task["slots"]) for task in tasks]
    assert any(op == "add_action" and slots.get("target_muscle") == "chest"
               for op, slots in ops)
    assert any(op == "add_action" and slots.get("target_muscle") == "back"
               for op, slots in ops)
    assert any(op == "remove_action" and slots.get("target_action") == "拉伸"
               for op, slots in ops)
    assert any(op == "update_food_preference"
               and slots.get("liked_foods") == ["牛肉"] for op, slots in ops)
    assert any(op == "replace_meal_item" and slots.get("meal_slot") == "dinner"
               for op, slots in ops)


def test_fallback_decompose_ambiguous_actions() -> None:
    from app.agent.intent_router import _fallback_decompose

    text = ("今天加一个动作蹲腿,减少一个练背动作,加个拉伸动作,"
            "然后我喜欢吃鸡肉,把晚餐替换成鸡肉")
    tasks = _fallback_decompose(text, [])
    assert any(t["operation"] == "add_action"
               and t["slots"].get("target_action") == "深蹲" for t in tasks)
    assert any(t["operation"] == "remove_action"
               and t["slots"].get("target_muscle") == "back" for t in tasks)
    assert any(t["operation"] == "add_action"
               and t["slots"].get("target_category") == "stretching" for t in tasks)
    assert any(t["operation"] == "replace_meal_item"
               and t["slots"].get("meal_slot") == "dinner" for t in tasks)


def test_fallback_wrist_scenario_keeps_all_tasks() -> None:
    from app.agent.intent_router import _fallback_decompose

    text = ("把划船和俯卧撑都去掉,我手不舒服,晚餐吃点米饭,"
            "然后帮我点个炸鸡")
    tasks = _fallback_decompose(text, [])
    ops = [(t["operation"], t["slots"]) for t in tasks]
    assert sum(1 for op, _ in ops if op == "remove_action") == 2
    assert any(op == "safety_advice"
               and slots.get("injured_regions") == ["wrist"] for op, slots in ops)
    assert any(op == "order_meal" and slots.get("foods") == ["炸鸡"]
               for op, slots in ops)
    assert any(op == "replace_meal_item"
               and slots.get("meal_slot") == "dinner" for op, slots in ops)


def test_food_category_matches_staple_not_protein() -> None:
    from app.agent.legacy_coach_agent import _food_category

    assert _food_category("米饭") == "staple"
    assert _food_category("红薯") == "staple"
    assert _food_category("三文鱼") == "protein"
