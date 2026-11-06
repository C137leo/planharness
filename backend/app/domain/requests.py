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
from app.agent.change_agent import propose_with_model, propose_without_model
from app.domain.planner import (Intent, cover_injured_slots, parse_intent, plan_operations, trim_unrelated_ops, violates_constraints)
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
    client=None,
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

    # 红旗症状：无论改什么计划，都先就医，不在这里排训练
    from app.knowledge.service import RED_FLAGS

    red_flags = [word for word in RED_FLAGS if word in (text or "")]
    if red_flags and quick_action not in ("rest",):
        request.status = "invalid"
        request.question = (
            f"提到「{'、'.join(red_flags)}」这类症状，先暂停训练并做医疗评估；"
            "等症状明确后我们再改计划。"
        )
        db.commit()
        db.refresh(request)
        return request

    intent = parse_intent(text, quick_action)
    request.intent = {**intent.to_dict(), "source": "pending"}

    if intent.needs_clarification:
        request.status = "clarifying"
        request.question = intent.question or "还需要一点信息才能改。"
        db.commit()
        db.refresh(request)
        return request

    base = _current_actions(db, plan)

    # 先让模型试着理解"用户到底要改哪几处"；模型只提议，校验在下面。
    proposal = None
    if client is not None and getattr(client, "available", lambda: False)():
        try:
            proposal = propose_with_model(
                client, text, quick_action, base, _constraints(db, user, plan, intent)
            )
        except Exception:  # noqa: BLE001 - 模型失败退回规则表，不能让改计划整体失败
            proposal = None
    if proposal is None:
        proposal = propose_without_model(text, quick_action, base)
    # 受伤类请求：意图已经明确（部位 + 避开它），模型再追问就没有意义，
    # 直接退回规则表，避免"说了后背拉伤，它反问你到底想改什么"。
    if (
        proposal["status"] != "ready"
        and intent.kind in ("injury_stop", "injury_swap")
        and intent.body_parts
    ):
        fallback = propose_without_model(text, quick_action, base)
        if fallback["status"] == "ready":
            proposal = fallback

    request.intent = {**intent.to_dict(), "source": proposal.get("source", "rules")}

    if proposal["status"] != "ready":
        request.status = "clarifying"
        request.question = proposal.get("question") or "还需要一点信息才能改。"
        db.commit()
        db.refresh(request)
        return request

    operations = proposal["operations"]
    risks = list(proposal.get("risks") or [])

    # 「今天想休息」必须落在**今天**：模型有时会改第 1 天，用户打开今天页却毫无变化。
    if intent.kind == "rest":
        from datetime import date as _date

        weekday = _date.today().isoweekday()
        todays = [
            item for item in base
            if item["day_index"] == weekday and item["action_type"] == "exercise"
        ]
        if not todays:
            request.status = "clarifying"
            request.question = "今天本来就没有训练安排，不需要休息调整。"
            db.commit()
            db.refresh(request)
            return request
        operations = [
            {
                "op": "replace_action",
                "slot_key": item["slot_key"],
                "before_action_ref": item["action_ref"],
                "after_action": {
                    "action_ref": "stretch",
                    "duration_min": 15,
                    "sets": None,
                    "reps": None,
                    "meta": {"name_zh": "全身拉伸", "movement_mode": "mobility"},
                },
                "reason": "今天以休息为主，训练换成拉伸",
            }
            for item in todays
        ]
        risks.append("休息日仍建议保持轻度活动（散步/拉伸），避免完全静止。")
    # 模型可能"过度反应"：把跟受伤部位无关的槽位也删了。
    # 先剪掉无关改动，再补全漏掉的（一紧一松都由代码兜住）。
    operations, dropped = trim_unrelated_ops(intent, base, operations)
    if dropped:
        from app.domain.safety_rules import labels_of

        risks.append(f"已忽略 {dropped} 处与{labels_of(intent.body_parts)}无关的改动（保持最小补丁）")

    # 模型可能只改了一部分；硬约束必须被补齐（模型提议 + 代码补全）
    extra = cover_injured_slots(intent, base, operations)
    if extra:
        operations = operations + extra
        risks.append(f"已补全 {len(extra)} 处涉及受伤部位的槽位")

    proposed = _apply_operations(base, operations)

    # 1) 通用约束（受伤部位 / 力量日）
    problems = violates_constraints(proposed, intent)

    # 2) 部位安全门：只检查用户真正提到的不适部位（膝/踝/腿/髋/腰/背/胸/肩/肘/腕/手臂）。
    #    没提部位就不跑，否则「想加点强度」会被无关规则误伤。
    if intent.body_parts:
        problems += _safety_check(proposed, intent.available_days or 7,
                                  regions=intent.body_parts)

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
            # 定向修改的来源：model = 模型理解后提议（经确定性校验），rules = 规则表兜底
            "change_source": proposal.get("source", "rules"),
            "change_summary": proposal.get("summary", ""),
        },
        diff=diff,
        status="pending_approval",
        requires_approval=True,
        created_by="agent",
    )
    db.add(patch)
    db.flush()

    # 同一份计划同时只留一个待确认草案：
    # 用户连点「看看会怎么改」会堆出好几条一模一样的待确认卡片，
    # 既没法分辨该点哪个，也会让审批页变成列表墙。
    _supersede_other_pending(db, plan.id, keep_id=patch.id)

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


def _supersede_other_pending(db: Session, plan_id: str, *, keep_id: str) -> int:
    """把同一计划下其它待确认草案标成 superseded，只留最新一条。"""
    from sqlalchemy import select

    rows = db.scalars(
        select(PlanPatch).where(
            PlanPatch.plan_id == plan_id,
            PlanPatch.id != keep_id,
            PlanPatch.status == "pending_approval",
        )
    ).all()
    for row in rows:
        row.status = "superseded"
    return len(rows)


def _constraints(db: Session, user: User, plan: Plan, intent: Intent) -> dict:
    """把"模型必须遵守的硬约束"显式给它，减少无效提议。"""
    from app.models import Profile

    profile = db.get(Profile, user.id)
    profile = profile if profile is not None else None
    return {
        "allergies": list(getattr(profile, "allergies", None) or []),
        "diet_preferences": list(getattr(profile, "diet_preferences", None) or []),
        "weekly_days": int(getattr(profile, "weekly_days", 0) or 0),
        "available_days_hint": intent.available_days,
        "body_parts": list(intent.body_parts),
        "current_days": sorted({item["day_index"] for item in _current_actions(db, plan)}),
    }


def add_feedback(
    db: Session, user: User, request_id: str, text: str, *, client=None
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
        client=client,
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


def ops_for_display(operations: list[dict]) -> list[dict]:
    """草案里的操作补上中文名（前端直接显示"深蹲 → 臀桥"）。"""
    from app.domain.catalog import enrich_operations

    return enrich_operations(operations)


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
                "operations": ops_for_display(patch.operations or []),
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

