"""Deterministic task orchestration; model text is never proof of a mutation."""
from __future__ import annotations

import copy
import logging
import time

logger = logging.getLogger(__name__)
TRAINING = {"skip_action", "remove_action", "replace_action", "add_action", "change_sets", "change_reps"}
MEALS = {"modify_meal_plan", "replace_meal_item", "remove_meal_item"}


from app.agent.task_executor import TaskFailure


def _bind(task, actions):
    slots = task.setdefault("slots", {})
    if task["operation"] == "add_action":
        return None
    visible = [a for a in actions if a.get("slot") != "warmup"]
    index = slots.get("target_index")
    if isinstance(index, int) and not isinstance(index, bool) and index != 0:
        index = len(visible) if index == -1 else index
        candidates = visible[index-1:index] if 1 <= index <= len(visible) else []
    else:
        ref = (task.get("context_reference") or {}).get("action_ref")
        hint = slots.get("target_action") or slots.get("exercise")
        candidates = [a for a in visible if (ref and a.get("action_ref") == ref)
                      or (hint and hint in (a.get("name"), a.get("action_ref")))]
    if len(candidates) != 1:
        raise TaskFailure("context_reference", "请明确要调整的动作编号或名称。")
    target = copy.deepcopy(candidates[0])
    # Bind once against the request snapshot, never renumber after a removal.
    slots.pop("target_index", None)
    slots["target_action"] = target.get("action_ref")
    return target


def _verify_training(task, before, after, target):
    op = task["operation"]
    if op in {"skip_action", "remove_action"}:
        return not any(a.get("action_ref") == target.get("action_ref") and
                       a.get("status") != "skipped" for a in after)
    if op == "add_action":
        return len(after) == len(before) + 1
    if op in {"change_sets", "change_reps"}:
        key = "sets" if op == "change_sets" else "reps"
        return any(a.get("action_ref") == target.get("action_ref") and
                   a.get(key, a.get("target_" + key)) == task["slots"][key] for a in after)
    if op == "replace_action":
        replacements = [a for a in after if a.get("replaced_from") == target.get("action_ref")]
        slots = task["slots"]
        if slots.get("replacement_category") == "stretching":
            return any(a.get("action_ref") == "full_body_stretch" for a in replacements)
        if slots.get("replacement_exercise"):
            from app.agent.legacy_coach_agent import _pick_action_by_name
            picked = _pick_action_by_name(slots["replacement_exercise"])
            return bool(picked) and any(a.get("action_ref") == picked["ref"] for a in replacements)
        return any(a.get("action_ref") != target.get("action_ref") for a in replacements)
    return False


