"""Contract/API regression with scripted model outputs; never an NLU accuracy score.

The live evaluation actor is in scripts/eval_dialogues.py and receives only user
text. These tests isolate Python's persistence/context/compensation obligations.
"""
import json
import copy
from pathlib import Path

import pytest
from sqlalchemy import select

from app.evals.dialogue_runner import DialogueEnvironment, DAY, diff, judge
from app.domain.day_plan import effective_day, save_override
from app.models import CoachTaskExecution, User
from app.seed import DEMO_USER_ID


class ScriptedModel:
    def __init__(self):
        self.outputs = []
    def available(self):
        return True
    def complete(self, messages, **kwargs):
        output = self.outputs.pop(0)
        if callable(output):
            output = output(json.loads(messages[-1]["content"])["context"])
        return {"message": {"content": json.dumps(output, ensure_ascii=False)}}


def task(op, slots=None, tid="T1", goal=None, **extra):
    domain, intent = ("nutrition", "nutrition_advice") if op in {"add_meal_item", "replace_meal_item"} else (
        ("dialogue", "dialogue") if op in {"undo_task", "inspect_changes", "acknowledge", "cancel_tasks", "clarify"} else ("training", "adjust_today"))
    return {"task_id": tid, "domain": domain, "intent": intent, "operation": op,
            "scope": "message" if domain == "dialogue" else "today", "slots": slots or {},
            "user_goal": goal or op, **extra}


@pytest.fixture()
def env(tmp_path):
    model = ScriptedModel()
    env = DialogueEnvironment(tmp_path / "conversation.db", "contract-conversation", model)
    yield env, model
    env.close()


def step(pair, text, tasks=None, **extra):
    env, model = pair
    model.outputs.append({"tasks": tasks or [], **extra})
    broad = [t for t in (tasks or []) if t["operation"] == "regenerate_training_today"]
    if broad:
        model.outputs.append({"authorized_task_ids": [t["task_id"] for t in broad], "question": ""})
    index = len(env.history()) // 2 + 1
    return env.step(text, index)


def statuses(row):
    return [r["status"] for r in row["result"]["task_results"]]


def test_seven_turns_keep_real_history_database_and_verified_corrections(env):
    runtime, model = env
    initial = runtime.snapshot()
    first = step(env, "晚餐加点牛肉，午饭别动。", [task("add_meal_item", {"meal_slot": "dinner", "food": "牛肉"}, goal="晚餐新增牛肉")])
    assert statuses(first) == ["success"]
    meal = first["after"]["days"][str(DAY)]["meals"]["dinner"]
    assert [i["name"] for i in meal["items"]] == ["鸡胸肉", "米饭", "牛肉"]
    second = step(env, "划船改三组。", [task("change_sets", {"target_index": 2, "sets": 3}, goal="划船减至三组")])
    third = step(env, "刚调的那项再少一组。", [task("change_sets", {"sets_delta": -1},
                  context_reference={"action_ref": "one-arm-dumbbell-row"})])
    assert statuses(second) == statuses(third) == ["success"]
    assert len(third["history_before"]) == 4
    assert len(third["context_used"]["execution_history"]) == 2
    assert third["after"]["days"][str(DAY)]["actions"]["one-arm-dumbbell-row"]["sets"] == 2
    fourth = step(env, "刚才到底动了什么？", [task("inspect_changes")])
    assert "晚餐新增牛肉" in fourth["reply"] and "划船减至三组" in fourth["reply"]
    assert not fourth["actual_data_changes"]
    fifth = step(env, "午餐保持原样。", dialogue_act="acknowledge", execution_mode="noop")
    assert not fifth["actual_data_changes"]
    sixth = step(env, "晚饭那次新增撤销，训练保留。", [task("undo_task", {"meal_slot": "dinner"})], dialogue_act="correction")
    assert statuses(sixth) == ["success"]
    assert sixth["after"]["days"][str(DAY)]["meals"] == initial["days"][str(DAY)]["meals"]
    assert sixth["after"]["days"][str(DAY)]["actions"]["one-arm-dumbbell-row"]["sets"] == 2
    seventh = step(env, "我没让你改午餐，你核实一下。", [task("inspect_changes", {"meal_slot": "lunch"})])
    assert "没有找到" in seventh["reply"]
    assert not seventh["actual_data_changes"]
    assert len(seventh["history_after"]) == 14
    with runtime.factory() as db:
        records = db.scalars(select(CoachTaskExecution)).all()
        assert any(r.status == "compensated" and r.compensated_by for r in records)
        assert all(r.thread == runtime.thread for r in records)


