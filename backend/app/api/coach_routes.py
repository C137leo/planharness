"""统一健身教练 Agent 与训练执行接口。

一个 `POST /agent/chat` 覆盖所有入口：今天页、计划页、我的页、训练页。
页面只传 `page` 作为上下文提示，真正的意图识别与业务调用在
`app/agent/coach_agent.py` 里完成，不存在"每个页面一套逻辑"。
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.agent import coach_agent
from app.api.routes import get_current_user, get_db, ok
from app.domain import workout as workout_service
from app.domain.day_plan import (
    adjustment_payload,
    effective_day,
    list_adjustments,
    mark_stale_adjustments,
    reset_day,
    restore_adjustment,
)
from app.domain.memory import (
    CATEGORY_LABELS,
    LONG_TERM,
    SHORT_TERM,
    archive_memory,
    list_memories,
    list_temporary,
    memory_payload,
    temporary_context_payload,
    update_memory,
    upsert_memory,
)
from app.domain.profile_sync import apply_memory_to_profile
from app.domain.progress import coach_insight_payload
from app.domain.timeutil import user_today
from app.llm.tool_client import tool_client_from_settings
from app.models import AgentMessage, AuditEvent, Plan, PlanAdjustment, PlanVersion, Profile, User

router = APIRouter(prefix="/api/v1")


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    page: str = Field(default="today", max_length=24)
    date: str | None = None
    session_id: str | None = None
    thread: str | None = Field(default=None, max_length=32)
    # 页面附带的业务上下文（今日动作、长期计划、进展数据、档案摘要）。
    # 它只作为 Agent 的只读上下文，不允许直接写库。
    context: dict = Field(default_factory=dict)


class DayQuery(BaseModel):
    date: str | None = None


class MemoryIn(BaseModel):
    kind: str = Field(default=LONG_TERM, max_length=16)
    category: str = Field(min_length=1, max_length=32)
    key: str = Field(default="manual", max_length=64)
    value: str = Field(min_length=1, max_length=2000)
    structured: dict = Field(default_factory=dict)


class MemoryPatchIn(BaseModel):
    value: str | None = Field(default=None, max_length=2000)
    structured: dict | None = None


class SessionIn(BaseModel):
    date: str | None = None
    source: str = Field(default="agent", max_length=16)


class SessionEventIn(BaseModel):
    reason: str = ""
    action_ref: str = ""
    equipment_busy: bool = False
    injured_regions: list[str] = Field(default_factory=list)
    easier: bool = False
    minutes_remaining: int | None = Field(default=None, ge=1, le=240)


class FinishIn(BaseModel):
    completed: bool = True
    completion_pct: int | None = Field(default=None, ge=0, le=100)
    difficulty: int = Field(default=3, ge=1, le=5)
    fatigue: int = Field(default=3, ge=1, le=5)
    actual_minutes: int | None = Field(default=None, ge=0, le=600)
    note: str = Field(default="", max_length=1000)


def _day(value: str | None, user: User | None = None, db: Session | None = None) -> dt.date:
    if value:
        try:
            return dt.date.fromisoformat(value)
        except ValueError as exc:
            raise HTTPException(422, "date 需要 YYYY-MM-DD") from exc
    profile = db.get(Profile, user.id) if user is not None and db is not None else None
    return user_today(profile)


def _tool_client(request: Request):
    client = tool_client_from_settings(request.app.state.settings)
    return client if client.available() else None


def _day_payload(db: Session, user: User, day: dt.date) -> dict:
    payload = effective_day(db, user, day, fallback_next=True)
    active = workout_service.active_session(db, user, day)
    latest = workout_service.latest_session(db, user, day)
    session = active or latest
    payload["session"] = workout_service.session_payload(session) if session else None
    payload["has_active_session"] = active is not None
    # 今天已经完成 / 没完成的事实（来自会话，不用前端再拼）
    payload["completed"] = bool(
        latest and latest.status == "finished" and float(latest.completion_pct or 0) >= 60
    )
    payload["finished"] = bool(latest and latest.status == "finished")
    plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    if plan is not None:
        version = db.scalar(select(PlanVersion).where(
            PlanVersion.plan_id == plan.id, PlanVersion.version == plan.current_version
        ))
        payload["plan_summary"] = version.summary if version else ""
    return payload


@router.post("/agent/chat")
def agent_chat(
    payload: ChatIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    day = _day(payload.date, user, db)
    result = coach_agent.run(
        db, user, payload.text,
        page=payload.page,
        day=day,
        session_id=payload.session_id,
        page_context=payload.context,
        thread=payload.thread or "coach",
        client=_tool_client(request),
    )
    return ok(request, result)


@router.get("/agent/messages")
def agent_messages(
    request: Request,
    limit: int = 20,
    thread: str = "coach",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ok(request, coach_agent.messages_payload(db, user, limit=limit, thread=thread))


@router.get("/agent/conversations")
def agent_conversations(
    request: Request,
    limit: int = 30,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ok(request, coach_agent.conversations_payload(db, user, limit=limit))


@router.delete("/agent/conversations/{thread}")
def delete_agent_conversation(
    thread: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    clean = coach_agent.normalize_thread(thread)
    count = db.query(AgentMessage).filter(
        AgentMessage.user_id == user.id,
        AgentMessage.thread == clean,
    ).delete(synchronize_session=False)
    db.commit()
    return ok(request, {"thread": clean, "deleted": count})


@router.get("/agent/day")
def agent_day(
    request: Request,
    date: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    day = _day(date, user, db)
    return ok(request, _day_payload(db, user, day))


@router.post("/agent/day/reset")
def agent_day_reset(
    request: Request,
    payload: DayQuery,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    day = _day(payload.date, user, db)
    reset_day(db, user, day)
    db.commit()
    return ok(request, _day_payload(db, user, day), message="reset")


@router.get("/agent/adjustments")
def agent_adjustments(
    request: Request,
    start: str = "",
    end: str = "",
    limit: int = 60,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """计划页/今天页读取临时调整历史；带日期范围，便于日历打标。"""
    try:
        start_date = dt.date.fromisoformat(start) if start else None
        end_date = dt.date.fromisoformat(end) if end else None
    except ValueError as exc:
        raise HTTPException(422, "start/end 需要 YYYY-MM-DD") from exc
    plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    mark_stale_adjustments(db, user, plan.current_version if plan is not None else None)
    db.commit()
    rows = list_adjustments(db, user, start=start_date, end=end_date, limit=limit)
    return ok(request, {
        "items": [adjustment_payload(row) for row in rows],
    })


@router.post("/agent/adjustments/{adjustment_id}/restore")
def agent_adjustment_restore(
    adjustment_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        row = restore_adjustment(db, user, adjustment_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    db.commit()
    return ok(request, {
        "adjustment": adjustment_payload(row),
        "day": _day_payload(db, user, row.effective_date),
    }, message="restored")


@router.get("/agent/progress-insights")
def agent_progress_insights(
    request: Request,
    days: int = 14,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """教练发现：从真实训练记录生成的、可点击进入统一 Agent 的建议。"""
    return ok(request, coach_insight_payload(db, user, days=max(7, min(90, days))))


@router.get("/agent/memory/status")
def agent_memory_status(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """只读：当前记忆保留配置 + 最近一次清理日志。"""
    settings = request.app.state.settings
    last = db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.stream_id == "system:memory",
            AuditEvent.action == "memory_lifecycle_run",
        )
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    )
    return ok(request, {
        "short_term_retention_days": settings.short_term_memory_retention_days,
        "temporary_context_retention_days": settings.temporary_context_retention_days,
        "cleanup_interval_seconds": settings.memory_cleanup_interval_seconds,
        "last_run": last.payload if last is not None else None,
        "last_run_at": last.created_at.isoformat() if last is not None else "",
    })


@router.get("/agent/memories")
def agent_memories(
    request: Request,
    kind: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = list_memories(db, user, kind=kind or None)
    items = [memory_payload(row) for row in rows]
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item["kind"], []).append(item)
    profile = db.get(Profile, user.id)
    today = user_today(profile)
    temporary = [
        temporary_context_payload(row)
        for row in list_temporary(db, user, today, include_completed=True)
    ]
    return ok(request, {
        "items": items,
        "groups": groups,
        "temporary": temporary,
        "labels": CATEGORY_LABELS,
    })


@router.post("/agent/memories")
def agent_memory_create(
    payload: MemoryIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = upsert_memory(
        db, user,
        kind=payload.kind if payload.kind in (LONG_TERM, SHORT_TERM) else LONG_TERM,
        category=payload.category,
        key=payload.key,
        value=payload.value,
        structured=payload.structured,
        source="user_edit",
        evidence="用户在记忆管理里手动添加",
    )
    if (row.structured or {}).get("canonical") or row.category in {
        "goal", "schedule", "location", "equipment", "like", "dislike",
    }:
        try:
            apply_memory_to_profile(db, user, row)
        except Exception:  # noqa: BLE001
            pass
    db.commit()
    return ok(request, memory_payload(row), message="created")


@router.put("/agent/memories/{memory_id}")
def agent_memory_update(
    memory_id: str,
    payload: MemoryPatchIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        row = update_memory(
            db, user, memory_id,
            value=payload.value,
            structured=payload.structured,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    db.commit()
    return ok(request, memory_payload(row), message="updated")


@router.delete("/agent/memories/{memory_id}")
def agent_memory_delete(
    memory_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        row = archive_memory(db, user, memory_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    db.commit()
    return ok(request, {"deleted": memory_id, "value": row.value}, message="deleted")


# ---------------------------------------------------------------- 训练执行


@router.post("/workout-sessions")
def workout_start(
    payload: SessionIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    day = _day(payload.date, user, db)
    try:
        session = workout_service.start_session(db, user, day, source=payload.source)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return ok(request, {
        "session": workout_service.session_payload(session),
        "day": _day_payload(db, user, day),
    }, message="started")


@router.get("/workout-sessions/active")
def workout_active(
    request: Request,
    date: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    day = _day(date, user, db)
    session = workout_service.active_session(db, user, day)
    if session is None:
        session = workout_service.latest_session(db, user, day)
    return ok(request, {
        "session": workout_service.session_payload(session) if session else None,
        "day": _day_payload(db, user, day),
    })


@router.get("/workout-sessions/{session_id}")
def workout_get(
    session_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = db.get(workout_service.WorkoutSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(404, "session not found")
    return ok(request, workout_service.session_payload(session))


@router.post("/workout-sessions/{session_id}/sets")
def workout_complete_set(
    session_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        payload = workout_service.complete_set(db, user, session_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return ok(request, {"session": payload}, message="set_done")


@router.post("/workout-sessions/{session_id}/skip")
def workout_skip(
    session_id: str,
    payload: SessionEventIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        session = workout_service.skip_action(db, user, session_id, reason=payload.reason)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return ok(request, {"session": session}, message="skipped")


@router.post("/workout-sessions/{session_id}/replace")
def workout_replace(
    session_id: str,
    payload: SessionEventIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        session, changes = workout_service.replace_action(
            db, user, session_id,
            action_ref=payload.action_ref,
            reason=payload.reason or "用户替换动作",
            equipment_busy=payload.equipment_busy,
            injured_regions=payload.injured_regions,
            easier=payload.easier,
            minutes_remaining=payload.minutes_remaining,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return ok(request, {"session": session, "changes": changes}, message="replaced")


@router.post("/workout-sessions/{session_id}/pause")
def workout_pause(
    session_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        session = workout_service.pause_session(db, user, session_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return ok(request, {"session": session}, message="toggled")


@router.post("/workout-sessions/{session_id}/finish")
def workout_finish(
    session_id: str,
    payload: FinishIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        result = workout_service.finish_session(
            db, user, session_id,
            completed=payload.completed,
            completion_pct=payload.completion_pct,
            difficulty=payload.difficulty,
            fatigue=payload.fatigue,
            actual_minutes=payload.actual_minutes,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    session = db.get(workout_service.WorkoutSession, session_id)
    day = session.date if session is not None else dt.date.today()
    return ok(request, {**result, "day": _day_payload(db, user, day)}, message="finished")


@router.post("/workout-sessions/{session_id}/feedback")
def workout_feedback(
    session_id: str,
    payload: FinishIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """兼容入口：和 finish 一样，语义更明确地"提交反馈"。"""
    return workout_finish(session_id, payload, request, user, db)
