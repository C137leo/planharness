"""训练执行：把"今天练什么"变成"现在练到哪一组"。

数据分工：

- `WorkoutSession.items` 保存**执行过程**：当前动作、第几组、哪些被替换、哪些跳过；
- `WorkoutLog` 保存**练完的事实**：一条一个动作，热量只从这里算；
- `Memory` 保存**训练后的状态**：最近难度、连续未完成次数，供下一次计划使用。

一条硬规则：任何调整（换动作 / 缩短时间 / 太累）都不能清空已完成记录。
所有修改都只碰"当前及之后"的 items。
"""

from __future__ import annotations

import datetime as dt
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import append_event
from app.domain import calories as calorie_stats
from app.domain.day_plan import (
    allowed_equipment,
    effective_day,
    pick_alternative,
    _action_equipment,
    _action_mode,
    _action_muscle,
)
from app.domain.memory import (
    complete_temporary_for_day,
    recent_training_state,
    set_temporary,
    upsert_memory,
    SHORT_TERM,
)
from app.domain.safety_rules import is_risky_for
from app.models import Profile, User, WorkoutLog, WorkoutSession, new_id

ACTIVE_STATUSES = ("active", "paused")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def active_session(db: Session, user: User, day: date) -> WorkoutSession | None:
    return db.scalar(
        select(WorkoutSession)
        .where(
            WorkoutSession.user_id == user.id,
            WorkoutSession.date == day,
            WorkoutSession.status.in_(ACTIVE_STATUSES),
        )
        .order_by(WorkoutSession.started_at.desc())
    )


def latest_session(db: Session, user: User, day: date) -> WorkoutSession | None:
    return db.scalar(
        select(WorkoutSession)
        .where(WorkoutSession.user_id == user.id, WorkoutSession.date == day)
        .order_by(WorkoutSession.started_at.desc())
    )


def _item_name(action: dict) -> str:
    from app.domain.catalog import display_action_name
    return display_action_name(action)


def _target_sets(action: dict) -> int:
    slot = str(action.get("slot") or "")
    if slot in ("warmup", "mobility"):
        return 1
    return max(1, int(action.get("sets") or 3))


def _item_from_action(action: dict, index: int) -> dict:
    ref = str(action.get("action_ref") or "unknown")
    return {
        "id": new_id(),
        "source_action_id": action.get('id'),
        "workout_cycle_id": action.get('workout_cycle_id'),
        "source_plan": {k: action.get(k) for k in ('action_ref','sets','reps','duration_min','slot','name','equipment')},
        "position": index,
        "slot_key": action.get("slot_key") or f"session:{index}",
        "action_ref": ref,
        "name": _item_name(action),
        "slot": action.get("slot") or "strength",
        "target_sets": _target_sets(action),
        "target_reps": action.get("reps"),
        "duration_min": int(action.get("duration_min") or 0)
        or (5 if action.get("slot") in ("warmup", "mobility") else 8),
        "rest_sec": 20 if action.get("slot") in ("warmup", "mobility") else (
            30 if action.get("slot") == "cardio" else 60
        ),
        "completed_sets": 0,
        "status": "pending",
        "replaced_from": action.get("original_ref"),
        "reason": action.get("reason") or "",
        "equipment": _action_equipment(ref, action.get("meta")),
        "movement_mode": _action_mode(ref, action.get("meta")),
        "primary_muscle": _action_muscle(ref, action.get("meta")),
        "guidance": "",
        "source": action.get("source") or "plan",
        "meta": action.get("meta") or {},
    }


