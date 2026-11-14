"""Structured execution memory and compare-before-restore compensation.

This module never infers intent from user text. Only authenticated, verified,
conversation-scoped execution records may be used to explain or undo changes.
"""
from __future__ import annotations

import copy
from sqlalchemy import select
from app.models import CoachTaskExecution, WorkoutSession, new_id


def resource_state(day):
    constraints = {k: copy.deepcopy(v) for k, v in (day.get("constraints") or {}).items() if k != "operations"}
    meal_constraints = constraints.pop("meal_constraints", {})
    constraints.update({"meal_constraints/" + slot: value for slot, value in meal_constraints.items()})
    return {"actions": {a["action_ref"]: copy.deepcopy(a) for a in day.get("actions", [])},
            "meals": {m["slot"]: copy.deepcopy(m) for m in day.get("meals", [])},
            "constraints": constraints,
            "sessions": copy.deepcopy(day.get("_sessions") or {}),
            "action_order": {"order": [a["action_ref"] for a in day.get("actions", [])]}}



def capture_state(c, db, user, day):
    state = c.effective_day(db, user, day, fallback_next=False)
    sessions = list(db.scalars(select(WorkoutSession).where(
        WorkoutSession.user_id == user.id, WorkoutSession.date == day)))
    state["_sessions"] = {r.id: {"items": copy.deepcopy(r.items), "current_index": r.current_index,
                                 "status": r.status} for r in sessions}
    return state

def resource_diff(before, after):
    changes = []
    for kind in ("actions", "meals", "constraints", "sessions", "action_order"):
        for key in sorted(before[kind].keys() | after[kind].keys()):
            old, new = before[kind].get(key), after[kind].get(key)
            if old != new:
                changes.append({"kind": kind, "key": key, "before": old, "after": new,
                                "position_before": list(before[kind]).index(key) if key in before[kind] else None})
    return changes


def executions(db, user, thread, day, limit=24):
    return list(db.scalars(select(CoachTaskExecution).where(
        CoachTaskExecution.user_id == user.id, CoachTaskExecution.thread == thread,
        CoachTaskExecution.effective_date == day,
    ).order_by(CoachTaskExecution.created_at.desc(), CoachTaskExecution.id.desc()).limit(limit)))


def history_context(db, user, thread, day):
    return [{"execution_id": r.id, "request_key": r.request_key, "task_id": r.task_id,
             "goal": r.goal, "operation": r.operation, "scope": r.scope,
             "tool": r.tool, "arguments": r.arguments, "changes": r.resource_changes,
             "status": r.status, "compensated_by": r.compensated_by}
            for r in reversed(executions(db, user, thread, day))]


def record(db, user, day, task, before, after, tool):
    changes = resource_diff(resource_state(before), resource_state(after))
    if not changes:
        return None
    row = CoachTaskExecution(id=new_id(), user_id=user.id, thread=db.info.get("coach_thread", "coach"),
                             request_key=db.info.get("coach_request_key", new_id()),
                             task_id=task["task_id"], effective_date=day, operation=task["operation"],
                             domain=task.get("domain", ""), scope=task["scope"],
                             goal=task.get("user_goal", "")[:500], tool=tool,
                             arguments=copy.deepcopy(task.get("slots") or {}), resource_changes=changes)
    db.add(row)
    db.flush()
    return row



def describe_changes(changes):
    parts = []
    for change in changes:
        old, new = change["before"], change["after"]
        if change["kind"] == "meals":
            def foods(meal):
                return "、".join(f"{i.get('name')} {i.get('grams')}克" for i in (meal or {}).get("items", [])) or "无"
            parts.append(f"{change['key']}：{foods(old)} → {foods(new)}")
        elif change["kind"] == "actions":
            def action(value):
                return (f"{value.get('name') or value.get('action_ref')} {value.get('sets')}组" if value else "移除")
            parts.append(f"{action(old)} → {action(new)}")
        elif change["kind"] == "sessions":
            parts.append("当前训练动作/进度已调整")
    return "；".join(parts) or "无业务对象变化"

