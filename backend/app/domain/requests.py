"""补丁请求服务：一句话 → 意图 → 草案 → 确认应用。

和多轮修改的规则：

- 每轮「再改改」都基于**同一个 base_version** 重新规划（不是在上一个草案上叠加），
  这样用户改主意时不会越改越乱；
- 最多 3 轮，超过就要求人给更明确的信息；
- 只有 approve 才写新版本，草案阶段计划一个字节都没动。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import append_event
from app.domain.patches import (
    PatchValidationError,
    _action_dict,
    _apply_operations,
    _diff,
    _load_plan,
    _safety_check,
    approve_patch,
    reject_patch,
    rollback_plan,
)
from app.domain.planner import Intent, parse_intent, plan_operations, violates_constraints
from app.models import (
    PatchFeedback,
    PatchRequest,
    Plan,
    PlanAction,
    PlanPatch,
    PlanVersion,
    User,
    new_id,
)

MAX_ROUNDS = 3


def _current_actions(db: Session, plan: Plan) -> list[dict]:
    version = db.scalar(
        select(PlanVersion).where(
            PlanVersion.plan_id == plan.id, PlanVersion.version == plan.current_version
        )
    )
    if version is None:
        raise PatchValidationError(["current plan version not found"])
    return [
        _action_dict(a)
        for a in db.scalars(
            select(PlanAction)
            .where(PlanAction.plan_version_id == version.id)
            .order_by(PlanAction.day_index, PlanAction.slot, PlanAction.seq)
        ).all()
    ]


def create_request(
    db: Session,
    user: User,
    plan_id: str,
    text: str,
    *,
    quick_action: str = "",
    base_version: int | None = None,
    parent_request_id: str | None = None,
) -> PatchRequest:
    plan = _load_plan(db, user, plan_id)
    if base_version is not None and base_version != plan.current_version:
        raise PatchValidationError(
            [f"plan changed: base_version={base_version}, current={plan.current_version}"]
        )

    request = PatchRequest(
        user_id=user.id,
        plan_id=plan.id,
        base_version=plan.current_version,
        raw_text=text or "",
        quick_action=quick_action or "",
        parent_request_id=parent_request_id,
        status="clarifying",
    )

    # 轮数：沿着 parent 链往上数
    round_no = 1
    cursor = parent_request_id
    while cursor:
        parent = db.get(PatchRequest, cursor)
        if parent is None:
            break
        round_no += 1
        cursor = parent.parent_request_id
    request.round = round_no
    db.add(request)
    db.flush()

    # 超过轮数上限：不再自动重规划，要求人给更具体的信息
    # 初稿是第 1 轮，允许再改 MAX_ROUNDS 轮
    if round_no > MAX_ROUNDS + 1:
        request.status = "invalid"
        request.question = f"已经改过 {MAX_ROUNDS} 轮了，请给一个更明确的调整方向。"
        db.commit()
        db.refresh(request)
        return request

    intent = parse_intent(text, quick_action)
    request.intent = intent.to_dict()

    if intent.needs_clarification:
        request.status = "clarifying"
        request.question = intent.question or "还需要一点信息才能改。"
        db.commit()
        db.refresh(request)
        return request

    base = _current_actions(db, plan)
    operations, risks = plan_operations(intent, base)

    if not operations:
        request.status = "clarifying"
        request.question = (
            risks[0] if risks else "按你的描述暂时没有需要改的地方，可以换个说法。"
        )
        db.commit()
        db.refresh(request)
        return request

    proposed = _apply_operations(base, operations)

    # 1) 通用约束（受伤部位 / 力量日）
    problems = violates_constraints(proposed, intent)

    # 2) 膝痛专用安全门：**只有确实提到膝盖时才跑**。
    #    否则「想加点强度」这种请求会被膝痛规则误伤 —— 它跟膝盖无关。
    if "knee" in intent.body_parts:
        problems += _safety_check(proposed, intent.available_days or 7)

    if problems:
        request.status = "invalid"
        request.question = "；".join(problems)
        db.commit()
        db.refresh(request)
        return request

    diff = _diff(base, proposed, required_changes=len(operations))
    if intent.available_days:
        diff["available_days"] = intent.available_days
    else:
        # 没明说天数：用当前计划的力量日数当上限，审批时重跑同一道门
        diff["available_days"] = len(
            {
                a["day_index"]
                for a in base
                if a["action_type"] == "exercise" and a["slot"] == "strength"
            }
        )

    patch = PlanPatch(
        plan_id=plan.id,
        base_version=plan.current_version,
        goal_id=plan.goal_id,
        constraint_ids=[],
        reason_code=intent.kind,
        reason_text=text or intent.kind,
        operations=operations,
        validation={
            "passed": True,
            "intent": intent.to_dict(),
            "risks": risks,
            "question": None,
        },
        diff=diff,
        status="pending_approval",
        requires_approval=True,
        created_by="agent",
    )
    db.add(patch)
    db.flush()

    # 上一轮的草案作废：它基于旧说法，留着只会让「待确认」列表越来越脏
    if parent_request_id:
        parent = db.get(PatchRequest, parent_request_id)
        if parent is not None and parent.patch_id:
            old = db.get(PlanPatch, parent.patch_id)
            if old is not None and old.status == "pending_approval":
                old.status = "superseded"

    request.patch_id = patch.id
    request.status = "ready"
    request.question = ""

    append_event(
        db,
        plan.id,
        "patch_created",
        {
            "patch_id": patch.id,
            "request_id": request.id,
            "intent": intent.kind,
            "changed_slots": diff.get("changed_slots"),
            "churn": diff.get("churn"),
        },
        actor_type="agent",
        actor_id="planner",
    )
    db.commit()
    db.refresh(request)
    return request


def add_feedback(
    db: Session, user: User, request_id: str, text: str
) -> PatchRequest:
    """用户说「再改改」：记下反馈，基于同一 base_version 重新规划一轮。"""
    parent = db.get(PatchRequest, request_id)
    if parent is None or parent.user_id != user.id:
        raise PatchValidationError(["request not found"])
    if parent.status not in ("ready", "clarifying", "invalid"):
        raise PatchValidationError([f"request status is {parent.status}, cannot revise"])
    if parent.round > MAX_ROUNDS:
        raise PatchValidationError(
            [f"已经改过 {MAX_ROUNDS} 轮了，请给一个更明确的调整方向"]
        )

    db.add(PatchFeedback(request_id=parent.id, user_text=text or ""))
    db.flush()

    combined = "；".join(filter(None, [parent.raw_text, text]))
    return create_request(
        db,
        user,
        parent.plan_id,
        combined,
        quick_action=parent.quick_action,
        parent_request_id=parent.id,
    )


def approve_request(
    db: Session, user: User, request_id: str, comment: str = ""
) -> tuple[PatchRequest, PlanVersion]:
    request = _owned_request(db, user, request_id)
    if request.status != "ready" or not request.patch_id:
        raise PatchValidationError([f"request status is {request.status}, cannot approve"])

    plan, version = approve_patch(db, user, request.patch_id, comment)
    request.status = "applied"
    db.commit()
    db.refresh(request)
    return request, version


def reject_request(db: Session, user: User, request_id: str, comment: str = "") -> PatchRequest:
    request = _owned_request(db, user, request_id)
    if request.patch_id:
        reject_patch(db, user, request.patch_id, comment)
    request.status = "rejected"
    db.commit()
    db.refresh(request)
    return request


def _owned_request(db: Session, user: User, request_id: str) -> PatchRequest:
    request = db.get(PatchRequest, request_id)
    if request is None or request.user_id != user.id:
        raise PatchValidationError(["request not found"])
    return request


def load_request(db: Session, user: User, request_id: str) -> PatchRequest:
    return _owned_request(db, user, request_id)


def request_payload(db: Session, request: PatchRequest) -> dict:
    """把 request + 草案补丁拼成前端要的形状。"""
    patch = db.get(PlanPatch, request.patch_id) if request.patch_id else None
    return {
        "request_id": request.id,
        "status": request.status,
        "round": request.round,
        "intent": request.intent or {},
        "question": request.question,
        "raw_text": request.raw_text,
        "parent_request_id": request.parent_request_id,
        "draft": (
            {
                "patch_id": patch.id,
                "operations": patch.operations or [],
                "summary": patch.reason_text,
                "explanation": (patch.validation or {}).get("explanation", ""),
                "risks": (patch.validation or {}).get("risks", []),
                "diff": patch.diff or {},
                "status": patch.status,
            }
            if patch is not None
            else None
        ),
        "created_at": request.created_at.isoformat() if request.created_at else None,
    }


def undo_last_change(db: Session, user: User, plan_id: str) -> PlanVersion:
    """撤销上一次提交：回滚到当前版本的父版本（也是新版本，历史不删）。"""
    plan = _load_plan(db, user, plan_id)
    version = db.scalar(
        select(PlanVersion).where(
            PlanVersion.plan_id == plan.id, PlanVersion.version == plan.current_version
        )
    )
    if version is None or version.parent_version is None:
        raise PatchValidationError(["没有可以撤销的版本"])
    return rollback_plan(db, user, plan.id, version.parent_version)