def start_session(
    db: Session,
    user: User,
    day: date,
    *,
    source: str = "agent",
    fallback_next: bool = True,
) -> WorkoutSession:
    """开始（或恢复）今天的训练。已有 active 会话时直接返回，避免重置进度。"""
    from app.domain.workout_records import lock
    lock(db,user)
    existing = active_session(db, user, day) or db.scalar(select(WorkoutSession).where(
        WorkoutSession.user_id==user.id,WorkoutSession.date==day,WorkoutSession.status=='completed').order_by(WorkoutSession.started_at.desc()))
    if existing is not None:
        # 计划升版后，旧会话的动作/时长已经过时：作废并重新开一场，
        # 不能把用户送回上一版的训练里（评审：版本切换一致性）。
        day_plan = effective_day(db, user, day, fallback_next=fallback_next)
        current_version = day_plan.get("plan_version")
        from app.domain.workout_replan import session_cycle
        if session_cycle(existing)!=day_plan.get('workout_cycle_id'):
            existing.status='superseded'
            db.flush()
        elif (existing.plan_version is not None and current_version is not None
                and int(existing.plan_version) != int(current_version)):
            existing.status = "abandoned"
            existing.note = (
                (existing.note or "")
                + f"（计划已更新到 v{current_version}，本次训练会话作废）"
            )[:1000]
            db.flush()
        else:
            sync_session_plan(existing,day_plan['actions'])
            db.commit()
            db.refresh(existing)
            return existing

    day_plan = effective_day(db, user, day, fallback_next=fallback_next)
    actions = day_plan.get("actions") or []
    if not actions:
        raise ValueError("今天没有可执行的训练动作，先让教练生成计划")

    prior=latest_session(db,user,day)
    from app.domain.workout_replan import session_cycle
    continuation=None
    remaining=None
    if prior and prior.status in {'finished','abandoned'} and session_cycle(prior)==day_plan.get('workout_cycle_id'):
        prior_progress=progress_for_day(prior,actions)
        if prior_progress['total_sets'] and prior_progress['done_sets']==prior_progress['total_sets']:
            raise ValueError('当前目标已有完整记录；如需新的训练目标，请先确认重排并预览新方案。')
        continuation=prior
        remaining={i['action_id']:i for i in prior_progress['items']}
    items=[]
    for action in actions:
        if remaining is not None:
            facts=remaining[action['id']]
            if facts['remaining_sets']==0:continue
            from app.domain.day_plan import _estimated_minutes
            full=_item_from_action(action,len(items))
            entry=_item_from_action({**action,'sets':facts['remaining_sets'],
                'duration_min':max(1,round(_estimated_minutes(action)*facts['remaining_sets']/facts['target_sets']))},len(items))
            entry['source_plan']=full['source_plan']
            entry['carried_completed_sets']=facts['completed_sets']
            entry['carried_from_session_id']=prior.id
            items.append(entry)
        else:items.append(_item_from_action(action,len(items)))
    session = WorkoutSession(
        id=new_id(),
        user_id=user.id,
        date=day,
        plan_id=day_plan.get("plan_id"),
        plan_version=day_plan.get("plan_version"),
        status="active",
        source=source,
        current_index=0,
        items=items,
        planned_minutes=sum(int(item["duration_min"]) for item in items),
        agent_log=[{'kind':'continuation','session_id':continuation.id,'workout_cycle_id':day_plan.get('workout_cycle_id'),
                    'credits':prior_progress['items']}] if continuation else [],
    )
    db.add(session)
    db.flush()
    append_event(
        db, f"user:{user.id}", "workout_started",
        {"session_id": session.id, "date": day.isoformat(),
         "actions": len(items), "planned_minutes": session.planned_minutes},
        actor_type="user", actor_id=user.id,
    )
    db.commit()
    db.refresh(session)
    return session


def sync_session_plan(session: WorkoutSession, actions: list[dict]) -> bool:
    """Synchronize dated targets; completed facts and stable session item IDs survive."""
    import copy
    if session.status not in ('active','paused','completed'):return False
    old=copy.deepcopy(session.items or []);used=set();items=[]
    for position,action in enumerate(actions):
        matches=[i for i in old if i['id'] not in used and i.get('source_action_id') and i['source_action_id'] in {action.get('id'),action.get('source_object_id')}]
        if not matches:
            # Compatibility for sessions predating source_action_id. Never guess
            # between repeated occurrences of the same catalog action.
            pool=[i for i in old if i['id'] not in used and not i.get('source_action_id') and i.get('action_ref')==action.get('action_ref')]
            if len(pool)==1 and sum(a.get('action_ref')==action.get('action_ref') for a in actions)==1:matches=pool
        if matches:
            item=copy.deepcopy(matches[0]);used.add(item['id'])
            if item.get('status') in ('done','skipped'):
                items.append(item);continue
            if int(item.get('completed_sets') or 0)>0 and item.get('action_ref')!=action.get('action_ref'):
                raise ValueError('不能用计划替换覆盖已记录组数的训练条目。')
            updated=_item_from_action(action,position)
            if item.get('source_plan')==updated['source_plan']:
                items.append(item);continue
            if item.get('carried_completed_sets'):
                updated['target_sets']=max(0,updated['target_sets']-int(item['carried_completed_sets']))
                if updated['target_sets']==0 and not item.get('completed_sets'):continue
            if int(updated['target_sets'])<int(item.get('completed_sets') or 0):
                raise ValueError('新目标组数不能低于已记录的实际组数。')
            for key,value in updated.items():
                if key not in {'id','completed_sets','status','reason','guidance'}:item[key]=value
        else:
            item=_item_from_action(action,position)
        item['position']=len(items);items.append(item)
    # Removing a planned action must not delete its already recorded work.
    for item in old:
        if item['id'] not in used and (int(item.get('completed_sets') or 0)>0 or item.get('status') in ('done','skipped')):
            item['outside_current_plan']=True;items.append(item)
    if items==old:return False
    paused=session.status=='paused'
    if session.status=='completed':session.status='active'
    session.items=items;session.planned_minutes=sum(int(i.get('duration_min') or 0) for i in items if not i.get('outside_current_plan'))
    _advance(session)
    if paused and session.status!='completed':session.status='paused'
    return True


