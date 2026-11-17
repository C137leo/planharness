"""ID-only execution. This module never receives or interprets the user's text."""
import copy
import datetime as dt
import json
from pydantic import ValidationError
from sqlalchemy import select
from app.agent.capabilities import REGISTRY, capability
from app.agent.business_snapshot import business_state
from app.domain.day_plan import effective_day, save_override
from app.domain.timeutil import as_utc, user_today, day_start_utc
from app.models import (CoachTaskExecution, Memory, Profile, WorkoutSession, new_id)


class TaskFailure(ValueError):
    def __init__(self, stage, message, details=None):
        self.stage = stage
        self.details = details or {}
        super().__init__(message)

def safe_error(exc):
    from sqlalchemy.exc import SQLAlchemyError
    return '数据保存失败，本次修改未完成。请稍后重试或联系维护人员核对数据库版本。' if isinstance(exc,SQLAlchemyError) else str(exc)

def allergy_preview_batch(tasks):
    ids=[t.capability_id for t in tasks]
    return (len(ids)==2 and ids.count('profile.add_allergies')==1 and
        any(i in {'plan.create_initial_draft','plan.create_replacement_draft','plan.revise_initial_draft','plan.revise_replacement_draft'} for i in ids))

# 验证任务
def validate_tasks(tasks, snapshot):
    objects = {o["id"]: o for o in snapshot.payload["objects"]}
    task_ids = [t.task_id for t in tasks]
    if len(set(task_ids)) != len(task_ids):
        raise TaskFailure("schema", "任务 ID 重复。")
    if len(tasks)>1 and any(t.capability_id.startswith(('day_plan.', 'plan.')) for t in tasks) and not allergy_preview_batch(tasks):
        raise TaskFailure('execution_boundary','整日草案每轮只执行一个生命周期任务，先展示差异再单独确认提交。')
    submitted={t.resolution.selected_id for t in tasks if t.capability_id=="commerce.submit_order"}
    edited={t.resolution.selected_id for t in tasks if t.capability_id in {"commerce.revise_order","commerce.cancel_checkout"}}
    if submitted & edited:
        raise TaskFailure("confirmation_scope", "同一订单草案的修订/取消不能与旧版本提交放在一个执行计划中；请先更新预览再确认。")
    seen = set()
    for task in tasks:
        cap = capability(task.capability_id)
        if cap is None:
            raise TaskFailure("capability", "不支持该能力。")
        if cap.execution_boundary in {"commerce","workout_lifecycle","day_revision","initial_plan"} and cap.executor == cap.execution_boundary:
            pass
        elif cap.execution_boundary != "local_database" or cap.executor not in {"action", "food", "profile", "memory", "undo", "inspect", "meal_record"}:
            raise TaskFailure("capability", "该能力尚未接入对应的执行边界，不能在本地事务中执行。")
        if task.parameters_resolved:
            try:cap.argument_model.model_validate(task.arguments)
            except ValidationError as exc:
                issues=exc.errors(include_url=False,include_context=False)
                message='工具参数不符合已注册Schema。'
                if cap.executor == 'meal_record':
                    message = '实际进食的餐次、描述或营养数值尚未明确，请补充或核对后再记录。' if task.capability_id == 'nutrition.record_meal' else '请明确需要更正的进食字段及合法数值。'
                if cap.execution_boundary=='day_revision':
                    fields={part for issue in issues for part in issue['loc'] if isinstance(part,str)}
                    message='新方案的动作、组数或食材份量尚未通过校验。'
                    if 'ingredient_key' in fields:message='所选食材不在当前可用食材目录，请重新选择。'
                    elif 'action_ref' in fields:message='所选动作不在可用目录，或尚未绑定当天的真实动作。'
                raise TaskFailure('arguments_schema',message,{'task_id':task.task_id,'capability_id':cap.capability_id,'errors':issues}) from exc
        for override in task.arguments.get('item_overrides',[]):
            item=objects.get(override.get('item_id'),{})
            if item.get('type')!='session_item' or item.get('parent_id')!=task.resolution.selected_id:
                raise TaskFailure('ownership_scope','完成摘要条目不属于目标训练会话。')
        r = task.resolution
        ids = set(r.candidate_ids + ([r.selected_id] if r.selected_id else []))
        for id in ids:
            if id not in objects:
                raise TaskFailure("ownership_scope", f"任务 {task.task_id} 的目标不在当前用户快照中。", {"task_id": task.task_id, "target_id": id})
            if objects[id]["type"] not in cap.target_types:
                actual = objects[id]["type"]
                raise TaskFailure("target_type", f"任务 {task.task_id}（{cap.capability_id}）绑定类型错误：实际为 {actual}，需要 {'/'.join(cap.target_types)}。",
                                  {"task_id": task.task_id, "target_id": id, "actual_type": actual, "expected_types": list(cap.target_types)})
        if r.status == "resolved" and (not r.selected_id or (r.candidate_ids and set(r.candidate_ids) != {r.selected_id})):
            raise TaskFailure("resolution", "唯一绑定与候选列表不一致。", {"task_id": task.task_id, "selected_id": r.selected_id, "candidate_ids": r.candidate_ids})
        if task.capability_id == 'history.undo' and r.selected_id and objects[r.selected_id].get('undo_supported') is False:
            raise TaskFailure('capability', '这类记录不支持自动撤销；备注可取消，训练事实请修订实际记录。')
        if task.capability_id == 'memory.archive' and r.selected_id:
            from app.domain.memory import CANONICAL_CATEGORIES
            target = objects[r.selected_id]
            if (target.get('structured') or {}).get('canonical') or target.get('category') in CANONICAL_CATEGORIES:
                raise TaskFailure('canonical_profile', '这是长期档案的镜像，请先预览修改或清空对应档案字段，不能单独归档镜像。')
        if r.status == "ambiguous" and len(set(r.candidate_ids)) < 2:
            raise TaskFailure("resolution", "多候选状态需要多个真实候选。")
        if task.capability_id in {'day_plan.create_revision','day_plan.update_revision','day_plan.extend_revision'} and task.parameters_resolved and r.status=='resolved' and snapshot.payload.get('day_revision'):
            # All draft edits use the exact same pure preflight as execution.
            from app.domain import day_revision
            from app.agent.revision_requirements import candidate
            try:candidate(task,snapshot)
            except day_revision.RevisionError as exc:
                raise TaskFailure('arguments_schema',str(exc),{'task_id':task.task_id,
                    'capability_id':cap.capability_id,'errors':[{'type':exc.code,'message':str(exc),'details':exc.details}]}) from exc
        for key, kind in (("ingredient_id", "ingredient"), ("exercise_id", "exercise")):
            if key in task.arguments and (task.arguments[key] not in objects or objects[task.arguments[key]]["type"] != kind):
                raise TaskFailure("ownership_scope", "目录 ID 不存在或类型错误。")
        if not set(task.dependencies) <= seen:
            raise TaskFailure("dependency", "依赖不存在或顺序错误。")
        seen.add(task.task_id)
    return objects


