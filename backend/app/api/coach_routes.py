"""统一健身教练 Agent 与训练执行接口。

一个 `POST /agent/chat` 覆盖所有入口：今天页、计划页、我的页、训练页。
页面只传 `page` 作为上下文提示，真正的意图识别与业务调用在
`app/agent/coach_agent.py` 里完成，不存在"每个页面一套逻辑"。
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

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
from app.models import (AgentMessage, AuditEvent, MealLog, Plan, PlanAdjustment, PlanVersion,
                        Profile, User)
from app.schemas import WorkoutSetIn

router = APIRouter(prefix="/api/v1")


from app.agent.binding_contract import PendingControl, CheckoutAction, CommerceAction


class ChatIn(BaseModel):
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    confirmation: PendingControl | None = None
    checkout_action: CheckoutAction | None = None
    commerce_action: CommerceAction | None = None
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
    completion_basis: Literal['recorded_only','confirm_current_plan'] = 'recorded_only'
    expected_revision: str | None = Field(default=None,min_length=64,max_length=64)
    completed: bool = True
    completion_pct: int | None = Field(default=None, ge=0, le=100)
    difficulty: int | None = Field(default=None, ge=1, le=5)
    fatigue: int | None = Field(default=None, ge=1, le=5)
    actual_minutes: int | None = Field(default=None, ge=0, le=600)
    note: str = Field(default="", max_length=1000)

# 这是干啥的?
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


def coach_client_dependency(request: Request):
    return _tool_client(request)


def _day_payload(db: Session, user: User, day: dt.date, *, fallback_next: bool = True) -> dict:
    payload = effective_day(db, user, day, fallback_next=fallback_next)
    active = workout_service.active_session(db, user, day)
    latest = workout_service.latest_session(db, user, day)
    session = active or latest
    from app.domain.workout_replan import current_session,recorded_history
    cycle=payload.get('workout_cycle_id')
    if cycle:session=current_session(db,user,day,cycle)
    payload["session"] = workout_service.session_payload(session) if session else None
    payload["day_progress"] = workout_service.progress_for_day(session, payload["actions"])
    payload['previous_training']=recorded_history(db,user,day,cycle) if cycle else []
    payload["has_active_session"] = session is not None and session.status in ('active','paused')
    # 今天已经完成 / 没完成的事实（来自会话，不用前端再拼）
    payload["completed"] = bool(
        session and session.status in ('finished','completed')
        and payload['day_progress']['total_sets']>0 and payload['day_progress']['done_sets']==payload['day_progress']['total_sets']
    )
    payload["finished"] = bool(session and session.status == "finished")
    plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    version = None
    if plan is not None:
        version = db.scalar(select(PlanVersion).where(
            PlanVersion.plan_id == plan.id, PlanVersion.version == plan.current_version
        ))
        payload["plan_summary"] = version.summary if version else ""
        payload["nutrition"] = plan.nutrition or {}

    # 评审 P-010：今天到底吃了什么，必须能记录并反映到"还能吃多少"。
    logs = db.scalars(
        select(MealLog)
        .where(MealLog.user_id == user.id, MealLog.date == day)
        .order_by(MealLog.created_at.asc())
    ).all()
    payload["meal_logs"] = [
        {"id": row.id, "slot": row.slot, "title": row.title, "kcal": row.kcal,
         "protein_g": row.protein_g, "note": row.note}
        for row in logs
    ]
    intake_kcal = sum(int(row.kcal or 0) for row in logs)
    intake_protein = round(sum(float(row.protein_g or 0) for row in logs), 1)
    nutrition = (plan.nutrition if plan is not None else {}) or {}
    target_kcal = int(nutrition.get("target_kcal") or (version.calorie_target if version else 0) or 0)
    target_protein = float(
        ((nutrition.get("macros") or {}).get("protein_g") or {}).get("low_g") or 0
    )
    payload["intake"] = {
        "kcal": intake_kcal,
        "protein_g": intake_protein,
        "target_kcal": target_kcal,
        "target_protein_g": target_protein,
        "remaining_kcal": max(target_kcal - intake_kcal, 0) if target_kcal else 0,
        "remaining_protein_g": max(round(target_protein - intake_protein, 1), 0) if target_protein else 0,
    }
    return payload

def coach_commerce_dependency(request: Request):
    from app.domain.commerce import CommerceProvider
    from app.food.mcd_transport import McdCommerceClient
    from hashlib import sha256
    settings=request.app.state.settings
    supplied=request.headers.get("X-MCD-Token", "").strip()
    token=supplied
    if not token:return None
    if len(token)>512:raise HTTPException(422,"MCP凭证过长")
    url=settings.mcd_mcp_url
    return CommerceProvider(account_key=sha256((url+"\0"+token).encode()).hexdigest(),
        client_factory=lambda:McdCommerceClient(url=url,token=token,timeout=settings.mcd_mcp_timeout),
        allow_orders=bool(settings.mcd_mcp_allow_order))


@router.get("/agent/commerce/status")
def commerce_status(request: Request,user: User=Depends(get_current_user),p=Depends(coach_commerce_dependency)):
    return ok(request,{'status':'ready' if p else 'token_required','guide_url':'https://open.mcd.cn/mcp',
                       'allow_order':bool(p and p.allow_orders)})


def _history_legacy_key(request:Request):
    from hashlib import sha256
    settings=request.app.state.settings
    supplied=request.headers.get('X-MCD-Token','').strip()
    token=supplied
    return sha256(token.encode()).hexdigest() if token else ''


@router.get('/agent/commerce/orders')
def commerce_orders(request:Request,group:Literal['active','paid','inactive','unknown']='active',cursor:str='',limit:int=20,
        user:User=Depends(get_current_user),db:Session=Depends(get_db),p=Depends(coach_commerce_dependency)):
    from app.domain import commerce_history
    if not 1<=limit<=50 or len(cursor)>512:raise HTTPException(422,'分页参数无效。')
    try:return ok(request,commerce_history.list_orders(db,user.id,group,cursor,limit,p=p,legacy_key=_history_legacy_key(request)))
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc


@router.post('/agent/commerce/orders/sync')
def commerce_orders_sync(request:Request,user:User=Depends(get_current_user),db:Session=Depends(get_db),p=Depends(coach_commerce_dependency)):
    from app.domain import commerce_history
    try:return ok(request,commerce_history.sync_recent(db,user.id,p,legacy_key=_history_legacy_key(request)))
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc


@router.get('/agent/commerce/orders/{record_id}')
def commerce_order_detail(record_id:str,request:Request,user:User=Depends(get_current_user),db:Session=Depends(get_db),p=Depends(coach_commerce_dependency)):
    from app.domain import commerce_history
    try:
        value=commerce_history.detail(db,user.id,record_id,p=p,legacy_key=_history_legacy_key(request))
        if value is None:raise LookupError('这条记录尚未提交为订单。')
        return ok(request,value)
    except LookupError as exc:raise HTTPException(404,str(exc)) from exc


@router.post('/agent/commerce/orders/{record_id}/refresh')
def commerce_order_refresh(record_id:str,request:Request,user:User=Depends(get_current_user),db:Session=Depends(get_db),p=Depends(coach_commerce_dependency)):
    from app.domain import commerce_history
    try:return ok(request,commerce_history.refresh(db,user.id,record_id,p,legacy_key=_history_legacy_key(request)))
    except LookupError as exc:raise HTTPException(404,str(exc)) from exc
    except Exception as exc:
        db.rollback()
        from app.domain.commerce import CheckoutError
        message=str(exc) if isinstance(exc,(ValueError,CheckoutError)) else '暂时无法同步官方订单，已保留原记录，请稍后重试。'
        raise HTTPException(422,message) from exc


class PickupPreferencesIn(BaseModel):
    city: str = Field(default='',max_length=80)
    keyword: str = Field(default='',max_length=120)


@router.get('/agent/commerce/preferences')
def pickup_preferences(request:Request,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    from app.domain.commerce import pickup_preferences
    return ok(request,pickup_preferences(db,user.id))


@router.put('/agent/commerce/preferences')
def save_pickup_preferences(payload:PickupPreferencesIn,request:Request,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    from app.domain.commerce import save_pickup_preferences,CheckoutError
    try:value=save_pickup_preferences(db,user.id,payload.city,payload.keyword)
    except CheckoutError as exc:raise HTTPException(422,str(exc)) from exc
    db.commit();return ok(request,value)


# 主agent chat方法
@router.post("/agent/chat")
def agent_chat(
    payload: ChatIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    client=Depends(coach_client_dependency),
    commerce_provider=Depends(coach_commerce_dependency),
):
    db.info["commerce_provider"] = commerce_provider
    day = _day(payload.date, user, db)
    result = coach_agent.run(
        db, user, payload.text,
        request_id=payload.request_id,
        page=payload.page,
        day=day,
        session_id=payload.session_id,
        page_context=payload.context,
        thread=payload.thread or "coach",
        client=client,
        confirmation=payload.confirmation,
        checkout_action=payload.checkout_action,
        commerce_action=payload.commerce_action,
    )
    return ok(request, result)


@router.post('/agent/requests/{request_id}/cancel')
def cancel_agent_request(request_id:str,request:Request,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    from uuid import NAMESPACE_URL,uuid5
    from app.models import AgentRun
    if not request_id or len(request_id)>128:raise HTTPException(422,'请求编号无效')
    key=str(uuid5(NAMESPACE_URL,f'coach:{user.id}:{request_id}'))
    row=db.scalar(select(AgentRun).where(AgentRun.id==key,AgentRun.user_id==user.id,AgentRun.kind=='coach_request').with_for_update())
    if row is None:raise HTTPException(404,'尚未查询到该请求')
    if row.status=='succeeded':return ok(request,{'status':'completed','result':row.output})
    if row.input.get('phase')=='executing':
        return ok(request,{'status':'too_late','message':'操作已进入执行阶段，无法撤销，请等待实际结果。'})
    if row.status not in {'running','cancel_requested'}:
        return ok(request,{'status':'needs_reconciliation','message':'结果需要核对，不能通过停止视为已撤销。'})
    row.status='cancel_requested';db.commit()
    return ok(request,{'status':'cancel_requested'})


@router.get("/agent/requests/{request_id}")
def agent_request_result(request_id: str, request: Request,
                         user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Read-only reconciliation; never dispatch or replay a timed-out write."""
    from uuid import NAMESPACE_URL, uuid5
    from app.models import AgentRun
    if not request_id or len(request_id) > 128:
        raise HTTPException(422, '请求编号无效')
    row = db.get(AgentRun, str(uuid5(NAMESPACE_URL, f'coach:{user.id}:{request_id}')))
    if row is None or row.user_id != user.id or row.kind != 'coach_request':
        raise HTTPException(404, '尚未查询到该请求，请核对消息记录，不要重复提交')
    from app.domain.timeutil import as_utc
    state = row.status
    if state == 'running' and dt.datetime.now(dt.timezone.utc) - as_utc(row.created_at) > dt.timedelta(minutes=5):
        # Read-only projection: an old claim is uncertain, never permission to retry.
        state = 'needs_reconciliation'
    return ok(request, {'request_id': request_id, 'status': state, 'thread': row.input.get('thread', 'coach'),
                        'result': row.output if state == 'succeeded' else None})


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
    fallback_next: bool = True,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    day = _day(date, user, db)
    return ok(request, _day_payload(db, user, day, fallback_next=fallback_next))