def sync_day_sessions(db: Session,user: User,day: date,actions: list[dict]) -> list[WorkoutSession]:
    changed=[]
    for session in db.scalars(select(WorkoutSession).where(WorkoutSession.user_id==user.id,WorkoutSession.date==day,WorkoutSession.status.in_(('active','paused','completed')))):
        from app.domain.workout_replan import active_cycle,session_cycle
        if session_cycle(session)!=active_cycle(db,user,day):continue
        if sync_session_plan(session,actions):changed.append(session)
    db.flush()
    return changed


def _current_index(session: WorkoutSession) -> int | None:
    for index, item in enumerate(session.items or []):
        if item.get("status") in ("pending", "active"):
            return index
    return None


def _progress(session: WorkoutSession) -> dict:
    items = session.items or []
    total_sets = sum(max(1, int(item.get("target_sets") or 1)) for item in items)
    done_sets = sum(int(item.get("completed_sets") or 0) for item in items)
    done_actions = sum(1 for item in items if int(item.get('completed_sets') or 0)>=max(1,int(item.get('target_sets') or 1)))
    skipped = sum(1 for item in items if item.get("status") == "skipped")
    remaining_minutes = 0
    for item in items:
        if item.get("status") == "skipped" or int(item.get('completed_sets') or 0)>=max(1,int(item.get('target_sets') or 1)):
            continue
        target = max(1, int(item.get("target_sets") or 1))
        left = max(0, target - int(item.get("completed_sets") or 0)) / target
        remaining_minutes += round(int(item.get("duration_min") or 0) * left)
    return {
        "done_actions": done_actions,
        "total_actions": len(items),
        "done_sets": done_sets,
        "total_sets": total_sets,
        "skipped_actions": skipped,
        "percent": round(done_sets / total_sets * 100) if total_sets else 0,
        "remaining_minutes": max(0, remaining_minutes),
    }


def progress_for_day(session: WorkoutSession | None, actions: list[dict]) -> dict:
    """Read-only progress against the displayed dated plan, including legacy finishes.

    Historical session targets/logs remain facts. Credit only matching actions;
    extra work is reported separately rather than inflating the denominator.
    """
    items = (session.items or []) if session else []
    item_progress = []
    used = set()
    total = done = done_actions = skipped = remaining = 0
    absent_credit=0
    credits={(event.get('workout_cycle_id'),i['action_id']):i for event in ((session.agent_log or []) if session else []) if event.get('kind')=='continuation' for i in event.get('credits',[])}
    for action in actions:
        target = max(1, int(action.get('sets') or 1))
        total += target
        refs = {action.get('id'), action.get('source_object_id')} - {None, ''}
        eligible=[i for i in items if i.get('workout_cycle_id')==action.get('workout_cycle_id')]
        candidates = [i for i in eligible if i['id'] not in used
                      and i.get('action_ref') == action.get('action_ref')
                      and i.get('source_action_id') in refs]
        if not candidates:
            pool = [i for i in eligible if i['id'] not in used and i.get('action_ref') == action.get('action_ref')]
            if len(pool) == 1 and sum(a.get('action_ref') == action.get('action_ref') for a in actions) == 1:
                candidates = pool
        item = candidates[0] if len(candidates) == 1 else None
        carried=max(0,int(item.get('carried_completed_sets') or 0)) if item else 0
        extra=credits.get((action.get('workout_cycle_id'),action.get('id')), {})
        credit=int(extra.get('completed_sets') or 0) if item is None else 0
        absent_credit+=credit
        actual=(max(0,int(item.get('completed_sets') or 0))+carried) if item else credit
        count = min(target, actual)
        if item:
            used.add(item['id'])
            skipped += int(item.get('status') == 'skipped')
        item_progress.append({'action_id':action.get('id'),'action_ref':action.get('action_ref'),
            'target_sets':target,'completed_sets':count,'remaining_sets':target-count,
            'actual_sets':actual,
            'session_item_id':item['id'] if item else None,'recorded':item is not None or bool(credit)})
        done += count
        done_actions += int(count == target)
        from app.domain.day_plan import _estimated_minutes
        remaining += _estimated_minutes(action) * (target - count) / target
    actual = sum(max(0, int(i.get('completed_sets') or 0))+max(0,int(i.get('carried_completed_sets') or 0)) for i in items)
    actual+=absent_credit
    return {'scope': 'effective_day', 'done_sets': done, 'total_sets': total,
            'done_actions': done_actions, 'total_actions': len(actions), 'skipped_actions': skipped,
            'percent': round(done / total * 100) if total else 0,
            'remaining_minutes': max(0, round(remaining)),
            'actual_done_sets': actual, 'additional_recorded_sets': max(0, actual - done), 'items':item_progress}