def test_ambiguous_plan_regeneration_is_clarification_not_mutation(env):
    row = step(env, "今天的计划重新生成一下吧。", dialogue_act="clarify",
               clarification_question="你想重新生成今天的训练计划，还是饮食计划？")
    assert statuses(row) == ["clarification_required"]
    assert not row["actual_data_changes"]
    assert "训练计划，还是饮食计划" in row["reply"]


def test_regenerate_training_preserves_new_meal_and_can_be_safely_reverted(env):
    first = step(env, "晚餐添牛肉。", [task("add_meal_item", {"meal_slot": "dinner", "food": "牛肉"})])
    before_training = first["after"]["days"][str(DAY)]["actions"]
    second = step(env, "今天训练重新排，饮食别改。", [task("regenerate_training_today")])
    assert statuses(second) == ["success"]
    assert second["after"]["days"][str(DAY)]["meals"] == first["after"]["days"][str(DAY)]["meals"]
    assert second["after"]["days"][str(DAY)]["actions"] != before_training
    execution_id = second["result"]["task_results"][0]["execution_id"]
    third = step(env, "撤销刚才重排训练那次。", [task("undo_task", {"execution_id": execution_id})])
    assert statuses(third) == ["success"]
    assert third["after"]["days"][str(DAY)]["actions"] == before_training


def test_ambiguous_multi_task_undo_requires_resource(env):
    first = step(env, "第一项跳过，第三项换拉伸。", [task("skip_action", {"target_index": 1}),
                  task("replace_action", {"target_index": 3, "replacement_category": "stretching"}, tid="T2")])
    assert statuses(first) == ["success", "success"]
    second = step(env, "刚才那个恢复吧。", [task("undo_task")])
    assert statuses(second) == ["clarification_required"]
    assert not second["actual_data_changes"]


def test_compensation_detects_newer_resource_change(env):
    runtime, _ = env
    first = step(env, "划船改三组。", [task("change_sets", {"target_index": 2, "sets": 3})])
    eid = first["result"]["task_results"][0]["execution_id"]
    with runtime.factory() as db:
        user = db.get(User, DEMO_USER_ID)
        current = effective_day(db, user, DAY)
        actions = copy.deepcopy(current["actions"])
        actions[1]["sets"] = 2
        save_override(db, user, DAY, actions=actions, meals=current["meals"], constraints=current["constraints"],
                      summary="concurrent user change", reason="test", base_version=current["plan_version"])
        db.commit()
    row = step(env, "恢复那条旧修改。", [task("undo_task", {"execution_id": eid})])
    assert row["result"]["task_results"][0]["failure_stage"] == "conflict"
    assert not row["actual_data_changes"]
    assert row["after"]["days"][str(DAY)]["actions"]["one-arm-dumbbell-row"]["sets"] == 2


def test_proposal_and_cancel_never_dispatch_mutations(env):
    first = step(env, "先给方案，不要执行。", [task("skip_action", {"target_index": 1})], execution_mode="propose")
    assert statuses(first) == ["confirmation_required"]
    assert not first["actual_data_changes"]
    # Cancel mode dominates a model's leftover mutation tasks.
    row = step(env, "取消刚才的方案。", [task("skip_action", {"target_index": 1})], dialogue_act="cancel")
    assert not row["actual_data_changes"]


def test_audit_records_are_conversation_scoped(env):
    runtime, _ = env
    first = step(env, "划船改三组。", [task("change_sets", {"target_index": 2, "sets": 3})])
    eid = first["result"]["task_results"][0]["execution_id"]
    runtime.thread = "different-conversation"
    row = step(env, "恢复这条记录。", [task("undo_task", {"execution_id": eid})])
    assert statuses(row) == ["clarification_required"]
    assert not row["context_used"]["execution_history"]
    assert not row["actual_data_changes"]