def _training(c, db, user, day, task, target, active, trace):
    slots = task["slots"]
    op = task["operation"]
    # Identity is bound to the request snapshot; mutable values are reloaded for
    # each dependent task, so relative updates use the state they actually follow.
    if target is not None:
        if active is not None and task["scope"] == "current_workout":
            db.refresh(active)
            fresh = active.items or []
        else:
            fresh = c.effective_day(db, user, day, fallback_next=False)["actions"]
        matches = [a for a in fresh if a.get("action_ref") == target["action_ref"]]
        if len(matches) != 1:
            raise TaskFailure("context_reference", "目标动作已变化或不唯一，请重新指定。")
        target = matches[0]
    for key in ("sets", "reps"):
        delta = slots.get(key + "_delta")
        if delta is not None:
            if type(delta) is not int or target is None:
                raise TaskFailure("parameters", "相对调整需要明确的目标和整数变化量。")
            value = target.get("target_" + key, target.get(key))
            if type(value) is not int:
                raise TaskFailure("parameters", "目标动作没有可调整的组数或次数。")
            slots[key] = value + delta
    if active is not None and task["scope"] == "current_workout":
        db.refresh(active)
        items = copy.deepcopy(active.items or [])
        matches = [i for i, a in enumerate(items) if target and a.get("action_ref") == target.get("action_ref")]
        if len(matches) != 1 or items[matches[0]].get("status") in {"done", "skipped"}:
            raise TaskFailure("scope", "目标动作已完成或无法唯一定位，未修改训练。")
        i = matches[0]
        if op in {"skip_action", "remove_action"}:
            items[i]["status"] = "skipped"
        elif op == "replace_action":
            if slots.get("replacement_category") != "stretching":
                raise TaskFailure("capability", "当前训练的指定替换需要明确拉伸类别。")
            items[i].update(action_ref="full_body_stretch", name="全身拉伸", slot="mobility",
                            replaced_from=target["action_ref"], target_sets=1, target_reps=None,
                            duration_min=5, completed_sets=0)
        elif op in {"change_sets", "change_reps"}:
            key = "sets" if op == "change_sets" else "reps"
            value = slots.get(key)
            if type(value) is not int or not 1 <= value <= (10 if key == "sets" else 100):
                raise TaskFailure("parameters", "请提供合理的目标组数或次数。")
            if key == "sets" and value < int(items[i].get("completed_sets") or 0):
                raise TaskFailure("scope", "不能删除已经完成的组数。")
            items[i]["target_" + key] = value
        else:
            raise TaskFailure("capability", "当前训练暂不支持此操作。")
        active.items = items
        c.workout_service._advance(active)
        db.flush()
        db.refresh(active)
        after = active.items or []
        if not _verify_training(task, [], after, target):
            raise TaskFailure("verification", "当前训练重新读取验证失败。")
        return {"reply": "当前训练动作已调整。", "changes": ["当前训练动作已调整"],
                "session": c.workout_service.session_payload(active)}
    before = c.effective_day(db, user, day, fallback_next=False)
    if op in {"change_sets", "change_reps"}:
        key = "sets" if op == "change_sets" else "reps"
        value = slots.get(key)
        if type(value) is not int or not 1 <= value <= (10 if key == "sets" else 100):
            raise TaskFailure("parameters", "请提供合理的目标组数或次数。")
        actions = copy.deepcopy(before["actions"])
        matches = [a for a in actions if a.get("action_ref") == target["action_ref"]]
        if len(matches) != 1:
            raise TaskFailure("context_reference", "目标动作已经变化，请重新指定。")
        matches[0][key] = value
        c.save_override(db, user, day, actions=actions, meals=before.get("meals"),
                        summary="调整动作数量", constraints=before.get("constraints") or {},
                        reason=task.get("user_goal") or "", base_version=before.get("plan_version"), expected_adjustment_id=before.get("adjustment_id"))
        one = {"reply": "动作数量已保存。", "changes": ["动作数量已保存"]}
    else:
        one = c._apply_today_operations(db, user, day, "", [task], trace)
        statuses = one.get("task_results") or []
        if not statuses or statuses[0]["status"] != "success":
            raise TaskFailure("parameters", statuses[0]["message"] if statuses else "没有执行操作。")
    db.flush()
    db.expire_all()
    after = c.effective_day(db, user, day, fallback_next=False)
    if not _verify_training(task, before["actions"], after["actions"], target):
        raise TaskFailure("verification", "训练数据重新读取后未满足目标。")
    one["day"] = after
    return one



def _proposal_reply(task):
    """Render prepared advice; a proposal is useful output without a write."""
    details = task.get("proposal_details") or {}
    summary = details.get("summary") or (task.get("slots") or {}).get("note") or task.get("user_goal") or "请明确需要的方案。"
    lines = ["以下仅为建议，尚未保存：" + str(summary)]
    for key, title in (("benefits", "好处"), ("tradeoffs", "取舍"), ("assumptions", "前提")):
        if details.get(key):
            lines.append(title + "：" + "；".join(str(x) for x in details[key]))
    return "\n".join(lines)