def session_payload(session: WorkoutSession, *, include_items: bool = True) -> dict:
    from app.domain.catalog import display_action_name
    from app.domain.workout_records import revision
    items = [{**item, "name": display_action_name(item)} for item in (session.items or [])]
    for item in items:
        if item.get('status')=='done' and int(item.get('completed_sets') or 0)<max(1,int(item.get('target_sets') or 1)):
            item['stored_status']=item['status']
            item['status']='partial' if item.get('completed_sets') else 'unrecorded'
    current_index = _current_index(session)
    current = items[current_index] if current_index is not None else None
    elapsed = 0
    if session.started_at:
        started = session.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=dt.timezone.utc)
        end = session.finished_at or _now()
        if end.tzinfo is None:
            end = end.replace(tzinfo=dt.timezone.utc)
        elapsed = max(0, int((end - started).total_seconds() // 60))
    return {
        "session_id": session.id,
        "record_revision": revision(session),
        "date": session.date.isoformat(),
        "status": session.status,
        "source": session.source,
        "plan_version": session.plan_version,
        "current_index": current_index,
        "current_item": current,
        "items": items if include_items else [],
        "planned_minutes": session.planned_minutes,
        "actual_minutes": session.actual_minutes if session.status=="finished" else session.actual_minutes or elapsed,
        "actual_minutes_known": any(x.get('kind')=='fact_finish' and x.get('actual_minutes_known') for x in session.agent_log or []),
        "elapsed_minutes": elapsed,
        "completion_pct": session.completion_pct,
        "difficulty": session.difficulty,
        "fatigue": session.fatigue,
        "note": session.note,
        "progress": _progress(session),
        "started_at": session.started_at.isoformat() if session.started_at else "",
        "finished_at": session.finished_at.isoformat() if session.finished_at else "",
    }


def _session(db: Session, user: User, session_id: str) -> WorkoutSession:
    session = db.get(WorkoutSession, session_id)
    if session is None or session.user_id != user.id:
        raise ValueError("session not found")
    return session


def _editable_session(db: Session, user: User, session_id: str) -> WorkoutSession:
    from app.domain.workout_records import lock
    lock(db,user)
    session=_session(db,user,session_id)
    if session.status not in ACTIVE_STATUSES:
        raise ValueError("训练已经结束或不可编辑，请使用独立的训练记录修订能力")
    return session


def _advance(session: WorkoutSession) -> None:
    # 减组后 completed_sets 可能已经达到新的 target_sets：把它正常收口，别卡在"进行中"
    normalized: list[dict] = []
    for entry in session.items or []:
        item = dict(entry)
        target = max(1, int(item.get("target_sets") or 1))
        if item.get("status") in ("pending", "active") and int(item.get("completed_sets") or 0) >= target:
            item["completed_sets"] = target
            item["status"] = "done"
            item["completed_at"] = _now().isoformat(timespec="seconds")
        normalized.append(item)
    session.items = normalized
    index = _current_index(session)
    session.current_index = index if index is not None else len(session.items or [])
    statuses = {item.get("status") for item in session.items or []}
    if not ({"pending", "active"} & statuses):
        session.status = "completed"
    session.updated_at = _now()


def complete_set(db: Session, user: User, session_id: str, *,
                 reps: int | None = None, weight_kg: float | None = None,
                 rpe: int | None = None) -> dict:
    session = _editable_session(db, user, session_id)
    if session.status == "finished":
        raise ValueError("这组训练已经结束了")
    if session.status == "completed":
        raise ValueError("所有动作都完成了，提交反馈即可")
    index = _current_index(session)
    if index is None:
        raise ValueError("没有待完成的动作")
    items = list(session.items or [])
    item = dict(items[index])
    item["completed_sets"] = int(item.get("completed_sets") or 0) + 1
    # 记录这一组实际做了什么；不填也能完成，但填了才能支撑渐进超负荷。
    set_logs = list(item.get("set_logs") or [])
    set_logs.append({
        "set_no": item["completed_sets"],
        "reps": int(reps) if reps is not None else None,
        "weight_kg": float(weight_kg) if weight_kg is not None else None,
        "rpe": int(rpe) if rpe is not None else None,
    })
    item["set_logs"] = set_logs
    target = max(1, int(item.get("target_sets") or 1))
    if item["completed_sets"] >= target:
        item["completed_sets"] = target
        item["status"] = "done"
        item["completed_at"] = _now().isoformat(timespec="seconds")
    else:
        item["status"] = "active"
    items[index] = item
    session.items = items
    session.status = "active"
    _advance(session)
    db.commit()
    db.refresh(session)
    return session_payload(session)


def skip_action(db: Session, user: User, session_id: str, *, reason: str = "") -> dict:
    session = _editable_session(db, user, session_id)
    index = _current_index(session)
    if index is None:
        raise ValueError("没有可跳过的动作")
    items = list(session.items or [])
    item = dict(items[index])
    item["status"] = "skipped"
    item["reason"] = reason or "用户跳过"
    items[index] = item
    session.items = items
    _advance(session)
    db.commit()
    db.refresh(session)
    return session_payload(session)


def replace_action(
    db: Session,
    user: User,
    session_id: str,
    *,
    action_ref: str = "",
    reason: str = "用户替换动作",
    equipment_busy: bool = False,
    injured_regions: list[str] | None = None,
    easier: bool = False,
    minutes_remaining: int | None = None,
) -> tuple[dict, list[str]]:
    """替换当前动作并重新评估剩余任务；已完成动作原样保留。"""
    session = _editable_session(db, user, session_id)
    items = list(session.items or [])
    index = _current_index(session)
    if index is None:
        raise ValueError("当前没有待执行的动作")

    # 器械被占用时，自动找"当前或之后第一个需要器械"的动作作为替换目标；
    # 热身 / 徒手动作不该替用户改掉。
    if equipment_busy:
        found_equipment = False
        for cursor in range(index, len(items)):
            candidate = items[cursor]
            if candidate.get("status") in ("done", "skipped"):
                continue
            if candidate.get("equipment") not in ("bodyweight", None, ""):
                index = cursor
                found_equipment = True
                break
        if not found_equipment:
            # 当前和后续都是徒手动作：没有被占用的器械，不用为了"响应"硬换一个
            return session_payload(session), ["当前动作不需要器械，继续完成即可"]

    changes: list[str] = []
    item = dict(items[index])
    profile = db.get(Profile, user.id)
    constraints = {
        "equipment_busy": equipment_busy,
        "injured_regions": injured_regions or [],
        "no_gym": False,
        "location": "",
        "unavailable_equipment": [],
    }
    allowed = allowed_equipment(profile, constraints)
    used = {str(entry.get("action_ref")) for entry in items}

    if action_ref:
        from app.domain.exercise_catalog import EXERCISE_CATALOG

        if action_ref not in EXERCISE_CATALOG:
            raise ValueError(f"动作不存在：{action_ref}")
        replacement = {
            "ref": action_ref,
            "name": (EXERCISE_CATALOG.get(action_ref) or {}).get("name_zh") or action_ref,
            "equipment": _action_equipment(action_ref),
            "movement_mode": _action_mode(action_ref),
            "primary_muscle": _action_muscle(action_ref),
        }
    else:
        replacement = pick_alternative(
            item,
            allowed=allowed,
            injured=injured_regions or [],
            used=used,
            prefer_bodyweight=equipment_busy or easier,
        )
    if replacement is None:
        raise ValueError("没找到合适的替代动作，可以跳过这个动作或直接结束训练")

    old_name = item.get("name")
    item["replaced_from"] = item.get("replaced_from") or item.get("action_ref")
    item["action_ref"] = replacement["ref"]
    item["name"] = replacement["name"]
    item["equipment"] = replacement.get("equipment") or "bodyweight"
    item["movement_mode"] = replacement.get("movement_mode") or ""
    item["primary_muscle"] = replacement.get("primary_muscle") or ""
    item["reason"] = reason
    item["source"] = "agent"
    item["status"] = "pending" if int(item.get("completed_sets") or 0) == 0 else "active"
    # 重置过的动作保留已完成组数：只换动作，不扣进度
    item["meta"] = {
        **(item.get("meta") or {}),
        "name_zh": replacement["name"],
        "movement_mode": item["movement_mode"],
        "equipment": item["equipment"],
        "replaced_from": item["replaced_from"],
        "reason": reason,
    }
    if easier:
        current = max(1, int(item.get("target_sets") or 1))
        item["target_sets"] = max(1, current - 1)
        changes.append(f"「{item['name']}」组数从 {current} 降到 {item['target_sets']}")
    items[index] = item
    changes.insert(0, f"「{old_name}」→「{replacement['name']}」（{reason}）")

    # 剩余任务重新评估：时间不够就砍后面的动作；太累就把剩余组数下调。
    remaining = [cursor for cursor in range(index + 1, len(items))
                 if items[cursor].get("status") not in ("done", "skipped")]
    if easier:
        for cursor in remaining:
            entry = dict(items[cursor])
            current = max(1, int(entry.get("target_sets") or 1))
            entry["target_sets"] = max(1, current - 1)
            items[cursor] = entry
        if remaining:
            changes.append(f"后面 {len(remaining)} 个动作也各减 1 组")

    if minutes_remaining is not None:
        budget = max(1, int(minutes_remaining))
        spent = 0
        for cursor in [index] + remaining:
            entry = dict(items[cursor])
            need = int(entry.get("duration_min") or 8)
            left_for_this = budget - spent
            if left_for_this <= 0:
                entry["status"] = "skipped"
                entry["reason"] = "时间不够，自动跳过"
            elif need > left_for_this:
                entry["duration_min"] = max(2, left_for_this)
                entry["target_sets"] = max(
                    1, round(int(entry.get("target_sets") or 1) * left_for_this / need)
                )
                spent = budget
            else:
                spent += need
            items[cursor] = entry
        changes.append(f"按剩余 {budget} 分钟重排了当前和后续动作")

    session.items = items
    _advance(session)
    db.commit()
    db.refresh(session)
    return session_payload(session), changes


def adjust_session(
    db: Session,
    user: User,
    session_id: str,
    *,
    easier: bool = False,
    harder: bool = False,
    minutes_remaining: int | None = None,
    injured_regions: list[str] | None = None,
) -> tuple[dict, list[str]]:
    """只调整"当前 + 剩余"：加减组、压时间、避开不适部位，不换已完成动作。"""
    session = _editable_session(db, user, session_id)
    items = list(session.items or [])
    index = _current_index(session)
    if index is None:
        return session_payload(session), []
    changes: list[str] = []
    injured = injured_regions or []
    profile = db.get(Profile, user.id)
    constraints = {"injured_regions": injured, "no_gym": False, "location": "",
                   "unavailable_equipment": [], "equipment_busy": False}
    allowed = allowed_equipment(profile, constraints)
    used = {str(entry.get("action_ref")) for entry in items}

    for cursor in range(index, len(items)):
        entry = dict(items[cursor])
        if entry.get("status") in ("done", "skipped"):
            continue
        if injured and any(
                is_risky_for(str(entry.get("action_ref") or ""), region)
                for region in injured):
            replacement = pick_alternative(
                entry, allowed=allowed, injured=injured, used=used,
                prefer_bodyweight=True,
            )
            if replacement is not None:
                old_name = entry.get("name")
                entry["replaced_from"] = entry.get("replaced_from") or entry.get("action_ref")
                entry["action_ref"] = replacement["ref"]
                entry["name"] = replacement["name"]
                entry["equipment"] = replacement.get("equipment") or "bodyweight"
                entry["movement_mode"] = replacement.get("movement_mode") or ""
                entry["primary_muscle"] = replacement.get("primary_muscle") or ""
                entry["source"] = "agent"
                entry["reason"] = "避开今天不舒服的部位"
                used.add(replacement["ref"])
                changes.append(f"「{old_name}」→「{replacement['name']}」（避开不适部位）")
        if easier and entry.get("slot") == "strength":
            current = max(1, int(entry.get("target_sets") or 1))
            entry["target_sets"] = max(1, current - 1)
            if entry["target_sets"] != current:
                changes.append(f"「{entry.get('name')}」组数 {current} → {entry['target_sets']}")
        if harder and entry.get("slot") == "strength":
            current = max(1, int(entry.get("target_sets") or 1))
            entry["target_sets"] = min(5, current + 1)
            if entry["target_sets"] != current:
                entry["source"] = "agent"
                entry["reason"] = "按你今天的状态加一组"
                changes.append(f"「{entry.get('name')}」组数 {current} → {entry['target_sets']}")
        items[cursor] = entry

    if minutes_remaining is not None:
        budget = max(1, int(minutes_remaining))
        spent = 0
        for cursor in range(index, len(items)):
            entry = dict(items[cursor])
            if entry.get("status") in ("done", "skipped"):
                continue
            need = max(1, int(entry.get("duration_min") or 8))
            left = budget - spent
            if left <= 0:
                entry["status"] = "skipped"
                entry["reason"] = "时间不够，自动跳过"
            elif need > left:
                entry["duration_min"] = max(2, left)
                entry["target_sets"] = max(
                    1, round(int(entry.get("target_sets") or 1) * left / need)
                )
                spent = budget
            else:
                spent += need
            items[cursor] = entry
        changes.append(f"按剩余 {budget} 分钟重排了当前和后续动作")

    session.items = items
    _advance(session)
    db.commit()
    db.refresh(session)
    return session_payload(session), changes


def update_guidance(
    db: Session,
    user: User,
    session_id: str,
    *,
    guidance: list[str],
    source_ids: list[str] | None = None,
) -> dict:
    """把动作指导写进当前训练任务（不是只回一段文字）。"""
    session = _editable_session(db, user, session_id)
    items = list(session.items or [])
    index = _current_index(session)
    if index is None:
        return session_payload(session)
    item = dict(items[index])
    item["guidance"] = "；".join(guidance)
    item["meta"] = {
        **(item.get("meta") or {}),
        "guidance": guidance,
        "guidance_source_ids": source_ids or [],
    }
    items[index] = item
    session.items = items
    db.commit()
    db.refresh(session)
    return session_payload(session)


def pause_session(db: Session, user: User, session_id: str) -> dict:
    from app.domain.workout_records import lock
    lock(db,user)
    session = _session(db, user, session_id)
    if session.status in ("active", "paused"):
        session.status = "paused" if session.status == "active" else "active"
        session.updated_at = _now()
        db.commit()
        db.refresh(session)
    return session_payload(session)


def _score_completion(items: list[dict]) -> float:
    total = sum(max(1, int(item.get("target_sets") or 1)) for item in items)
    done = sum(int(item.get("completed_sets") or 0) for item in items)
    return round(done / total * 100, 1) if total else 0.0


def finish_session(
    db: Session,
    user: User,
    session_id: str,
    *,
    completed: bool = True,
    completion_pct: int | None = None,
    difficulty: int | None = None,
    fatigue: int | None = None,
    actual_minutes: int | None = None,
    note: str = "",
    completion_basis: str = 'recorded_only',
    expected_revision: str | None = None,
) -> dict:
    """结束训练：写真实训练记录 + 更新短期记忆。

    completed/completion_pct仅作为反馈保留，不生成未记录的组数或计划次数。
    日志和会话在同一个事务内持久化。
    """
    session = _session(db, user, session_id)
    if session.status == "finished":
        return finish_payload(db, user, session)

    # The legacy endpoint now uses the same fact-preserving close operation.
    # A broad "completed" flag is feedback, not per-set completion evidence.
    from app.domain import workout_records
    from app.agent.workout_capabilities import FinishSession
    workout_records.finish(db,user,session_id,FinishSession(
        expected_revision=expected_revision or workout_records.revision(session),actual_minutes=actual_minutes,
        completion_basis=completion_basis,
        difficulty=difficulty,fatigue=fatigue,note=note))
    session = _session(db,user,session_id)
    session.agent_log=[*(session.agent_log or []),{'kind':'legacy_finish_feedback','completed':completed,'self_reported_pct':completion_pct}]
    logs=list(db.scalars(select(WorkoutLog).where(WorkoutLog.user_id==user.id,WorkoutLog.session_id==session_id)))
    upsert_memory(
        db, user,
        kind=SHORT_TERM,
        category="session",
        key=f"session:{session.id}",
        value=(
            f"{session.date.isoformat()} 完成 {session.completion_pct:.0f}% · "
            f"难度 {str(session.difficulty)+'/5' if session.difficulty is not None else '未记录'} · 疲劳 {str(session.fatigue)+'/5' if session.fatigue is not None else '未记录'}"
        ),
        structured={
            "session_id": session.id,
            "completion_pct": session.completion_pct,
            "difficulty": session.difficulty,
            "fatigue": session.fatigue,
            "actual_minutes": session.actual_minutes,
        },
        source="feedback",
        audit=False,
    )
    append_event(
        db, f"user:{user.id}", "workout_finished",
        {"session_id": session.id, "date": session.date.isoformat(),
         "completion_pct": session.completion_pct,
         "difficulty": session.difficulty, "fatigue": session.fatigue,
         "logs": len(logs)},
        actor_type="user", actor_id=user.id,
    )
    db.flush()
    # 用户主动结束当天训练：当天环境上下文标 completed（保留原始记录，不删除）
    complete_temporary_for_day(db, user, session.date)
    # 重新汇总连续未完成状态（刚 finish 的这条也算进去）
    recent_training_state(db, user)
    db.commit()
    db.refresh(session)
    return finish_payload(db, user, session, logs=logs)


def _write_workout_logs(db: Session, user: User, session: WorkoutSession) -> list[WorkoutLog]:
    from app.domain.workout_records import sync_item
    for item in session.items or []:
        sync_item(db,user,session,item)
    db.flush()
    return list(db.scalars(select(WorkoutLog).where(WorkoutLog.user_id==user.id,WorkoutLog.session_id==session.id)))


def _weight_for(db: Session, user: User) -> tuple[float, str]:
    profile = db.scalar(select(Profile).where(Profile.user_id == user.id))
    if profile is not None and profile.weight_kg:
        return float(profile.weight_kg), "profile"
    return 70.0, "default"


def finish_payload(
    db: Session, user: User, session: WorkoutSession, logs: list[WorkoutLog] | None = None
) -> dict:
    weight, source = _weight_for(db, user)
    if logs is None:
        logs = list(db.scalars(
            select(WorkoutLog).where(
                WorkoutLog.user_id == user.id,
                WorkoutLog.date == session.date,
                ((WorkoutLog.session_id == session.id) | ((WorkoutLog.session_id.is_(None)) & (WorkoutLog.note == f"训练会话 {session.id}"))),
            )
        ).all())
    burned = [calorie_stats.burn_for_log(log, weight) for log in logs]
    kcal = round(sum(float(item.get("kcal") or 0) for item in burned), 1)
    state = recent_training_state(db, user)
    items = session.items or []
    completed_items = [
        {"name": item.get("name"), "action_ref": item.get("action_ref"),
         "completed_sets": item.get("completed_sets"), "target_sets": item.get("target_sets"),
         "status": item.get("status"), "replaced_from": item.get("replaced_from")}
        for item in items
    ]
    unfinished = [item for item in completed_items
                  if item["status"] != "done" or int(item["completed_sets"] or 0) < int(item["target_sets"] or 1)]
    return {
        "session": session_payload(session),
        "summary": {
            "actual_minutes": session.actual_minutes,
            "actual_minutes_known": any(x.get('kind')=='fact_finish' and x.get('actual_minutes_known') for x in session.agent_log or []),
            "completion_pct": session.completion_pct,
            "difficulty": session.difficulty,
            "fatigue": session.fatigue,
            "kcal": kcal,
            "weight_source": source,
            "done_actions": sum(1 for item in items if item.get("status") == "done"),
            "total_actions": len(items),
            "unfinished": unfinished,
            "items": completed_items,
            "note": session.note,
        },
        "training_state": state,
        "logs": [
            {
                "log_id": log.id,
                "action_ref": log.action_ref,
                "kind": log.kind,
                "duration_min": log.duration_min,
                "sets": log.sets,
                "date": log.date.isoformat(),
            }
            for log in logs
        ],
    }


def advance_to_next(db: Session, user: User, session_id: str) -> dict:
    """当前动作做不下去时往前推进（不记完成，只标记跳过）。"""
    return skip_action(db, user, session_id, reason="用户切到下一个动作")


def session_summary_for_day(db: Session, user: User, day: date) -> dict | None:
    session = latest_session(db, user, day)
    if session is None:
        return None
    return session_payload(session)


def record_manual_feedback(
    db: Session,
    user: User,
    day: date,
    *,
    completed: bool,
    difficulty: int,
    fatigue: int,
    note: str = "",
    actual_minutes: int | None = None,
) -> dict:
    """用户没点「开始训练」也能用一句话记反馈：自动建会话再完成。"""
    session = start_session(db, user, day, source="feedback", fallback_next=True)
    return finish_session(
        db, user, session.id,
        completed=completed,
        difficulty=difficulty,
        fatigue=fatigue,
        note=note,
        actual_minutes=actual_minutes,
    )


def unavailable_equipment_context(db: Session, user: User, day: date) -> None:
    """把"器械被占用"写进当天临时上下文，供后续调整复用。"""
    set_temporary(db, user, day, "equipment_busy", {"busy": True}, source="workout")