@pytest.mark.parametrize("operation,slots", [
    ("add_meal_item", {"meal_slot": "dinner", "food": "牛肉", "grams": -10}),
    ("change_sets", {"target_index": 2, "sets_delta": -5}),
    ("change_sets", {"target_index": 2, "sets": 1, "date": "tomorrow"}),
])
def test_invalid_parameters_and_date_cannot_mutate(env, operation, slots):
    row = step(env, "调整一下。", [task(operation, slots)])
    assert statuses(row) == ["clarification_required"]
    assert not row["actual_data_changes"]


def test_unavailable_model_cannot_pass_a_noop_or_context_test(env):
    row = step(env, "保持原样。", dialogue_act="acknowledge", execution_mode="noop")
    row["model_calls"] = [{"error_type": "ConnectError"}]
    judge(row, {"completion": False, "assertions": [], "allowed_changes": []})
    assert not row["passed"] and not row["model_available"]


def test_suite_has_105_distinct_turns_and_separate_sealed_holdout():
    root = Path(__file__).parents[2] / "app/evals/dialogue_suites"
    suite = json.loads((root / "expanded_regression.json").read_text())
    assert len(suite["conversations"]) >= 15
    turns = [t for c in suite["conversations"] for t in c["turns"]]
    assert all(5 <= len(c["turns"]) <= 10 for c in suite["conversations"])
    assert len({t["text"] for t in turns}) >= 100
    # Expectations are an oracle, not prompts or prefilled tool choices.
    assert all("intent" not in t["expect"] and "tool" not in t["expect"] for t in turns)
    import hashlib
    assert hashlib.sha256((root / "holdout-final.json").read_bytes()).hexdigest() == "85c109b5fb2742057e67a983e7c97d255aaf752861adc350fe14e944e7c7a9ba"


def test_narrow_meal_undo_never_restores_both_meals(env):
    first = step(env, "今天午饭晚饭都先不吃鸡胸肉。", [task("modify_meal_plan", {"meal_slot": "all", "avoid_foods": ["鸡胸肉"]}, intent="nutrition_advice", domain="nutrition")])
    assert statuses(first) == ["success"]
    row = step(env, "只恢复午餐，晚餐保持现在这样。", [task("undo_task", {"meal_slot": "lunch"})])
    assert statuses(row) == ["clarification_required"]
    assert not row["actual_data_changes"]


def test_aba_cannot_undo_an_old_execution_over_new_decisions(env):
    first = step(env, "划船两组。", [task("change_sets", {"target_index": 2, "sets": 2})])
    eid = first["result"]["task_results"][0]["execution_id"]
    step(env, "改成三组。", [task("change_sets", {"target_index": 2, "sets": 3})])
    step(env, "重新决定两组。", [task("change_sets", {"target_index": 2, "sets": 2})])
    row = step(env, "恢复最早的那次，别覆盖后来的决定。", [task("undo_task", {"execution_id": eid})])
    assert row["result"]["task_results"][0]["failure_stage"] == "conflict"
    assert not row["actual_data_changes"]


def test_relative_dependent_task_uses_latest_numeric_state(env):
    row = step(env, "先设三组，再少一组。", [task("change_sets", {"target_index": 2, "sets": 3}),
                task("change_sets", {"target_index": 2, "sets_delta": -1}, tid="T2", dependencies=["T1"])])
    assert statuses(row) == ["success", "success"]
    assert row["after"]["days"][str(DAY)]["actions"]["one-arm-dumbbell-row"]["sets"] == 2


def test_failed_dependency_blocks_compensation(env):
    first = step(env, "划船两组。", [task("change_sets", {"target_index": 2, "sets": 2})])
    eid = first["result"]["task_results"][0]["execution_id"]
    row = step(env, "前一步成功后才能恢复。", [task("skip_action", {"target_action": "不存在的动作"}),
                task("undo_task", {"execution_id": eid}, tid="T2", dependencies=["T1"])])
    assert statuses(row) == ["clarification_required", "blocked"]
    assert not row["actual_data_changes"]