def execute_tasks(db, user, day, text, tasks, trace, active, client, recent, plan):
    from app.agent import legacy_coach_agent as c
    from app.agent.intent_router import TASK_OPERATIONS
    from app.agent import conversation_state as conversation
    from app.models import CoachTaskExecution

    tasks = copy.deepcopy(tasks)
    snapshot = c.effective_day(db, user, day, fallback_next=False)
    bindings, binding_errors = {}, {}
    previous = conversation.executions(db, user, db.info.get("coach_thread", "coach"), day)
    recent_action_edits = [r for r in previous if any(x["kind"] == "actions" for x in r.resource_changes)]
    ambiguous_antecedents = set()
    if recent_action_edits:
        group = recent_action_edits[0].request_key
        ambiguous_antecedents = {x["key"] for r in recent_action_edits if r.request_key == group
                                 for x in r.resource_changes if x["kind"] == "actions"}
    explicit_names = [a.get("name") or a.get("action_ref") for a in snapshot["actions"]]
    grounded_names = [name for name in explicit_names if name and name.casefold() in text.casefold()]
    ambiguous_reference = len(ambiguous_antecedents) > 1 and not grounded_names and not c._ordinal_indexes(text)
    for task in tasks:
        if task["operation"] in TRAINING:
            try:
                if ambiguous_reference and task["operation"] != "add_action":
                    raise TaskFailure("context_reference", "上一轮修改了多个动作，请明确动作名称或编号。")
                source = active.items if active is not None and task["scope"] == "current_workout" else snapshot["actions"]
                bindings[task["task_id"]] = _bind(task, source)
            except TaskFailure as exc:
                binding_errors[task["task_id"]] = exc
    result = c._base_result(c._decision_from_task(tasks[0], text, active, plan), trace)
    outcomes = []
    for task in tasks:
        started = time.monotonic()
        tid, op = task["task_id"], task["operation"]
        history = ["pending", "ready", "running"]
        one = {}
        tool = "none"
        before_task = conversation.capture_state(c, db, user, day)
        try:
            if not set(task.get("dependencies") or []) <= {r["task_id"] for r in outcomes if r["status"] == "success"}:
                raise TaskFailure("dependencies", "依赖任务未完成。")
            mode = task.get("execution_mode", "execute")
            if task.get("clarification_needed") or (mode == "execute" and task.get("missing_slots")):
                raise TaskFailure("parameters", "需要补充：" + "、".join(task.get("missing_slots") or ["目标和范围"]))
            if mode != "execute":
                task["execution_mode"] = mode
                # Proposals are descriptions, never dispatched to mutation handlers.
                one = {"reply": _proposal_reply(task) if mode == "propose" else "已保持当前数据，未执行修改。",
                       "task_status": "confirmation_required" if mode == "propose" else
                                      "success" if op in {"acknowledge", "cancel_tasks", "inspect_changes"} else "cancelled",
                       "changes": []}
                tool = "proposal_only"
            elif task["intent"] == "dialogue":
                if op in {"inspect_changes", "inspect_current_state", "undo_task"}:
                    if op == "undo_task":
                        selected_id = (task.get("slots") or {}).get("execution_id") or (task.get("context_reference") or {}).get("execution_id")
                        selected = next((r for r in previous if r.id == selected_id), None)
                        if selected and selected.id not in text:
                            group = [r for r in previous if r.request_key == selected.request_key]
                            subjects = {(x["kind"], x["key"]) for r in group for x in r.resource_changes
                                        if x["kind"] in {"actions", "meals", "sessions"}}
                            names = {x["key"] for x in selected.resource_changes if x["kind"] in {"actions", "meals"}}
                            for change in selected.resource_changes:
                                for value in (change["before"], change["after"]):
                                    if isinstance(value, dict):
                                        names.update(n for n in (value.get("name"), value.get("title")) if n)
                            grounded = any(str(name).casefold() in text.casefold() for name in names) or bool(c._ordinal_indexes(text))
                            if len(group) > 1 and len(subjects) > 1 and not grounded:
                                raise TaskFailure("context_reference", "上一轮修改了多个对象，模型引用的记录ID不能代替你的选择；请明确恢复哪个动作或餐次。")
                    tool = "conversation_state.inspect_or_undo"
                    one = conversation.inspect_or_undo(c, db, user, day, task)
                elif op in {"cancel_tasks", "acknowledge"}:
                    one = {"reply": "本轮没有执行数据修改；已取消未执行的方案。", "changes": [], "task_status": "success"}
                elif op == "clarify":
                    raise TaskFailure("parameters", task.get("slots", {}).get("question") or "你想调整训练还是饮食？请明确对象和范围。")
                else:
                    raise TaskFailure("capability", "无法执行该对话操作。")
            else:
                one = None
            if one is not None:
                pass
            else:
                one = _execute_business_task(c, db, user, day, text, task, bindings, binding_errors,
                                             active, trace, client, recent, plan, outcomes)
                tool = one.pop("_selected_tool", op)
            if one.get("error") or one.get("task_status") in {"failed", "unsupported", "clarification_required"}:
                raise TaskFailure("execution", one.get("reply") or "业务执行失败。")
            history.append("verifying")
            status = one.get("task_status") or "success"
            after_task = conversation.capture_state(c, db, user, day)
            journal = None
            verified = mode == "execute" and (op in TRAINING | MEALS | {"add_meal_item", "regenerate_training_today", "undo_task"})
            if status == "success" and verified:
                journal = conversation.record(db, user, day, task, before_task, after_task, tool)
                if one.get("compensates") and journal:
                    source = db.get(CoachTaskExecution, one["compensates"])
                    source.compensated_by = journal.id
            try:
                db.commit()
            except Exception as exc:
                raise TaskFailure("persistence", "数据库提交失败，此任务未确认完成。") from exc
            verification = {"passed": True if verified else None, "method": "state_reload" if verified else tool}
            row = {"task_id": tid, "status": status, "message": one.get("reply") or "已处理。",
                   "changes": one.get("changes") or [], "verification": verification,
                   "execution_id": journal.id if journal else None,
                   "selected_tool": tool, "tool_arguments": copy.deepcopy(task.get("slots") or {}),
                   "inspection": one.get("inspection")}
        except Exception as exc:
            db.rollback()
            stage = exc.stage if isinstance(exc, TaskFailure) else "execution"
            status = {"capability": "unsupported", "dependencies": "blocked", "parameters": "clarification_required",
                      "context_reference": "clarification_required", "scope": "clarification_required"}.get(stage, "failed")
            message = str(exc) if isinstance(exc, TaskFailure) else "业务执行或持久化失败，未确认完成。"
            row = {"task_id": tid, "status": status, "message": message, "changes": [],
                   "failure_stage": stage, "error": type(exc).__name__, "verification": {"passed": False},
                   "selected_tool": tool, "tool_arguments": copy.deepcopy(task.get("slots") or {})}
            one = {}
        history.append(row["status"])
        row["state_history"] = history
        row["latency_ms"] = round((time.monotonic() - started) * 1000)
        outcomes.append(row)
        c._push(trace, "task_execution", row["status"], task_id=tid, scope=task["scope"],
                operation=op, selected_tool=row["selected_tool"], tool_arguments=row["tool_arguments"],
                failure_stage=row.get("failure_stage"), verification=row["verification"],
                latency_ms=row["latency_ms"])
        logger.info("coach_task task_id=%s operation=%s status=%s stage=%s latency_ms=%s",
                    tid, op, row["status"], row.get("failure_stage"), row["latency_ms"])
        for key in ("day", "session", "plan_draft", "patch_request", "today_brief", "guidance", "mcd_order"):
            if one.get(key):
                result[key] = one[key]
        for key in ("changes", "actions", "warnings", "memory_saved"):
            result[key] += list(one.get(key) or [])
    successes = sum(r["status"] in {"success", "cancelled"} for r in outcomes)
    result["overall_status"] = "succeeded" if successes == len(outcomes) else "partial_success" if successes else "failed"
    result["task_results"] = outcomes
    result["task_plan"] = tasks
    result["execution_mode"] = "execute" if all(t.get("execution_mode", "execute") == "execute" for t in tasks) else "propose"
    result["execution_history"] = conversation.history_context(db, user, db.info.get("coach_thread", "coach"), day)
    result["reply"] = "\n".join(f"{i}. {'已处理' if r['status'] == 'success' else '已取消' if r['status'] == 'cancelled' else '未完成'}：{r['message']}" for i, r in enumerate(outcomes, 1))
    return result