def inspect_or_undo(c, db, user, day, task):
    from app.agent.task_executor import TaskFailure
    thread = db.info.get("coach_thread", "coach")
    rows = executions(db, user, thread, day)
    slots = task.get("slots") or {}
    wanted_id = slots.get("execution_id") or (task.get("context_reference") or {}).get("execution_id")
    meal = slots.get("meal_slot")
    action = slots.get("action_ref")
    legacy_target = slots.get("target_action")
    food = slots.get("food")
    source_operation = slots.get("source_operation")
    # Compatibility for structured outputs that put an operation ID in the old
    # overloaded target_action field. Match capability IDs, never user words.
    from app.agent.intent_router import TASK_OPERATIONS
    known_operations = {op for ops in TASK_OPERATIONS.values() for op in ops}
    if legacy_target in known_operations:
        source_operation = legacy_target
    elif meal and legacy_target and legacy_target != "all":
        food = legacy_target
    elif not action:
        action = legacy_target
    if meal == "all":
        meal = None
    if action == "all":
        action = None
    def has_food(row):
        return not food or any(food in {i.get("name") for state in (x["before"], x["after"])
                                      for i in (state or {}).get("items", [])}
                               for x in row.resource_changes if x["kind"] == "meals")
    parent_by_inverse = {r.compensated_by: r for r in rows if r.compensated_by}
    def operation_matches(row):
        if not source_operation:
            return True
        visited = set()
        while row and row.id not in visited:
            if row.operation == source_operation:
                return True
            visited.add(row.id)
            row = parent_by_inverse.get(row.id)
        return False
    filtered = [r for r in rows if has_food(r) and (not wanted_id or r.id == wanted_id) and operation_matches(r) and (
        not meal or any(x["kind"] == "meals" and x["key"] == meal for x in r.resource_changes)) and (
        not action or r.arguments.get("target_action") == action or
        any(x["kind"] == "actions" and x["key"] == action for x in r.resource_changes))]
    if task["operation"] in {"inspect_changes", "inspect_current_state"}:
        current = capture_state(c, db, user, day)
        view = resource_state(current)
        relevant_actions = {action} if action else {
            x["key"] for r in filtered for x in r.resource_changes if x["kind"] == "actions"}
        if not relevant_actions and task["operation"] == "inspect_current_state" and not meal:
            relevant_actions = set(view["actions"])
        current_lines = []
        for ref in sorted(relevant_actions):
            item = view["actions"].get(ref)
            current_lines.append(f"当前{item.get('name') or ref}为{item.get('sets')}组" if item else f"{ref}当前不在今日训练中")
        relevant_meals = {meal} if meal else {
            x["key"] for r in filtered for x in r.resource_changes if x["kind"] == "meals"}
        for slot in sorted(relevant_meals):
            meal_value = view["meals"].get(slot)
            if meal_value:
                current_lines.append(f"当前{slot}：" + "、".join(f"{i['name']} {i.get('grams')}克" for i in meal_value.get("items", [])))
        live_summary = "；".join(current_lines)
        history_reply = (("已验证的修改记录：" + "；".join(
            f"{r.goal or r.operation}（{'已撤销' if r.status == 'compensated' else '已验证'}）：{describe_changes(r.resource_changes)}" for r in filtered[:8])) if filtered else
            "本会话这一天没有找到对应的数据修改记录，不能确认发生过该修改。")
        return {"reply": (live_summary + "。" if live_summary else "") +
                          (history_reply if task["operation"] == "inspect_changes" else "以上来自最新业务数据。"),
            "task_status": "success", "changes": [],
            "inspection": {"current_state": view, "matched_execution_ids": [r.id for r in filtered[:8]],
                           "facts": [{"execution_id": r.id, "status": r.status, "changes": r.resource_changes}
                                     for r in filtered[:8]]},
            "execution_history": history_context(db, user, thread, day)}
    candidates = [r for r in filtered if r.status == "verified" and not r.compensated_by]
    if not candidates:
        if wanted_id:
            raise TaskFailure("context_reference", "对应记录不存在、已恢复，或不属于本会话这一天。")
        return {"reply": "本会话没有找到对应的未撤销修改，数据保持不变。", "task_status": "success", "changes": []}
    # Without an explicit selector, a multi-task request cannot be silently reduced
    # to its last operation. With a resource selector, latest relevant operation wins.
    if not wanted_id and not meal and not action:
        latest_group = candidates[0].request_key
        if sum(r.request_key == latest_group for r in candidates) != 1:
            raise TaskFailure("context_reference", "上一轮改了多个对象，请明确要恢复哪个动作或餐次。")
    row = candidates[0]
    substantive = [x for x in row.resource_changes if x["kind"] not in {"constraints", "action_order"}]
    if meal and any(x["kind"] != "meals" or x["key"] != meal for x in substantive):
        raise TaskFailure("scope", "该记录同时修改了多个对象，不能按单餐授权整次撤销；请明确恢复方案。")
    if action and any(x["kind"] != "actions" or x["key"] != action for x in substantive):
        raise TaskFailure("scope", "该记录包含其他对象的修改，请指定完整执行记录并核对恢复范围。")
    touched = {(x["kind"], x["key"]) for x in row.resource_changes}
    newer = db.scalars(select(CoachTaskExecution).where(
        CoachTaskExecution.user_id == user.id, CoachTaskExecution.effective_date == day,
        CoachTaskExecution.id != row.id, CoachTaskExecution.created_at >= row.created_at)).all()
    if any(touched & {(x["kind"], x["key"]) for x in event.resource_changes} for event in newer):
        raise TaskFailure("conflict", "目标存在后续修改记录，即使当前值相同也不能撤销这条旧修改。")
    current = capture_state(c, db, user, day)
    state = resource_state(current)
    for change in row.resource_changes:
        if state[change["kind"]].get(change["key"]) != change["after"]:
            raise TaskFailure("conflict", "目标在这次修改后又发生了变化，无法安全恢复；请先核对最新状态。")
    for change in row.resource_changes:
        bucket, key = state[change["kind"]], change["key"]
        if change["before"] is None:
            bucket.pop(key, None)
        else:
            bucket[key] = change["before"]
            if change["kind"] == "actions" and change["after"] is None:
                pairs = list(bucket.items())
                restored = pairs.pop()
                pairs.insert(min(change["position_before"], len(pairs)), restored)
                state["actions"] = dict(pairs)
    if any(x["kind"] == "sessions" for x in row.resource_changes):
        for change in row.resource_changes:
            if change["kind"] != "sessions":
                continue
            session = db.get(WorkoutSession, change["key"])
            restored = state["sessions"][change["key"]]
            session.items = restored["items"]
            session.current_index = restored["current_index"]
            session.status = restored["status"]
    else:
        restored_constraints = {}
        for key, value in state["constraints"].items():
            if key.startswith("meal_constraints/"):
                restored_constraints.setdefault("meal_constraints", {})[key.split("/", 1)[1]] = value
            else:
                restored_constraints[key] = value
        c.save_override(db, user, day, actions=[state["actions"][ref] for ref in state["action_order"]["order"]],
                    meals=list(state["meals"].values()), constraints=restored_constraints,
                    summary="恢复经过核对的历史修改", reason=task.get("user_goal") or "恢复修改",
                    base_version=current.get("plan_version"), borrowed_from=current.get("borrowed_from"),
                    expected_adjustment_id=current.get("adjustment_id"))
    db.flush()
    db.expire_all()
    after = capture_state(c, db, user, day)
    if resource_state(after) != state:
        raise TaskFailure("verification", "恢复后的数据未通过重读验证。")
    row = db.get(CoachTaskExecution, row.id)
    row.status = "compensated"
    # The orchestrator links this row to the inverse execution before committing.
    inverse_description = describe_changes(resource_diff(resource_state(current), resource_state(after)))
    return {"reply": f"已恢复并核对当前数据：{inverse_description}。其他后续修改保持不变。",
            "task_status": "success", "changes": [inverse_description],
            "day": {k: v for k, v in after.items() if not k.startswith("_")}, "compensates": row.id}