def requires_confirmation(task, objects):
    cap = capability(task.capability_id)
    r = task.resolution
    target = objects.get(r.selected_id, {})
    if cap.execution_boundary in {"commerce","day_revision","initial_plan"} and cap.confirmation == "never":
        return cap.mutates_state and (not task.parameters_resolved or r.status != "resolved")
    return cap.mutates_state and (cap.confirmation == "always" or task.needs_confirmation
           or not task.parameters_resolved or r.status != "resolved" or r.basis in {"inferred", "none"}
           or (target.get("slot") == "warmup" and task.capability_id in {"workout.remove_action", "workout.skip_action"}))


def _get(items, id):
    matches = [item for item in items if item["id"] == id]
    if len(matches) != 1:
        raise TaskFailure("stale", "目标不存在或已变化。")
    return matches[0]


def _save_day(db, user, date, expected, current):
    payload = save_override(db, user, dt.date.fromisoformat(date), actions=current["actions"], meals=current["meals"],
                           constraints=current["constraints"], summary="教练结构化任务调整", reason="validated_bound_task",
                           base_version=current["plan_version"], expected_adjustment_id=current["adjustment_id"])
    expected["days"][date] = copy.deepcopy(current)
    expected["days"][date]["adjustment_id"] = payload["adjustment_id"]
    for row in db.scalars(select(WorkoutSession).where(WorkoutSession.user_id==user.id,WorkoutSession.date==dt.date.fromisoformat(date))):
        expected['sessions'][row.id]={'date':date,'status':row.status,'items':copy.deepcopy(row.items)}


