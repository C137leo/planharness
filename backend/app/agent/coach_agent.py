"""Single production chat facade; semantics are delegated exclusively to the LLM."""
from __future__ import annotations
import datetime as dt
import json
import re
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import AgentMessage, AgentRun, User, new_id

_STORED_RESULT_KEYS = (
    "intent", "intent_label", "scope", "reason", "reply",
    "changes", "actions", "day", "session", "plan_draft", "patch_request",
    "today_brief", "today_brief_run_id", "mcd_order", "memories", "memory_saved",
    "guidance", "feedback", "progress_insights", "warnings", "blocked",
    "task_plan", "task_results", "execution_history", "overall_status",
    "pending_confirmation", "context_used", "unfulfilled_goals", "checkout", "order", "commerce_catalog", "commerce_nutrition", "commerce_connection", "workout_records", "day_revision", "workout_replan",
)


def _stored_result(result: dict) -> dict:
    """只落历史卡片需要的结果，避免把 trace/页面上下文写进消息表。"""
    return {
        key: result[key]
        for key in _STORED_RESULT_KEYS
        if result.get(key) not in (None, [], {}, "")
    }


def _record_messages(
    db: Session, user: User, text: str, result: dict, *, thread: str = "coach",
) -> None:
    stamp = dt.datetime.now(dt.timezone.utc)
    db.add(AgentMessage(
        id=new_id(), user_id=user.id, thread=thread, role="user", text=text[:2000],
        intent={"intent": result.get("intent"), "scope": result.get("scope")},
        run_id=result.get("run_id"), created_at=stamp,
    ))
    db.add(AgentMessage(
        id=new_id(), user_id=user.id, thread=thread, role="agent",
        text=str(result.get("reply") or "")[:2000],
        intent={"intent": result.get("intent"), "scope": result.get("scope")},
        actions=result.get("actions") or [],
        result=_stored_result(result),
        run_id=result.get("run_id"), created_at=stamp + dt.timedelta(microseconds=1),
    ))


def recent_messages(db: Session, user: User, thread: str, limit: int = 8) -> list[dict]:
    rows = db.scalars(
        select(AgentMessage)
        .where(AgentMessage.user_id == user.id, AgentMessage.thread == thread)
        .order_by(AgentMessage.created_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "role": row.role,
            "text": row.text,
            "intent": (row.intent or {}).get("intent"),
            "scope": (row.intent or {}).get("scope"),
            "actions": [
                {"type": action.get("type"), "label": action.get("label")}
                for action in (row.actions or [])
                if isinstance(action, dict)
            ],
            "result_kind": str((row.result or {}).get("kind") or ""),
            "task_plan": (row.result or {}).get("task_plan") or [],
            "task_results": (row.result or {}).get("task_results") or [],
            "historical_context": True,
            "plan_version_at_message": ((row.result or {}).get('day') or {}).get('plan_version'),
        }
        for row in reversed(rows)
    ]


def mark_stored_plan_result(
    db: Session, user: User, *, status: str,
    draft_id: str | None = None, request_id: str | None = None,
    version: int = 0,
) -> None:
    """计划确认/废弃后，把历史消息里的草案状态同步掉，避免刷新后重复确认。"""
    rows = db.scalars(
        select(AgentMessage).where(
            AgentMessage.user_id == user.id,
            AgentMessage.role == "agent",
        )
    ).all()
    for row in rows:
        saved = dict(row.result or {})
        changed = False
        draft = saved.get("plan_draft") or {}
        if draft_id and str(draft.get("draft_id") or "") == draft_id:
            next_draft = dict(draft)
            next_draft["status"] = status
            if version:
                next_draft["committed_version"] = version
            saved["plan_draft"] = next_draft
            saved["actions"] = []
            changed = True
        patch = saved.get("patch_request") or {}
        if request_id and str(patch.get("request_id") or "") == request_id:
            next_patch = dict(patch)
            next_patch["status"] = status
            if version:
                next_patch["committed_version"] = version
            saved["patch_request"] = next_patch
            saved["actions"] = []
            changed = True
        if changed:
            row.result = saved
    db.commit()


def normalize_thread(thread: str | None) -> str:
    """对话线程 ID 只允许短标识；空值回退到旧的单线程。"""
    value = re.sub(r"[^a-zA-Z0-9_-]", "", (thread or "").strip())[:32]
    return value or "coach"


def _iso_utc(value: dt.datetime | None) -> str:
    """SQLite 读出来常常是 naive datetime；按 UTC 补时区，前端才会显示本地时间。"""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.isoformat()