def _execute_business_task(c, db, user, day, text, task, bindings, binding_errors,
                           active, trace, client, recent, plan, outcomes):
    from app.agent.intent_router import TASK_OPERATIONS
    from app.agent.scoped_mutations import add_meal_item, regenerate_training
    tid, op = task["task_id"], task["operation"]
    requested_date = (task.get("slots") or {}).get("date")
    if requested_date not in (None, "today", day.isoformat()):
        raise TaskFailure("scope", "请求的日期与当前计划日期不同，请明确目标日期后再修改。")
    if task["intent"] in {"create_plan", "adjust_plan"} and task["scope"] != "long_term":
        raise TaskFailure("scope", "长期计划工具不能用于今日含糊重生成，请明确训练或饮食。")
    # Business dispatch is independent of natural language and dialogue control.

    if not set(task.get("dependencies") or []) <= {r["task_id"] for r in outcomes if r["status"] == "success"}:
        raise TaskFailure("dependencies", "依赖任务未完成。")
    if task.get("clarification_needed") or task.get("missing_slots"):
        raise TaskFailure("parameters", "需要补充：" + "、".join(task.get("missing_slots") or ["目标和范围"]))
    if tid in binding_errors:
        raise binding_errors[tid]
    if op not in TASK_OPERATIONS.get(task["intent"], set()):
        raise TaskFailure("capability", "暂不支持这个业务操作，未执行修改。")
    if task["intent"] in {"adjust_today", "adjust_workout"} and task["scope"] not in {"today", "current_workout"}:
        raise TaskFailure("scope", "训练修改范围无法可靠执行，请明确今天或当前训练。")
    if op in {"reorder_action", "change_duration", "change_location"}:
        raise TaskFailure("capability", "此结构化操作尚无经过验证的执行器，请明确调整方式。")
    if op == "add_meal_item":
        if task["scope"] not in {"today", "meal"}:
            raise TaskFailure("scope", "新增餐食仅支持今天的明确餐次。")
        one = add_meal_item(c, db, user, day, task)
    elif op == "regenerate_training_today":
        if task["scope"] != "today" or active is not None:
            raise TaskFailure("scope", "训练重排仅适用于今天尚未执行的训练。")
        one = regenerate_training(c, db, user, day, task)
    elif op in TRAINING:
        if task["scope"] not in {"today", "current_workout"}:
            raise TaskFailure("scope", "动作修改仅支持今天或当前训练，请明确范围。")
        one = _training(c, db, user, day, task, bindings.get(tid), active, trace)
    else:
        if op in {"rest_today", "easier", "harder"}:
            task["slots"]["rest" if op == "rest_today" else op] = True
        decision = c._decision_from_task(task, task.get("user_goal") or text, active, plan)
        if op in MEALS:
            if task["scope"] not in {"today", "meal"}:
                raise TaskFailure("scope", "餐单修改仅支持今天，请明确范围。")
            meal_before = c.effective_day(db, user, day, fallback_next=False)
            slots = task["slots"]
            if slots.get("meal_slot") not in {"breakfast", "lunch", "dinner", "snack", "all"}:
                raise TaskFailure("scope", "请明确要修改的餐次。")
            target_food = slots.get("to_food") or slots.get("replacement_food")
            if target_food and any(x in target_food for x in slots.get("avoid_foods", [])):
                raise TaskFailure("parameters", "替换食物与忌口约束冲突。")
            profile = db.get(c.Profile, user.id)
            allergies = list(profile.allergies or []) if profile else []
            if target_food and any(a and a in target_food for a in allergies):
                raise TaskFailure("parameters", "替换食物与档案过敏约束冲突。")
            one = c._handle_nutrition_operation(db, user, day, text, decision, trace, client)
        else:
            one = c._dispatch_decision(db, user, day, task.get("user_goal") or text,
                                       decision, trace, active, client, recent)
    if op in MEALS:
        db.flush()
        db.expire_all()
        reloaded = c.effective_day(db, user, day, fallback_next=False)
        slots = task["slots"]
        selected = [m for m in reloaded["meals"] if slots["meal_slot"] == "all" or m["slot"] == slots["meal_slot"]]
        avoid = list(slots.get("avoid_foods") or []) + ([slots["from_food"]] if slots.get("from_food") else [])
        names = [c._food_name(i) for m in selected for i in m.get("items", [])]
        target_food = slots.get("to_food") or slots.get("replacement_food")
        if not selected or any(a in n for a in avoid for n in names) or (target_food and target_food not in names):
            raise TaskFailure("verification", "餐单重新读取后未满足目标约束。")
        untouched_before = [m for m in meal_before["meals"] if slots["meal_slot"] != "all" and m["slot"] != slots["meal_slot"]]
        untouched_after = [m for m in reloaded["meals"] if slots["meal_slot"] != "all" and m["slot"] != slots["meal_slot"]]
        if untouched_before != untouched_after or meal_before["actions"] != reloaded["actions"]:
            raise TaskFailure("verification", "修改超出了目标餐次范围。")
        one["day"] = reloaded
    if op not in TRAINING | MEALS | {"add_meal_item", "regenerate_training_today"}:
        one["task_status"] = one.get("task_status") or (
            "confirmation_required" if decision.needs_confirmation or one.get("plan_draft") or one.get("patch_request") or one.get("mcd_order")
            else "blocked" if one.get("blocked") else "success")
    one["_selected_tool"] = ("scoped_mutations." + op if op in {"add_meal_item", "regenerate_training_today"} else
                             "workout.session_items" if op in TRAINING and task["scope"] == "current_workout" else
                             "day_plan.save_override" if op in TRAINING | MEALS else "coach_agent._dispatch_decision")
    return one