def _exercise(objects, id):
    ex = objects[id]
    from app.domain.catalog import display_action_name
    name = display_action_name({**ex, "name": ex.get("name_zh") or ex.get("name_en")})
    return {"action_ref": ex["action_ref"], "name": name,
            "equipment": ex.get("equipment"), "primary_muscle": ex.get("primary_muscle"),
            "meta": {"knee_risky": ex.get("knee_risky", False), "name_zh": name}}


def _action(db, user, task, target, objects, expected):
    op, args = task.capability_id, task.arguments
    session = target["type"] == "session_item"
    date = target["date"]
    current = copy.deepcopy(expected["days"][date])
    row = db.get(WorkoutSession, target["parent_id"]) if session else None
    if session and (row is None or row.user_id != user.id or row.status not in {"active", "paused"}):
        raise TaskFailure("scope", "训练会话不可修改。")
    actions = copy.deepcopy(row.items) if session else current["actions"]
    item = None if op == "workout.add_action" else _get(actions, target["id"])
    if session and item["status"] in {"done", "skipped"}:
        raise TaskFailure("constraint", "已完成的训练条目不可修改。")
    if op == "workout.remove_action":
        actions.remove(item)
    elif op == "workout.skip_action":
        if session:
            item["status"] = "skipped"
        else:
            actions.remove(item)
    elif op in {"workout.replace_action", "workout.add_action"}:
        if session and int(item.get("completed_sets") or 0) > 0:
            raise TaskFailure("constraint", "已经完成组数的动作不能替换。")
        exercise = _exercise(objects, args["exercise_id"])
        from app.domain.day_plan import allowed_equipment, _norm_equipment
        from app.domain.safety_rules import is_risky_for
        profile = db.get(Profile, user.id)
        if _norm_equipment(exercise.get("equipment")) not in allowed_equipment(profile, current["constraints"]):
            raise TaskFailure("constraint", "当前器械约束不允许该动作。")
        injured = current["constraints"].get("injured_regions") or []
        from app.domain.planning_preferences import blocked,active
        if blocked(exercise['action_ref'],active(current['constraints'].get('body_preferences',[]),date)):
            raise TaskFailure('arguments_schema','替换动作仍使用用户明确避开的部位。')
        if any(is_risky_for(exercise["action_ref"], region) for region in injured):
            raise TaskFailure("constraint", "该动作与身体限制冲突。")
        if item is None:
            item = {"id": new_id(), "slot": "strength", "sets": args.get("sets") or 3,
                    "reps": args.get("reps") or 10, "duration_min": None}
            actions.append(item)
        item.update(exercise)
    if op in {"workout.update_action", "workout.replace_action"}:
        changed = op == "workout.replace_action"
        for key, upper in (("sets", 10), ("reps", 100)):
            field = "target_" + key if session else key
            value, delta = args.get(key), args.get(key + "_delta")
            if value is not None and delta is not None:
                raise TaskFailure("parameters", "绝对值和相对值不能同时指定。")
            if delta is not None:
                if type(item.get(field)) is not int:
                    raise TaskFailure("parameters", "动作没有可调整的数量。")
                value = item[field] + delta
            if value is not None:
                if not 1 <= value <= upper or (session and key == "sets" and value < int(item.get("completed_sets") or 0)):
                    raise TaskFailure("constraint", "目标数量越界或低于已完成数量。")
                item[field] = value
                changed = True
        if not changed:
            raise TaskFailure("parameters", "没有提供可执行的数量调整。")
    if session:
        row.items = actions
        from app.domain.workout import _advance
        _advance(row)
        expected["sessions"][row.id].update(items=copy.deepcopy(row.items), status=row.status)
    else:
        current["actions"] = actions
        _save_day(db, user, date, expected, current)