def test_current_session_changes_are_journaled_and_reversible(env):
    from app.domain import workout
    from app.models import WorkoutSession
    runtime, _ = env
    with runtime.factory() as db:
        user = db.get(User, DEMO_USER_ID)
        session = workout.start_session(db, user, DAY)
        sid = session.id
        original_items = copy.deepcopy(session.items)
    first = step(env, "当前训练第一个跳过。", [task("skip_action", {"target_index": 1}, intent="adjust_workout", scope="current_workout")])
    eid = first["result"]["task_results"][0]["execution_id"]
    assert eid
    with runtime.factory() as db:
        record = db.get(CoachTaskExecution, eid)
        assert any(c["kind"] == "sessions" for c in record.resource_changes)
    row = step(env, "恢复刚才跳过的训练动作。", [task("undo_task", {"execution_id": eid})])
    assert statuses(row) == ["success"]
    with runtime.factory() as db:
        assert db.get(WorkoutSession, sid).items == original_items


def test_capability_dispatch_does_not_depend_on_duplicate_model_intent_label(env):
    # Real model failure: a valid replace/skip operation was rejected solely
    # because a second free-form label repeated the operation instead of intent.
    row = step(env, "把晚饭鸡胸肉换成牛肉。", [task("replace_meal_item", {
        "meal_slot": "dinner", "from_food": "鸡胸肉", "to_food": "牛肉"}, intent="replace_meal_item")])
    assert statuses(row) == ["success"]
    row = step(env, "第一个跳过。", [task("skip_action", {"target_index": 1}, intent="skip_action")])
    assert statuses(row) == ["success"]


def test_invalid_model_scope_is_rejected_not_silently_widened(env):
    row = step(env, "晚饭换牛肉。", [task("replace_meal_item", {
        "meal_slot": "dinner", "to_food": "牛肉"}, scope="invented_scope")])
    assert row["result"]["error"] == "semantic_schema_invalid"
    assert not row["actual_data_changes"]


@pytest.mark.parametrize("selector", [
    {"meal_slot": "dinner", "source_operation": "add_meal_item"},
    {"meal_slot": "dinner", "target_action": "add_meal_item"},
    {"meal_slot": "dinner", "target_action": "牛肉"},
    {"meal_slot": "dinner", "food": "牛肉"},
])
def test_meal_compensation_selectors_are_typed_by_resource_not_exercise(env, selector):
    step(env, "晚饭添加牛肉。", [task("add_meal_item", {"meal_slot": "dinner", "food": "牛肉"})])
    row = step(env, "撤销那顿新增的食物。", [task("undo_task", selector)])
    assert statuses(row) == ["success"]
    assert all(i["name"] != "牛肉" for i in row["after"]["days"][str(DAY)]["meals"]["dinner"]["items"])


def test_remove_food_uses_the_food_resource_parameter(env):
    step(env, "加牛肉。", [task("add_meal_item", {"meal_slot": "dinner", "food": "牛肉"})])
    row = step(env, "只移除这餐牛肉。", [task("remove_meal_item", {"meal_slot": "dinner", "food": "牛肉"}, intent="nutrition_advice", domain="nutrition")])
    assert statuses(row) == ["success"]
    assert [i["name"] for i in row["after"]["days"][str(DAY)]["meals"]["dinner"]["items"]] == ["鸡胸肉", "米饭"]


def test_clarification_question_is_presented_even_if_model_sets_noop(env):
    row = step(env, "重新生成计划。", dialogue_act="clarify", execution_mode="noop",
               clarification_question="请明确训练还是饮食。")
    assert statuses(row) == ["clarification_required"]
    assert "训练还是饮食" in row["reply"]
    assert not row["actual_data_changes"]


def test_inspection_all_meals_uses_records_instead_of_literal_all_selector(env):
    step(env, "加牛肉。", [task("add_meal_item", {"meal_slot": "dinner", "food": "牛肉"})])
    row = step(env, "查所有修改。", [task("inspect_changes", {"meal_slot": "all", "target_action": "all"})])
    assert "牛肉 100克" in row["reply"]
    assert not row["actual_data_changes"]


def test_task_local_modes_allow_cancel_proposal_and_undo_saved_change_together(env):
    step(env, "晚饭先加牛肉。", [task("add_meal_item", {"meal_slot": "dinner", "food": "牛肉"})])
    row = step(env, "明天那个建议取消，另外今天晚饭已保存的添加撤销。", [
        task("cancel_tasks", tid="T1", execution_mode="noop"),
        task("undo_task", {"meal_slot": "dinner"}, tid="T2", execution_mode="execute")], dialogue_act="cancel")
    assert statuses(row) == ["success", "success"]
    assert all(i["name"] != "牛肉" for i in row["after"]["days"][str(DAY)]["meals"]["dinner"]["items"])