def conversations_payload(db: Session, user: User, limit: int = 30) -> list[dict]:
    """按 thread 聚合对话列表：标题取第一条用户消息，预览取最后一条。"""
    rows = db.scalars(
        select(AgentMessage)
        .where(AgentMessage.user_id == user.id)
        .order_by(AgentMessage.created_at.asc())
    ).all()
    grouped: dict[str, list[AgentMessage]] = {}
    for row in rows:
        grouped.setdefault(normalize_thread(row.thread), []).append(row)
    items: list[dict] = []
    for thread, thread_rows in grouped.items():
        first_user = next((r for r in thread_rows if r.role == "user"), thread_rows[0])
        last = thread_rows[-1]
        title = " ".join((first_user.text or "").split())[:30] or "新的对话"
        preview = " ".join((last.text or "").split())[:48]
        updated = _iso_utc(last.created_at)
        items.append({
            "thread": thread,
            "title": title,
            "preview": preview,
            "message_count": len(thread_rows),
            "updated_at": updated,
        })
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    return items[:max(1, min(limit, 50))]


def messages_payload(
    db: Session, user: User, limit: int = 20, thread: str = "coach",
) -> list[dict]:
    thread = normalize_thread(thread)
    rows = db.scalars(
        select(AgentMessage)
        .where(AgentMessage.user_id == user.id, AgentMessage.thread == thread)
        .order_by(AgentMessage.created_at.desc())
        .limit(max(1, min(limit, 100)))
    ).all()
    return [
        {
            "id": row.id,
            "role": row.role,
            "text": row.text,
            "intent": row.intent or {},
            "actions": row.actions or [],
            "result": row.result or {},
            "created_at": _iso_utc(row.created_at),
        }
        for row in reversed(rows)
    ]


def _run(db, user, text, **kwargs):
    """The only production semantic route. No rule router fallback."""
    from app.agent.semantic_runtime import run as run_semantic
    from app.llm.deadline import model_budget
    with model_budget():
        return run_semantic(db, user, text, **kwargs)


def run(db, user, text, *, request_id=None, **kwargs):
    """Claim an optional request key before execution; replay only completed results.

    Incomplete claims deliberately require reconciliation, never blind re-execution.
    """
    from hashlib import sha256
    from uuid import NAMESPACE_URL, uuid5
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    db.info.pop('coach_claim_id', None)
    db.info["coach_request_key"] = request_id or new_id()
    if not request_id:
        return _run(db, user, text, **kwargs)
    key = str(uuid5(NAMESPACE_URL, f"coach:{user.id}:{request_id}"))
    # Clients are runtime objects, not part of request identity.
    fingerprint = sha256(json.dumps({"text": text, **{k: v for k, v in kwargs.items() if k != "client"}},
                                   sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()

    def replay(row):
        if row.input.get("fingerprint") != fingerprint:
            raise HTTPException(409, "request_id 已用于不同请求")
        if row.status != "succeeded":
            raise HTTPException(409, "请求执行中或需要核对任务记录，请勿重复执行")
        return row.output

    previous = db.get(AgentRun, key)
    if previous:
        return replay(previous)
    claim = AgentRun(id=key, user_id=user.id, kind="coach_request", mode="agent",
                     status="running", input={"fingerprint": fingerprint,
                     "request_id": request_id, "thread": kwargs.get("thread", "coach")})
    db.add(claim)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return replay(db.get(AgentRun, key))
    db.info['coach_claim_id'] = key
    try:
        from app.llm.cancellation import cancellation_scope, ModelCancelled
        from sqlalchemy.orm import Session as ControlSession
        def cancelled():
            with ControlSession(db.get_bind()) as control:
                return control.get(AgentRun,key).status=='cancel_requested'
        def begin():
            db.commit()  # Persist only planning/snapshot artifacts before dispatch; release SQLite writer lock.
            with ControlSession(db.get_bind()) as control:
                row=control.scalar(select(AgentRun).where(AgentRun.id==key).with_for_update())
                if row.status=='cancel_requested': raise ModelCancelled()
                row.input={**row.input,'phase':'executing'}
                control.commit()
        try:
            with cancellation_scope(cancelled,begin):
                result = _run(db,user,text,**kwargs)
        except ModelCancelled:
            db.rollback()
            result={'overall_status':'cancelled','reply':'已停止本次处理，未执行本次修改。','actions':[]}
            _record_messages(db,user,text,result,thread=kwargs.get('thread','coach'))
        claim = db.get(AgentRun, key)
        claim.output = result
        claim.status = "succeeded"
        claim.finished_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        return result
    except Exception:
        db.rollback()
        claim = db.get(AgentRun, key)
        if claim.status != 'succeeded':
            claim.status = "needs_reconciliation"
            db.commit()
        raise
    finally:
        db.info.pop('coach_claim_id', None)