def _recalc(meal):
    for field, source in (("kcal", "kcal"), ("protein_g", "protein"), ("fat_g", "fat"), ("carb_g", "carb")):
        meal[field] = round(sum(float(i.get(source) or 0) for i in meal["items"]), 1)


def _food(db, user, task, target, objects, expected):
    date, args, op = target["date"], task.arguments, task.capability_id
    current = copy.deepcopy(expected["days"][date])
    meal = _get(current["meals"], target["id"] if target["type"] == "meal" else target["parent_id"])
    item = None if op == "meal.add_food" else _get(meal["items"], target["id"])
    if op == "meal.remove_food":
        meal["items"].remove(item)
    elif op == "meal.set_quantity":
        grams = float(item.get("grams") or 0)
        if grams <= 0:
            raise TaskFailure("constraint", "缺少原始份量，无法可靠重算营养。")
        ratio = args["grams"] / grams
        for field in ("kcal", "protein", "fat", "carb"):
            item[field] = round(float(item.get(field) or 0) * ratio, 1)
        item["grams"] = args["grams"]
    else:
        ingredient = objects[args["ingredient_id"]]
        profile = db.get(Profile, user.id)
        forbidden = (profile.allergies or []) + (current["constraints"].get("avoid_foods") or [])
        from app.domain.meal_safety import forbidden as conflicts
        if conflicts(ingredient['name'],profile) or any(term and term in ingredient['name'] for term in forbidden):
            raise TaskFailure("constraint", "食材与过敏或忌口约束冲突。")
        if item is None:
            item = {"id": new_id()}
            meal["items"].append(item)
        values = ingredient["macros_per_100g"]
        item.update(name=ingredient["name"], ingredient_id=ingredient["id"], ingredient_key=ingredient["name"], grams=args["grams"])
        item.update({field: round(value * args["grams"] / 100, 1) for field, value in zip(("kcal", "protein", "fat", "carb"), values)})
    if op in {"meal.add_food", "meal.replace_food"}:
        from app.domain.meal_items import merge_food_items
        meal["items"] = merge_food_items(meal["items"], target=item)
    from app.domain.meal_safety import check_meals
    check_meals([meal],db.get(Profile,user.id))
    _recalc(meal)
    _save_day(db, user, date, expected, current)


def resource_changes(before, after):
    changes = []
    for date in before["days"]:
        for kind in ("actions", "meals"):
            old, new = before["days"][date][kind], after["days"][date][kind]
            a, b = {i["id"]: i for i in old}, {i["id"]: i for i in new}
            for id in a.keys() | b.keys():
                if a.get(id) != b.get(id):
                    changes.append({"kind": kind, "date": date, "key": id, "before": a.get(id), "after": b.get(id),
                                    "before_index": next((n for n, v in enumerate(old) if v["id"] == id), None)})
    for kind in ("profile", "memories", "sessions"):
        a, b = before[kind], after[kind]
        if kind == "profile":
            if a != b:
                changes.append({"kind": kind, "key": "profile", "before": a, "after": b})
        else:
            for id in a.keys() | b.keys():
                if a.get(id) != b.get(id):
                    changes.append({"kind": kind, "key": id, "before": a.get(id), "after": b.get(id)})
    for date in before['facts']:
        a = {r['id']: r for r in before['facts'][date]['meal_logs']}
        b = {r['id']: r for r in after['facts'][date]['meal_logs']}
        for ident in a.keys() | b.keys():
            if a.get(ident) != b.get(ident):
                changes.append({'kind': 'meal_logs', 'date': date, 'key': ident,
                                'before': a.get(ident), 'after': b.get(ident)})
    return changes