def test_inspection_returns_verifiable_fact_ids(env):
    first = step(env, "午餐加牛肉。", [task("add_meal_item", {"meal_slot": "lunch", "food": "牛肉"})])
    eid = first["result"]["task_results"][0]["execution_id"]
    row = step(env, "核实午餐修改记录。", [task("inspect_changes", {"meal_slot": "lunch"})])
    inspection = row["result"]["task_results"][0]["inspection"]
    assert inspection["matched_execution_ids"] == [eid]
    assert inspection["facts"][0]["changes"]


def test_request_level_clarification_keeps_other_independent_executable_goals(env):
    row = step(env, "删掉不存在的动作，同时晚餐添加牛肉。", [
        task("remove_action", {"target_action": "missing"}, clarification_needed=True),
        task("add_meal_item", {"meal_slot": "dinner", "food": "牛肉"}, tid="T2")],
        dialogue_act="clarify", clarification_question="哪个动作？")
    assert statuses(row) == ["clarification_required", "success"]
    assert any(i["name"] == "牛肉" for i in row["after"]["days"][str(DAY)]["meals"]["dinner"]["items"])


def test_undo_lunch_does_not_conflict_with_independent_dinner_constraint(env):
    first = step(env, "午餐鸡胸肉换牛肉。", [task("replace_meal_item", {"meal_slot": "lunch", "from_food": "鸡胸肉", "to_food": "牛肉"})])
    eid = first["result"]["task_results"][0]["execution_id"]
    step(env, "晚餐鸡胸肉换豆腐。", [task("replace_meal_item", {"meal_slot": "dinner", "from_food": "鸡胸肉", "to_food": "豆腐"})])
    row = step(env, "只恢复午餐。", [task("undo_task", {"execution_id": eid, "meal_slot": "lunch"})])
    assert statuses(row) == ["success"]
    meals = row["after"]["days"][str(DAY)]["meals"]
    assert meals["lunch"]["items"][0]["name"] == "鸡胸肉"
    assert meals["dinner"]["items"][0]["name"] == "豆腐"


def test_independent_scope_guard_blocks_planner_guess_for_broad_regeneration(env):
    runtime, model = env
    model.outputs.extend([{"tasks": [task("regenerate_training_today")]},
                          {"authorized_task_ids": [], "question": "训练还是饮食？"}])
    row = runtime.step("今天计划重新生成。", 1)
    assert statuses(row) == ["clarification_required"]
    assert not row["actual_data_changes"]


def test_stale_daily_revision_cannot_overwrite_another_request(env):
    runtime, _ = env
    with runtime.factory() as db:
        user = db.get(User, DEMO_USER_ID)
        old = effective_day(db, user, DAY)
    step(env, "划船改三组。", [task("change_sets", {"target_index": 2, "sets": 3})])
    with runtime.factory() as db:
        user = db.get(User, DEMO_USER_ID)
        with pytest.raises(ValueError, match="plan_adjustment_conflict"):
            save_override(db, user, DAY, actions=old["actions"], meals=old["meals"], constraints={},
                          summary="stale", reason="test", base_version=old["plan_version"],
                          expected_adjustment_id=old["adjustment_id"])
        db.rollback()
    assert runtime.snapshot()["days"][str(DAY)]["actions"]["one-arm-dumbbell-row"]["sets"] == 3


def test_multiple_recent_action_antecedents_cannot_be_guessed_by_model(env):
    step(env, "深蹲两组，划船三组。", [task("change_sets", {"target_index": 1, "sets": 2}),
         task("change_sets", {"target_index": 2, "sets": 3}, tid="T2")])
    row = step(env, "刚才那个再少一组。", [task("change_sets", {"target_index": 1, "sets_delta": -1})])
    assert statuses(row) == ["clarification_required"]
    assert not row["actual_data_changes"]
    # Explicit entity grounding permits a subsequent unambiguous clarification.
    row = step(env, "我说的是划船，再少一组。", [task("change_sets", {"target_index": 2, "sets_delta": -1})])
    assert statuses(row) == ["success"]
    assert row["after"]["days"][str(DAY)]["actions"]["one-arm-dumbbell-row"]["sets"] == 2


