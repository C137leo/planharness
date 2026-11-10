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
    return str(action.get("name") or action.get("action_ref") or "动作")


def _target_sets(action: dict) -> int:
    slot = str(action.get("slot") or "")
    if slot in ("warmup", "mobility"):
        return 1
    return max(1, int(action.get("sets") or 3))


def _item_from_action(action: dict, index: int) -> dict:
    ref = str(action.get("action_ref") or "unknown")
    return {
        "id": new_id(),
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
    existing = active_session(db, user, day)
    if existing is not None:
        return existing

    day_plan = effective_day(db, user, day, fallback_next=fallback_next)
    actions = day_plan.get("actions") or []
    if not actions:
        raise ValueError("今天没有可执行的训练动作，先让教练生成计划")

    items = [_item_from_action(action, index) for index, action in enumerate(actions)]
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


def _current_index(session: WorkoutSession) -> int | None:
    for index, item in enumerate(session.items or []):
        if item.get("status") in ("pending", "active"):
            return index
    return None


def _progress(session: WorkoutSession) -> dict:
    items = session.items or []
    total_sets = sum(max(1, int(item.get("target_sets") or 1)) for item in items)
    done_sets = sum(int(item.get("completed_sets") or 0) for item in items)
    done_actions = sum(1 for item in items if item.get("status") == "done")
    skipped = sum(1 for item in items if item.get("status") == "skipped")
    remaining_minutes = 0
    for item in items:
        if item.get("status") in ("done", "skipped"):
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


def session_payload(session: WorkoutSession, *, include_items: bool = True) -> dict:
    items = list(session.items or [])
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
        "date": session.date.isoformat(),
        "status": session.status,
        "source": session.source,
        "plan_version": session.plan_version,
        "current_index": current_index,
        "current_item": current,
        "items": items if include_items else [],
        "planned_minutes": session.planned_minutes,
        "actual_minutes": session.actual_minutes or elapsed,
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


def complete_set(db: Session, user: User, session_id: str) -> dict:
    session = _session(db, user, session_id)
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
    session = _session(db, user, session_id)
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
    session = _session(db, user, session_id)
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
    minutes_remaining: int | None = None,
    injured_regions: list[str] | None = None,
) -> tuple[dict, list[str]]:
    """只调整"当前 + 剩余"：减组、压时间、避开不适部位，不换已完成动作。"""
    session = _session(db, user, session_id)
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
        if easier:
            current = max(1, int(entry.get("target_sets") or 1))
            entry["target_sets"] = max(1, current - 1)
            if entry["target_sets"] != current:
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
    session = _session(db, user, session_id)
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
    difficulty: int = 3,
    fatigue: int = 3,
    actual_minutes: int | None = None,
    note: str = "",
) -> dict:
    """结束训练：写真实训练记录 + 更新短期记忆。

    completed=True 表示"用户说完成了"：把剩余动作按用户自述记为完成，
    但不伪造组数——它记录的是 user_reported 完成，而不是系统观察到每组都点过。
    """
    session = _session(db, user, session_id)
    if session.status == "finished":
        return finish_payload(db, user, session)

    items = [dict(item) for item in (session.items or [])]
    if completed:
        for item in items:
            if item.get("status") in ("pending", "active"):
                item["completed_sets"] = max(1, int(item.get("target_sets") or 1))
                item["status"] = "done"
                item["completed_at"] = _now().isoformat(timespec="seconds")
                item["user_reported"] = True
        session.completion_pct = 100.0
    elif completion_pct is not None:
        session.completion_pct = max(0.0, min(100.0, float(completion_pct)))
    else:
        session.completion_pct = _score_completion(items)

    elapsed = 0
    if session.started_at:
        started = session.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=dt.timezone.utc)
        elapsed = max(0, int((_now() - started).total_seconds() // 60))
    session.actual_minutes = int(actual_minutes or elapsed or session.planned_minutes)
    session.difficulty = max(1, min(5, int(difficulty or 3)))
    session.fatigue = max(1, min(5, int(fatigue or 3)))
    session.note = (note or "")[:1000]
    session.status = "finished"
    session.finished_at = _now()
    session.items = items
    session.current_index = len(items)

    logs = _write_workout_logs(db, user, session)
    upsert_memory(
        db, user,
        kind=SHORT_TERM,
        category="session",
        key=f"session:{session.id}",
        value=(
            f"{session.date.isoformat()} 完成 {session.completion_pct:.0f}% · "
            f"难度 {session.difficulty}/5 · 疲劳 {session.fatigue}/5"
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
    logs: list[WorkoutLog] = []
    weight, _source = _weight_for(db, user)
    for item in session.items or []:
        done = int(item.get("completed_sets") or 0)
        if done <= 0:
            continue
        target = max(1, int(item.get("target_sets") or 1))
        duration = max(1, round(int(item.get("duration_min") or 8) * min(done, target) / target))
        kind = "strength"
        if item.get("slot") == "cardio":
            kind = "cardio"
        elif item.get("slot") in ("warmup", "mobility"):
            kind = "flexibility"
        intensity = "normal"
        if int(session.difficulty or 3) >= 4:
            intensity = "hard"
        elif int(session.difficulty or 3) <= 2:
            intensity = "light"
        log = WorkoutLog(
            user_id=user.id,
            date=session.date,
            action_ref=str(item.get("action_ref") or "unknown"),
            kind=kind,
            duration_min=duration,
            sets=done,
            reps=item.get("target_reps"),
            intensity=intensity,
            note=f"训练会话 {session.id}",
        )
        db.add(log)
        logs.append(log)
    db.flush()
    for log in logs:
        # 触发一次热量估算，确保记录写入的是可复算的 MET 口径
        calorie_stats.burn_for_log(log, weight)
    return logs


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
                WorkoutLog.note == f"训练会话 {session.id}",
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