def apply_profile_fields(db, user, args, expected):
    from app.domain.profile_sync import normalize_profile_updates, sync_profile_change
    args = normalize_profile_updates(args)
    row = db.get(Profile, user.id)
    for key, value in args.items():
        setattr(row, key, value)
    expected['profile'].update(args)
    if 'allergies' in args:
        from app.domain.meal_safety import repair_saved_future_meals
        repair_saved_future_meals(db,user,row)
        expected['days'] = business_state(db,user,list(expected['days']))['days']
    db.flush()
    sync_profile_change(db, user, row, list(args))
    db.flush()
    categories = {'goal': 'goal', 'session_minutes': 'session_minutes', 'weekly_days': 'weekly_days',
        'preferred_time_window': 'schedule', 'preferred_time_label': 'schedule', 'preferred_location': 'location', 'likes': 'like', 'dislikes': 'dislike'}
    categories.update(training_experience='experience',equipment='equipment')
    allowed = {categories[k] for k in args if k in categories}
    for memory in db.scalars(select(Memory).where(Memory.user_id == user.id, Memory.category.in_(allowed))):
        expected['memories'][memory.id] = {'category': memory.category, 'value': memory.value,
            'status': memory.status, 'structured': memory.structured, 'kind': memory.kind,
            'expires_at': as_utc(memory.expires_at).isoformat() if memory.expires_at else None}


def _undo(db, user, thread, target, expected):
    row = db.get(CoachTaskExecution, target["id"])
    if row is None or row.user_id != user.id or row.thread != thread or row.status != "verified":
        raise TaskFailure("scope", "该执行记录不可撤销。")
    if row.domain=='workout_lifecycle':
        raise TaskFailure('constraint','已发生的训练事实不能撤销成未发生；请使用训练记录修订能力。')
    if row.operation=='profile.add_allergies' or 'allergies' in (row.arguments or {}):
        raise TaskFailure('constraint','过敏限制不能通过通用撤销删除，请明确核对长期过敏档案。')
    if row.domain=='initial_plan':
        raise TaskFailure('constraint', '首份计划不能用局部补偿撤销；请通过新的完整计划草案调整。')
    if row.domain=='day_revision':
        raise TaskFailure('constraint','整日草案请取消待应用草案，或创建新的补偿草案；不能绕过完成事实保护直接撤销。')
    later = db.scalars(select(CoachTaskExecution).where(CoachTaskExecution.user_id == user.id,
                       CoachTaskExecution.created_at > row.created_at))
    keys = {(c.get("date"), c["kind"], c["key"]) for c in row.resource_changes}
    if any(keys & {(c.get("date"), c["kind"], c["key"]) for c in other.resource_changes} for other in later):
        raise TaskFailure("conflict", "目标已有后续修改，需重新确认恢复内容。")
    dirty_dates = set()
    for change in row.resource_changes:
        kind, key, old, new = (change[k] for k in ("kind", "key", "before", "after"))
        if kind == 'profile':
            if expected['profile'] != new:
                raise TaskFailure('conflict', '档案已变化，请重新核对恢复字段。')
            apply_profile_fields(db, user, {field: value for field, value in old.items() if value != new.get(field)}, expected)
            continue
        if kind == 'memories' and row.operation == 'profile.update':
            # Canonical mirror changes are restored by the profile domain service.
            continue
        if kind not in {"actions", "meals"}:
            raise TaskFailure("capability", "此类型记录暂不支持自动补偿。")
        date = change["date"]
        if date not in expected["days"]:
            raise TaskFailure("scope", "撤销日期未加载，请先加载原操作日期。")
        items = expected["days"][date][kind]
        actual = next((i for i in items if i["id"] == key), None)
        if actual != new:
            raise TaskFailure("conflict", "目标当前数据与历史操作后状态不一致。")
        if actual is not None:
            items.remove(actual)
        if old is not None:
            items.insert(min(change["before_index"], len(items)), copy.deepcopy(old))
        dirty_dates.add(date)
    for date in dirty_dates:
        _save_day(db, user, date, expected, copy.deepcopy(expected["days"][date]))
    row.status = "compensated"
    return row