def test_model_truncation_is_retried_with_larger_bounded_budget():
    from app.agent.intent_router import understand_tasks
    class TruncatedThenComplete:
        def __init__(self):
            self.budgets = []
        def available(self):
            return True
        def complete(self, messages, **kwargs):
            self.budgets.append(kwargs["max_tokens"])
            if len(self.budgets) == 1:
                return {"finish_reason": "length", "message": {"content": '{"tasks":['}}
            return {"finish_reason": "stop", "message": {"content": json.dumps({"tasks": [task("inspect_changes")]})}}
    model = TruncatedThenComplete()
    tasks, _, _, error = understand_tasks(model, "核实修改记录。", context={})
    assert not error and tasks[0]["operation"] == "inspect_changes"
    assert model.budgets == [2400, 4800]


@pytest.mark.parametrize("grams", [1, 5, 20, 25, 100, 0.5])
def test_positive_food_increments_do_not_have_an_arbitrary_minimum_portion(env, grams):
    row = step(env, "按明确的小份量添加，不替换其他食物。", [task("add_meal_item", {"meal_slot": "lunch", "food": "牛肉", "grams": grams})])
    assert statuses(row) == ["success"]
    items = row["after"]["days"][str(DAY)]["meals"]["lunch"]["items"]
    assert next(i["grams"] for i in items if i["name"] == "牛肉") == grams
    assert next(i["grams"] for i in items if i["name"] == "鸡胸肉") == 150


@pytest.mark.parametrize("name,key", [("牛肉丝", "牛肉"), ("嫩豆腐", "豆腐"), ("鸡胸肉丁", "鸡胸肉"), ("清蒸三文鱼", "三文鱼")])
def test_ingredient_identity_is_distinct_from_display_name(name, key):
    from app.domain.nutrition_catalog import ingredient_key
    assert ingredient_key(name) == key
    assert ingredient_key("牛肉豆腐混合菜") is None


def test_same_ingredient_different_display_names_aggregate_quantity(env):
    step(env, "午餐少量添加。", [task("add_meal_item", {"meal_slot": "lunch", "food": "牛肉丝", "grams": 30})])
    row = step(env, "这份同种食材再加五克。", [task("add_meal_item", {"meal_slot": "lunch", "food": "牛肉", "grams": 5})])
    items = row["after"]["days"][str(DAY)]["meals"]["lunch"]["items"]
    beef = [i for i in items if i.get("ingredient_key") == "牛肉"]
    assert len(beef) == 1 and beef[0]["grams"] == 35


def test_proposal_preserves_specific_advice_without_writing(env):
    row = step(env, "只给建议和利弊，不保存。", [task("add_meal_item", {
        "meal_slot": "dinner", "food": "牛肉", "grams": 30,
        "note": "追加30克牛肉约75千卡、7.8克蛋白；蛋白增加，同时热量和脂肪也会上升。"},
        execution_mode="propose")])
    assert statuses(row) == ["confirmation_required"]
    assert "7.8" in row["reply"] and "脂肪" in row["reply"] and "尚未保存" in row["reply"]
    assert not row["actual_data_changes"]


def test_structured_proposal_renders_benefits_and_tradeoffs_without_writes(env):
    row = step(env, "先讲方案的利弊。", [task("change_sets", {"target_index": 2, "sets": 2},
        execution_mode="propose", proposal_details={"summary": "划船减少到两组",
        "benefits": ["减少本次疲劳"], "tradeoffs": ["本次训练量下降"], "assumptions": ["没有疼痛"]})])
    assert "好处：减少本次疲劳" in row["reply"]
    assert "取舍：本次训练量下降" in row["reply"]
    assert not row["actual_data_changes"]


def test_undo_reply_and_query_describe_current_inverse_state_not_original_goal(env):
    step(env, "划船两组。", [task("change_sets", {"target_index": 2, "sets": 2})])
    step(env, "临时加回一组。", [task("change_sets", {"target_index": 2, "sets_delta": 1}, goal="划船加回一组到三组")])
    row = step(env, "撤销最后一次加组。", [task("undo_task")])
    eid = row["result"]["task_results"][0]["execution_id"]
    assert "2组" in row["reply"] and "3组" in row["reply"]
    assert "加回一组到三组" not in row["reply"]
    query = step(env, "现在真实组数是多少？", [task("inspect_changes", {
        "action_ref": "one-arm-dumbbell-row", "source_operation": "change_sets"},
        context_reference={"execution_id": eid})])
    result = query["result"]["task_results"][0]
    assert result["inspection"]["matched_execution_ids"] == [eid]
    assert result["inspection"]["current_state"]["actions"]["one-arm-dumbbell-row"]["sets"] == 2
    assert "当前划船为2组" in query["reply"]
    assert not query["actual_data_changes"]