@router.get('/agent/workout-replans/{authorization_id}')
def workout_replan_read(authorization_id: str, request: Request, user: User=Depends(get_current_user), db: Session=Depends(get_db)):
    from app.domain import workout_replan,day_revision
    try:
        row=workout_replan.owned(db,user,authorization_id)
        value=workout_replan.payload(row,day_revision.digest(day_revision.context(db,user,row.date)))
        return ok(request,value)
    except day_revision.RevisionError as exc:raise HTTPException(404,str(exc)) from exc


@router.get('/agent/day-revisions/{revision_id}')
def day_revision_read(revision_id: str, request: Request, user: User=Depends(get_current_user), db: Session=Depends(get_db)):
    from app.domain import day_revision as revisions
    try:
        row=revisions._owned(db,user,revision_id)
        value=revisions.preview(db,user,revision_id)
        value['base_matches_current']=revisions.digest(revisions.context(db,user,row.date))==row.base_revision
        db.commit()
        return ok(request,value)
    except revisions.RevisionError as exc:
        raise HTTPException(404,str(exc)) from exc


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
        "last_run_at": coach_agent._iso_utc(last.created_at) if last is not None else "",
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
    from app.domain.workout_replan import active_cycle, current_session
    cycle = active_cycle(db, user, day)
    session = current_session(db, user, day, cycle)
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
    body: WorkoutSetIn | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        payload = workout_service.complete_set(
            db, user, session_id,
            reps=body.reps if body else None,
            weight_kg=body.weight_kg if body else None,
            rpe=body.rpe if body else None,
        )
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
    if payload.completion_basis=='confirm_current_plan' and not payload.expected_revision:
        raise HTTPException(422,'请先读取最新完成摘要，再确认默认完成值。')
    try:
        result = workout_service.finish_session(
            db, user, session_id,
            completed=payload.completed,
            completion_pct=payload.completion_pct,
            difficulty=payload.difficulty,
            fatigue=payload.fatigue,
            actual_minutes=payload.actual_minutes,
            note=payload.note,
            completion_basis=payload.completion_basis,
            expected_revision=payload.expected_revision,
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


@router.get('/agent/runtime/conversations')
def runtime_conversations(request:Request,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    from app.domain.runtime_console import conversations
    return ok(request,conversations(db,user.id))


@router.get('/agent/runtime/conversations/{thread}')
def runtime_messages(thread:str,request:Request,before:str='',limit:int=50,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    from app.domain.runtime_console import messages
    if not 1<=limit<=100:raise HTTPException(422,'每页消息数应为1–100。')
    if coach_agent.normalize_thread(thread)!=thread:raise HTTPException(422,'会话标识无效。')
    try:return ok(request,messages(db,user.id,thread,limit,before))
    except LookupError as exc:raise HTTPException(404,str(exc)) from exc


@router.get('/agent/runtime/turns/{turn_id}')
def runtime_turn(turn_id:str,request:Request,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    from app.domain.runtime_console import turn
    try:return ok(request,turn(db,user.id,turn_id))
    except LookupError as exc:raise HTTPException(404,str(exc)) from exc


@router.post('/agent/runtime/conversations')
def runtime_create_conversation(request:Request,user:User=Depends(get_current_user),db:Session=Depends(get_db)):
    from app.models import RuntimeConversation,new_id
    thread=new_id().replace('-','')
    db.add(RuntimeConversation(id=thread,user_id=user.id));db.commit()
    return ok(request,{'thread':thread,'title':'新的会话','status':'idle'})