def execute_tasks(db, user, snapshot, tasks, *, confirmed=False):
    if snapshot.user_id != user.id:
        raise TaskFailure("ownership_scope", "快照不属于当前用户。")
    if any(capability(t.capability_id).execution_boundary != "local_database" for t in tasks):
        raise TaskFailure("execution_boundary", "外部能力必须使用独立商务执行边界。")
    objects = validate_tasks(tasks, snapshot)
    results, successful = [], set()
    for task in tasks:
        base = {"task_id": task.task_id, "capability_id": task.capability_id, "operation": task.capability_id,
                "user_goal": task.user_goal, "tool": REGISTRY[task.capability_id].executor,
                "tool_arguments": {"target_id": task.resolution.selected_id, **task.arguments},
                "state_history": ["bound", "validated"]}
        if not set(task.dependencies) <= successful:
            results.append({**base, "status": "blocked", "message": "前置任务未成功。"})
            continue
        try:
            if not task.parameters_resolved:
                raise TaskFailure("parameters", "操作或参数仍未明确，不能执行。")
            if not task.resolution.selected_id or task.resolution.status not in {"resolved", "candidate"}:
                raise TaskFailure("resolution", "仍需明确具体目标。")
            if requires_confirmation(task, objects) and not confirmed:
                raise TaskFailure("confirmation", "此任务尚未确认。")
            with db.begin_nested():
                before = business_state(db, user, snapshot.payload["dates"])
                expected = copy.deepcopy(before)
                target, cap = objects[task.resolution.selected_id], REGISTRY[task.capability_id]
                undone = None
                if cap.executor == "action":
                    _action(db, user, task, target, objects, expected)
                elif cap.executor == "food":
                    _food(db, user, task, target, objects, expected)
                elif cap.executor == "profile":
                    args = cap.argument_model.model_validate(task.arguments).model_dump(exclude_none=True)
                    if not args:
                        raise TaskFailure("parameters", "没有指定档案更新内容。")
                    if task.capability_id=='profile.add_allergies':
                        args['allergies']=list(dict.fromkeys([*(db.get(Profile,user.id).allergies or []),*args['allergies']]))
                    apply_profile_fields(db, user, args, expected)
                elif cap.executor == 'meal_record':
                    from app.models import MealLog
                    args = cap.argument_model.model_validate(task.arguments).model_dump(exclude_none=True)
                    if 'title' in args:
                        args['title'] = args['title'].strip()
                        if not args['title']:
                            raise TaskFailure('parameters', '进食描述不能为空。')
                    date = target['date']
                    rows = expected['facts'][date]['meal_logs']
                    if task.capability_id == 'nutrition.record_meal':
                        row = MealLog(id=new_id(), user_id=user.id, date=dt.date.fromisoformat(date),
                                      source='agent', **args)
                        db.add(row)
                    else:
                        if not args:
                            raise TaskFailure('parameters', '没有指定需要更正的进食内容。')
                        row = db.get(MealLog, target['id'])
                        if row is None or row.user_id != user.id or row.date.isoformat() != date:
                            raise TaskFailure('ownership_scope', '进食记录不存在或不属于当前日期。')
                        for key, value in args.items():
                            setattr(row, key, value)
                        rows[:] = [r for r in rows if r['id'] != row.id]
                    db.flush()
                    rows.append({key: getattr(row, key) for key in ('id', 'slot', 'title', 'kcal', 'protein_g', 'note')})
                    rows.sort(key=lambda r: r['id'])
                elif cap.executor == "memory":
                    if task.capability_id == "memory.archive":
                        row = db.get(Memory, target["id"])
                        if row is None or row.user_id != user.id:
                            raise TaskFailure("scope", "记忆不存在。")
                        row.status = "archived"
                        expected["memories"][row.id]["status"] = "archived"
                    else:
                        args = cap.argument_model.model_validate(task.arguments)
                        row = Memory(id=new_id(), user_id=user.id, kind=args.kind, category=args.category,
                                     key=new_id(), value=args.value, structured={}, evidence=task.user_goal,
                                     expires_at=None if args.kind == 'long_term' else day_start_utc(user_today(db.get(Profile,user.id)) + dt.timedelta(days=args.retention_days), db.get(Profile,user.id)))
                        db.add(row)
                        expected["memories"][row.id] = {"category": row.category, "value": row.value, "status": "active", "structured": {},
                            "kind": row.kind, "expires_at": as_utc(row.expires_at).isoformat() if row.expires_at else None}
                elif cap.executor == "undo":
                    undone = _undo(db, user, snapshot.thread, target, expected)
                db.flush()
                db.expire_all()
                after = business_state(db, user, snapshot.payload["dates"])
                if after != expected:
                    raise TaskFailure("verification", "数据库重读发现目标未达成或有超出范围的修改，已回滚本任务。")
                changes = resource_changes(before, after)
                execution_id = None
                if cap.mutates_state:
                    journal = CoachTaskExecution(user_id=user.id, thread=snapshot.thread,
                        request_key=db.info.get("coach_request_key") or new_id(), task_id=task.task_id,
                        effective_date=dt.date.fromisoformat(target.get("date") or snapshot.payload["dates"][0]),
                        operation=task.capability_id, domain=cap.domain, scope=target["type"], goal=task.user_goal,
                        tool=cap.executor, arguments=base["tool_arguments"], resource_changes=changes, status="verified")
                    db.add(journal)
                    db.flush()
                    execution_id = journal.id
                    if undone:
                        undone.compensated_by = execution_id
                message = ("已保存修改。" if cap.mutates_state else "已读取记录。")

                if cap.executor in {'food', 'action'}:
                    from app.domain.catalog import display_action_name
                    saved_day = after['days'][target['date']]
                    if cap.executor == 'food':
                        mid = target['id'] if target['type'] == 'meal' else target['parent_id']
                        meal = next(m for m in saved_day['meals'] if m['id'] == mid)
                        message += '\n当前餐食：' + '、'.join(f"{i['name']} {i.get('grams', 0):g}克" for i in meal['items'])
                    else:
                        message += '\n当前训练：' + '；'.join(display_action_name(a) + f" {a.get('sets') or 1}组" for a in saved_day['actions'])
                if cap.executor == 'profile':
                    labels = {'session_minutes': '每次训练时长', 'weekly_days': '每周训练天数', 'weight_kg': '档案体重',
                        'goal': '长期目标', 'allergies': '过敏限制', 'likes': '喜欢', 'dislikes': '不喜欢',
                        'diet_preferences': '饮食限制', 'preferred_time_window': '训练时段', 'preferred_location': '训练地点'}
                    details = []
                    for change in changes:
                        if change['kind'] != 'profile': continue
                        for field, value in change['after'].items():
                            if value != change['before'].get(field):
                                details.append(labels.get(field, '档案项') + '：' + str(change['before'].get(field)) + ' → ' + str(value))
                    message = ('已加入长期过敏限制，并核对长期食谱及后续未完成餐次；已发生进食记录保留。' if 'allergies' in task.arguments else '已保存档案修改。') + ('\n' + '；'.join(details) if details else '当前值无需重复修改。')
                elif cap.executor == 'meal_record':
                    message = ('已登记实际进食：' if task.capability_id == 'nutrition.record_meal' else '已更正实际进食：') + row.title
                    message += f'；{row.kcal} kcal，蛋白质 {row.protein_g:g} g。计划餐食未改变。'
                elif cap.executor == 'memory':
                    message = ('已归档备注：' if task.capability_id == 'memory.archive' else '已保存' + ('长期偏好：' if row.kind == 'long_term' else '近期备注：')) + row.value
                elif cap.executor == 'undo':
                    message = '已恢复上次修改，并重读核对档案或计划；实际训练记录未撤销。'
                if not cap.mutates_state:
                    if target['type']=='day':
                        progress={i['action_id']:i for i in target.get('current_progress',{}).get('items',[])}
                        message+='\n'+target['date']+' 当前有效安排'+('（第'+str(target['plan_version'])+'版）' if target.get('plan_version') else '：尚未建立计划')
                        p=target.get('current_progress',{})
                        message+='\n系统已记录 '+str(p.get('done_sets',0))+'/'+str(p.get('total_sets',0))+' 组。'
                        if not p.get('total_sets',0):message+='尚无训练目标，不能据此判断完成。'
                        elif p.get('done_sets',0)<p.get('total_sets',0):message+='当前训练目标尚未全部完成。'
                        else:message+='已有记录已达到当前目标；若与实际不符，可先核对记录。'
                        facts=target.get('recorded_facts',{})
                        for log in facts.get('workout_logs',[]):
                            message+='\n运动记录：'+str(log['action_ref'])+'，'+str(log.get('duration_min') or 0)+'分钟'
                        for checkin in facts.get('checkins',[]):
                            message+='\n当日打卡：体重'+str(checkin.get('weight_kg'))+'kg，睡眠'+str(checkin.get('sleep_hours'))+'小时'
                        for meal in facts.get('meal_logs',[]):
                            message+='\n已吃记录：'+str(meal['title'])
                        if not facts.get('meal_logs'):message+='\n当日尚无已保存的进食记录。'
                        for action in target.get('current_actions',[]):
                            p=progress.get(action['id'],{})
                            message+='\n'+str(action.get('name') or action['action_ref'])+'：'+str(action.get('sets') or 1)+' 组'
                            if action.get('duration_min'):message+='，约 '+str(action['duration_min'])+' 分钟'
                            message+='；已记录 '+str(p.get('completed_sets',0))+' 组，还差 '+str(p.get('remaining_sets',action.get('sets') or 1))+' 组'
                        if target.get('previous_training'):
                            records=target['previous_training']
                            message+='\n重排前记录单独保留，不计入当前新方案进度：'+ '；'.join(i['name']+' '+str(i['completed_sets'])+'组' for i in records)+'；合计 '+str(sum(i['completed_sets'] for i in records))+' 组。'
                        message+='\n你希望继续当前剩余安排，还是保留记录并重新制定目标？重排前会先确认是否放弃旧方案未完成部分。'
                    elif target['type'] == 'profile':
                        message += '\n档案体重：'+str(target.get('weight_kg'))+'kg；每周'+str(target.get('weekly_days'))+'天，每次'+str(target.get('session_minutes'))+'分钟。'
                        if target.get('goal_note'):message+='\n已保存目标原话：'+target['goal_note']
                        message += '\n训练时段：'+str(target.get('preferred_time_window') or '未填写')
                        message += '\n器械：'+ '、'.join(target.get('equipment') or [])
                    elif target['type'] == 'memory':
                        message += '\n'+str(target.get('value',''))
                    else:message += '\n'+str(target.get('name') or target.get('title') or '已核对目标对象；具体字段见记录详情。')
                results.append({**base, "status": "success", "message": message, "execution_id": execution_id,
                                "actual_changes": changes, "unrelated_changes": [],
                                "data": target if not cap.mutates_state else None,
                                "state_history": base["state_history"] + ["executed", "reloaded", "verified"]})
                successful.add(task.task_id)
        except Exception as exc:
            results.append({**base, "status": "failed", "stage": getattr(exc, "stage", "execution"),
                            "message": safe_error(exc)[:300], "actual_changes": [], "unrelated_changes": []})
    return results