def test_model_selected_execution_ids_are_not_user_authorization_for_ambiguous_undo(env):
    first = step(env, "深蹲两组，划船三组。", [task("change_sets", {"target_index": 1, "sets": 2}),
                 task("change_sets", {"target_index": 2, "sets": 3}, tid="T2")])
    ids = [r["execution_id"] for r in first["result"]["task_results"]]
    row = step(env, "刚才那个恢复吧。", [task("undo_task", {"execution_id": ids[0]}),
                task("undo_task", {"execution_id": ids[1]}, tid="T2")])
    assert statuses(row) == ["clarification_required", "clarification_required"]
    assert not row["actual_data_changes"]
    row = step(env, "我说划船恢复四组，深蹲两组保持。", [task("undo_task", {"execution_id": ids[1], "action_ref": "one-arm-dumbbell-row"})])
    assert statuses(row) == ["success"]
    assert row["after"]["days"][str(DAY)]["actions"]["goblet-squat"]["sets"] == 2


def test_ambiguous_record_ids_are_also_guarded_for_multiple_meals(env):
    first = step(env, "午餐晚餐各加牛肉。", [task("add_meal_item", {"meal_slot": "lunch", "food": "牛肉"}),
          task("add_meal_item", {"meal_slot": "dinner", "food": "牛肉"}, tid="T2")])
    ids = [r["execution_id"] for r in first["result"]["task_results"]]
    row = step(env, "刚改的那顿撤回。", [task("undo_task", {"execution_id": ids[0]})])
    assert statuses(row) == ["clarification_required"] and not row["actual_data_changes"]
    row = step(env, "只恢复午餐，晚餐保留。", [task("undo_task", {"execution_id": ids[0]})])
    assert statuses(row) == ["success"]
    assert any(i["name"] == "牛肉" for i in row["after"]["days"][str(DAY)]["meals"]["dinner"]["items"])


def test_single_replacement_is_one_undo_goal_despite_two_resource_refs(env):
    step(env, "第三项替换成拉伸。", [task("replace_action", {"target_index": 3, "replacement_category": "stretching"})])
    row = step(env, "撤销刚才那次替换。", [task("undo_task")])
    assert statuses(row) == ["success"]
    assert "push-up" in row["after"]["days"][str(DAY)]["actions"]


def test_dialogue_fixture_and_oracle_can_run_on_a_later_calendar_date(tmp_path, monkeypatch):
    import datetime as dt
    from app.evals import dialogue_runner
    future = dt.date(2099, 5, 15)
    monkeypatch.setattr(dialogue_runner, "DAY", future)
    model = ScriptedModel()
    runtime = DialogueEnvironment(tmp_path / "future.db", "future-fixture", model)
    try:
        model.outputs.append({"tasks": [task("change_sets", {"target_index": 2, "sets": 2})]})
        row = runtime.step("今天划船两组，别的保持。", 1)
        judge(row, {"assertions": [{"path": "/days/2026-11-11/actions/one-arm-dumbbell-row/sets", "value": 2}],
                    "allowed_changes": ["/days/2026-11-11/actions/one-arm-dumbbell-row/sets"]})
        assert row["passed"] and str(future) in row["after"]["days"]
    finally:
        runtime.close()


def test_saved_trajectory_can_be_rejudged_after_the_host_calendar_changes(env, monkeypatch):
    import datetime as dt
    from app.evals import dialogue_runner
    row = step(env, "划船两组。", [task("change_sets", {"target_index": 2, "sets": 2})])
    monkeypatch.setattr(dialogue_runner, "DAY", dt.date(2099, 1, 1))
    judge(row, {"assertions": [{"path": "/days/2026-11-11/actions/one-arm-dumbbell-row/sets", "value": 2}],
                "allowed_changes": ["/days/2026-11-11/actions/one-arm-dumbbell-row/sets"]})
    assert row["passed"]
