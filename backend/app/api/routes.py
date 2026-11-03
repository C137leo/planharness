"""REST 接口层（前缀 /api/v1）。

职责边界：本层只做「鉴权 → 调领域服务 → 包响应 / 把领域异常映射成 HTTP 状态码」，
业务规则（目标计算、安全门、版本提交）全部落在 app/domain 下。

统一响应格式：{code, message, data, trace_id}，前端只需判断 code 与 data。
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from langgraph.prebuilt import interrupt
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from langgraph.types import Command

from app import config
from app.auth import get_current_user, get_db, hash_password, issue_token, verify_password
from app.domain import calories as calorie_stats
from app.domain import requests as patch_requests
from app.domain.patches import (
    PatchValidationError,
    approve_patch,
    create_knee_patch,
    reject_patch,
    rollback_plan,
)
from app.domain.planning import PlanValidationError, generate_plan as generate_plan_service
from app.llm.deepseek import DeepSeekExplainer
from app.audit import verify_stream
from app.models import (
    AgentRun,
    AuditEvent,
    Checkin,
    ContextSnapshot,
    Plan,
    PlanAction,
    PlanPatch,
    PlanVersion,
    Profile,
    User,
    WorkoutLog,
)
from app.schemas import (
    ActionOut,
    CurrentPlanOut,
    ApprovalIn,
    CheckinIn,
    CheckinOut,
    DemoLoginIn,
    PatchCreateIn,
    PatchFeedbackIn,
    PatchRequestIn,
    WorkoutLogIn,
    PatchOut,
    ProfileOut,
    RollbackIn,
    RunDetailOut,
    RunSummaryOut,
    TokenOut,
    WeChatLoginIn, RegisterIn, LoginIn,
)
from app.seed import DEMO_USER_ID

router = APIRouter(prefix="/api/v1")

def _graph_config(patch_id: str) -> dict:
    return {
        "configurable": {"thread_id": patch_id},
        "recursion_limit": 20,
    }

def ok(request: Request, data, message: str = "ok"):
    """统一成功响应包装；trace_id 由 add_trace_id 中间件写入 request.state。"""
    return {
        "code": 0,
        "message": message,
        "data": data,
        "trace_id": getattr(request.state, "trace_id", ""),
    }


@router.get("/health")
def health(request: Request, db: Session = Depends(get_db)):
    """健康检查：真正打一次库，避免"进程活着但数据库断了"的假健康。"""
    db.execute(text("SELECT 1"))
    return ok(request, {"status": "ok", "service": "planharness-api"})


@router.post("/auth/demo", response_model=None)
def demo_login(payload: DemoLoginIn, request: Request, db: Session = Depends(get_db)):
    """demo 登录：校验预置用户后签发 JWT，用于本地演示与自动化测试。"""
    user_id = payload.user_id or DEMO_USER_ID
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="demo user not found")
    # token = create_access_token(user.id, request.app.state.settings)
    token = issue_token(user, request.app.state.settings)
    return ok(request, TokenOut(access_token=token, user_id=user.id).model_dump())


@router.post("/auth/wechat")
def wechat_login(payload: WeChatLoginIn):
    """微信登录占位：没有 AppID 时明确返回 501，而不是静默失败。

    AppID 下来后在这里用 code 换 openid，再复用同一套 token 签发逻辑。
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="WeChat AppID is not configured; use /auth/demo for the demo",
    )


@router.get("/me/profile")
def my_profile(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """读取当前登录用户的画像。"""
    profile = db.get(Profile, user.id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="profile not found")
    return ok(request, ProfileOut.model_validate(profile).model_dump())


def _current_plan_payload(db: Session, user: User) -> CurrentPlanOut:
    """组装"当前计划"响应：定位 active 计划 → 取 current_version → 按天/槽位排序动作。"""
    plan = db.scalar(
        select(Plan)
        .where(Plan.user_id == user.id, Plan.status == "active")
        .order_by(Plan.created_at.desc())
    )
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no active plan")

    version = db.scalar(
        select(PlanVersion).where(
            PlanVersion.plan_id == plan.id,
            PlanVersion.version == plan.current_version,
        )
    )
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan version not found")

    # 排序保证前端渲染顺序稳定（天 → 槽位 → 序号）
    actions = db.scalars(
        select(PlanAction)
        .where(PlanAction.plan_version_id == version.id)
        .order_by(PlanAction.day_index, PlanAction.slot, PlanAction.seq)
    ).all()

    return CurrentPlanOut(
        plan_id=plan.id,
        status=plan.status,
        version=version.version,
        calorie_target=version.calorie_target,
        protein_target_g=version.protein_target_g,
        step_target=version.step_target,
        summary=version.summary,
        actions=[ActionOut.model_validate(item) for item in actions],
    )


@router.get("/plans/current")
def current_plan(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """读取当前生效版本的计划。"""
    return ok(request, _current_plan_payload(db, user).model_dump())


@router.post("/plans/generate")
def generate_plan(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """全量重新生成计划（对应 A/B 模式的"裸奔"路径），成功后版本号 +1。"""
    try:
        generate_plan_service(db, user)
    except PlanValidationError as exc:
        # 校验失败属于"用户输入/目标不合理"，用 422 而不是 500
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors,
        ) from exc
    return ok(request, _current_plan_payload(db, user).model_dump(), message="generated")


def _patch_payload(patch: PlanPatch):
    """把补丁 ORM 对象转成出参 dict。"""
    return PatchOut.model_validate(patch).model_dump()


def _owned_patch(db: Session, user: User, patch_id: str) -> PlanPatch:
    """读取补丁并校验归属；不属于当前用户时统一返回 404（不暴露资源是否存在）。"""
    patch = db.get(PlanPatch, patch_id)
    if patch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="patch not found")
    plan = db.get(Plan, patch.plan_id)
    if plan is None or plan.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="patch not found")
    return patch


@router.post("/plans/{plan_id}/patches")
def create_patch(
    plan_id: str,
    payload: PatchCreateIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建膝痛补丁：只生成待审批草稿，不写新版本（审批前不落盘计划）。"""
    if payload.reason_code != "injury":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=["MVP 只实现了 injury 补丁"],
        )
    # 同一份计划同时只允许一个待审批补丁：
    # 否则用户连点两次「生成补丁」会留下两条永远没人处理的 pending。
    existing = db.scalar(
        select(PlanPatch)
        .where(
            PlanPatch.plan_id == plan_id,
            PlanPatch.status == "pending_approval",
        )
        .order_by(PlanPatch.created_at.desc())
    )
    # 只在「请求完全一样」时才复用；参数不同就照常往下走，
    # 让安全门/校验器有机会给出 422（否则同一个计划永远改不了参数）。
    if existing is not None and (existing.diff or {}).get("available_days") == payload.available_days:
        return ok(
            request,
            _patch_payload(existing),
            message="existing_pending_patch",
        )

    try:
        settings = request.app.state.settings
        explainer = DeepSeekExplainer(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            timeout_s=8,
        )
        patch_id = str(uuid.uuid4())
        config = _graph_config(patch_id)
        try:
            out = request.app.state.patch_graph.invoke(
                {
                    "user_id": user.id,
                    "plan_id": plan_id,
                    "available_days": payload.available_days,
                    "patch_id":patch_id
                },
                config=config,
            )
        except PatchValidationError as exc:
            raise HTTPException(422, detail=exc.errors) from exc
        if "__interrupt__" not in out:
            raise HTTPException(500, detail="graph did not reach approval interrupt")

        patch = _owned_patch(db, user, patch_id)
    except PatchValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors
        ) from exc
    return ok(request, _patch_payload(patch), message="pending_approval")


@router.get("/audit/{stream_id}")
def get_audit_stream(
    stream_id: str,
    request: Request,
    limit: int = 200,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """按 stream（plan_id）读整条审计链。"""
    # 归属校验：stream 就是 plan_id，只能看自己的计划
    plan = db.get(Plan, stream_id)
    if plan is None or plan.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audit stream not found")

    limit = max(1, min(limit, 500))
    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.stream_id == stream_id)
        .order_by(AuditEvent.seq.asc())
        .limit(limit)
    ).all()
    return ok(
        request,
        [
            {
                "seq": e.seq,
                "action": e.action,
                "actor_type": e.actor_type,
                "actor_id": e.actor_id,
                "payload": e.payload,
                "payload_hash": e.payload_hash,
                "prev_hash": e.prev_hash,
                "hash": e.hash,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
    )


@router.get("/audit/{stream_id}/verify")
def verify_audit_stream(
    stream_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """重算整条链：valid / broken_at / reason。"""
    plan = db.get(Plan, stream_id)
    if plan is None or plan.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audit stream not found")
    return ok(request, verify_stream(db, stream_id).to_dict())


@router.post("/compare/knee-patch")
def compare_knee_patch_modes(
    request: Request,
    user: User = Depends(get_current_user),
):
    """跑一次 A/B/C 三模式对比（临时库，不影响调用者的计划）。

    A 裸奔：全量重生成，无安全门、无审批
    B 安全门：全量重生成 + 安全门修正 + 审批
    C 补丁：最小补丁（3/12）+ 安全门 + 审批
    """
    from app.evals.compare import run_knee_comparison

    return ok(request, run_knee_comparison())


@router.get("/evals/scenarios")
def list_eval_scenarios(
    request: Request,
    user: User = Depends(get_current_user),
):
    """可回放的评测场景清单（Console 的场景按钮用它渲染）。"""
    from app.evals.scenarios import SCENARIOS

    return ok(
        request,
        [
            {"id": s.id, "title": s.title, "description": s.description}
            for s in SCENARIOS
        ],
    )


@router.post("/evals/scenarios/{scenario_id}/run")
def run_eval_scenario(
    scenario_id: str,
    request: Request,
    user: User = Depends(get_current_user),
):
    """在服务端跑一次评测场景并返回结果。

    注意：场景自带临时数据库、mock 模型，**不会写用户的数据**；
    这里只是把「评测」这个能力暴露给 Console，方便现场演示。
    """
    from app.evals.runner import run_all as run_evals
    from app.evals.scenarios import SCENARIOS

    known = {s.id for s in SCENARIOS}
    if scenario_id not in known:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown scenario: {scenario_id}"
        )

    run = run_evals(only=[scenario_id])
    result = run.results[0]
    sample = result.sample
    return ok(
        request,
        {
            "id": result.id,
            "title": result.title,
            "passed": result.passed,
            "error": result.error,
            "latency_ms": result.latency_ms,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail} for c in result.checks
            ],
            "metrics": {
                "tokens_in": sample.tokens_in,
                "tokens_out": sample.tokens_out,
                "cost_cny": sample.cost_cny,
                "llm_fallback": sample.llm_fallback,
                "churn": sample.churn,
                "safety_violations": sample.safety_violations,
            },
        },
        message="ok" if result.passed else "failed",
    )


@router.post("/plans/{plan_id}/patch-requests")
def create_patch_request(
    plan_id: str,
    payload: PatchRequestIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """一句话改计划：解析意图 → 生成草案（或追问）。

    草案阶段不写新版本；只有 approve 才提交。
    """
    try:
        req = patch_requests.create_request(
            db,
            user,
            plan_id,
            payload.text,
            quick_action=payload.quick_action,
            base_version=payload.base_version,
        )
    except PatchValidationError as exc:
        raise HTTPException(422, detail=exc.errors) from exc
    return ok(request, patch_requests.request_payload(db, req))


@router.get("/patch-requests/{request_id}")
def get_patch_request(
    request_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        req = patch_requests.load_request(db, user, request_id)
    except PatchValidationError as exc:
        raise HTTPException(404, detail=exc.errors) from exc
    return ok(request, patch_requests.request_payload(db, req))


@router.post("/patch-requests/{request_id}/feedback")
def feedback_patch_request(
    request_id: str,
    payload: PatchFeedbackIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """「再改改」：基于同一 base_version 重新规划一轮（最多 3 轮）。"""
    try:
        req = patch_requests.add_feedback(db, user, request_id, payload.text)
    except PatchValidationError as exc:
        raise HTTPException(422, detail=exc.errors) from exc
    return ok(request, patch_requests.request_payload(db, req))


@router.post("/patch-requests/{request_id}/approve")
def approve_patch_request(
    request_id: str,
    payload: ApprovalIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """确认应用：走真实审批 + 验证 + 审计。"""
    try:
        _, version = patch_requests.approve_request(db, user, request_id, payload.comment)
    except PatchValidationError as exc:
        raise HTTPException(422, detail=exc.errors) from exc
    db.expire_all()
    plan = _current_plan_payload(db, user)
    return ok(
        request,
        {
            "request_id": request_id,
            "committed_version": version.version,
            "plan": plan.model_dump(),
        },
        message="committed",
    )


@router.post("/patch-requests/{request_id}/reject")
def reject_patch_request(
    request_id: str,
    payload: ApprovalIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        req = patch_requests.reject_request(db, user, request_id, payload.comment)
    except PatchValidationError as exc:
        raise HTTPException(422, detail=exc.errors) from exc
    return ok(request, patch_requests.request_payload(db, req), message="rejected")


@router.post("/plans/{plan_id}/undo")
def undo_plan_change(
    plan_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """撤销上一次提交：回滚到父版本（仍生成新版本，历史不删）。"""
    try:
        version = patch_requests.undo_last_change(db, user, plan_id)
    except PatchValidationError as exc:
        raise HTTPException(422, detail=exc.errors) from exc
    plan = _current_plan_payload(db, user)
    return ok(
        request,
        {"new_version": version.version, "plan": plan.model_dump()},
        message="undone",
    )


@router.get("/patches")
def list_patches(
    request: Request,
    limit: int = 20,
    status_filter: str | None = Query(default=None, alias="status"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """补丁历史列表（审批页据此判断「还有没有待审批的」）。"""
    limit = max(1, min(limit, 100))
    stmt = (
        select(PlanPatch)
        .join(Plan, Plan.id == PlanPatch.plan_id)
        .where(Plan.user_id == user.id)
    )
    if status_filter:
        stmt = stmt.where(PlanPatch.status == status_filter)
    patches = db.scalars(stmt.order_by(PlanPatch.created_at.desc()).limit(limit)).all()

    return ok(
        request,
        [
            {
                "id": p.id,
                "plan_id": p.plan_id,
                "base_version": p.base_version,
                "status": p.status,
                "reason_code": p.reason_code,
                "reason_text": p.reason_text,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "diff": p.diff or {},
            }
            for p in patches
        ],
    )


@router.get("/patches/{patch_id}")
def get_patch(
    patch_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查询补丁详情（Console 侧展示 diff 与校验结果）。"""
    return ok(request, _patch_payload(_owned_patch(db, user, patch_id)))


@router.post("/patches/{patch_id}/approve")
def approve(
    patch_id: str,
    payload: ApprovalIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """审批通过：重新跑安全门后提交新版本，并返回提交后的完整计划。"""
    patch = _owned_patch(db, user, patch_id)

    if patch.status != "pending_approval":
        raise HTTPException(409,detail=f"patch status is{patch.status}")
    try:
        out = request.app.state.patch_graph.invoke(
            Command(resume={"decision": "approve", "comment": payload.comment}),
            _graph_config(patch_id),
        )
    except PatchValidationError as exc:
        raise HTTPException(422, detail=exc.errors) from exc
    except Exception as exc:
        raise HTTPException(409, detail=f"cannot resume graph: {exc}") from exc

    # 图在 commit 节点里用另一个 Session 提交，这里重新读一遍最新状态
    db.expire_all()
    plan = _current_plan_payload(db, user)
    committed_version = int(out.get("version") or plan.version)
    verification = (out or {}).get("verification") or {}

    # 提交后验证不通过：图已经自动回滚，这里仍然不能当成功返回
    if verification and not verification.get("passed", True):
        failed = [c["name"] for c in verification.get("checks", []) if not c.get("passed")]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "post-commit verification failed",
                "status": (out or {}).get("status"),
                "rolled_back": (out or {}).get("status") == "rolled_back",
                "rollback_version": (out or {}).get("rollback_version"),
                "failed_checks": failed,
                "verification": verification,
            },
        )

    return ok(
        request,
        {
            "patch_id": patch_id,
            "committed_version": committed_version,
            "plan": plan.model_dump(),
            "verification": verification,
        },
        message="committed",
    )


@router.post("/patches/{patch_id}/reject")
def reject(
    patch_id: str,
    payload: ApprovalIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """驳回补丁：只改补丁状态并留痕，计划版本保持不变。"""
    try:
        request.app.state.patch_graph.invoke(
            Command(resume={"decision": "reject", "comment": payload.comment}),
            _graph_config(patch_id)
        )
    except PatchValidationError as exc:
        raise HTTPException(422, detail=exc.errors) from exc
    except Exception as exc:
        raise HTTPException(409, detail=f"cannot resume graph:{exc}") from exc
    db.expire_all()
    patch = _owned_patch(db, user,patch_id)
    return ok(request, _patch_payload(patch), message="rejected")


def _run_summary(run: AgentRun, db: Session | None = None) -> RunSummaryOut:
    output = run.output or {}
    plan_id = None
    patch_id = output.get("patch_id")
    if db is not None and patch_id:
        patch = db.get(PlanPatch, patch_id)
        plan_id = patch.plan_id if patch is not None else None
    return RunSummaryOut(
        id=run.id,
        kind=run.kind,
        mode=run.mode,
        status=run.status,
        stop_reason=output.get("stop_reason"),
        patch_id=patch_id,
        plan_id=plan_id,
        created_at=run.created_at,
        finished_at=run.finished_at,
        latency_ms=run.latency_ms,
        error_code=run.error_code,
    )


@router.get("/runs")
def list_runs(
    request: Request,
    limit: int = 20,
    status_filter: str | None = Query(default=None, alias="status"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """最近的 Agent 运行列表（Console 左侧列表用）。

    `status` 可按 succeeded / failed / rejected / running 过滤。
    注意参数名用 status_filter：`status` 是 fastapi 的模块名，不能覆盖。
    """
    limit = max(1, min(limit, 100))
    stmt = select(AgentRun).where(AgentRun.user_id == user.id)
    if status_filter:
        stmt = stmt.where(AgentRun.status == status_filter)
    runs = db.scalars(stmt.order_by(AgentRun.created_at.desc()).limit(limit)).all()
    return ok(request, [_run_summary(run, db).model_dump() for run in runs])


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """一次运行的全部证据：trace 时间线 + 补丁 diff + 上下文快照 + 验证结论。"""
    run = db.get(AgentRun, run_id)
    if run is None or run.user_id != user.id:
        # 不区分「不存在」和「不是你的」，避免探测
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")

    summary = _run_summary(run, db)
    output = run.output or {}
    patch = db.get(PlanPatch, output.get("patch_id")) if output.get("patch_id") else None

    snapshot = db.scalar(
        select(ContextSnapshot)
        .where(ContextSnapshot.run_id == run.id)
        .order_by(ContextSnapshot.created_at.desc())
    )

    detail = RunDetailOut(
        **summary.model_dump(),
        trace=run.trace or [],
        input=run.input or {},
        output=output,
        patch=(
            {
                "id": patch.id,
                "status": patch.status,
                "base_version": patch.base_version,
                "reason_code": patch.reason_code,
                "reason_text": patch.reason_text,
                "diff": patch.diff or {},
                "operations": patch.operations or [],
            }
            if patch is not None
            else None
        ),
        context=(
            {
                "token_count": snapshot.token_count,
                "token_budget": snapshot.token_budget,
                "dropped": snapshot.dropped or [],
                "compressed": snapshot.compressed,
                "compression_method": snapshot.compression_method,
                "sections": snapshot.sections or [],
            }
            if snapshot is not None
            else None
        ),
        verification=output.get("verification"),
    )
    return ok(request, detail.model_dump())


@router.post("/plans/{plan_id}/rollback")
def rollback(
    plan_id: str,
    payload: RollbackIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """回滚到历史版本：不回退指针，而是以旧内容"再提交一个新版本"。"""
    try:
        version = rollback_plan(db, user, plan_id, payload.target_version)
    except PatchValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors
        ) from exc
    plan = _current_plan_payload(db, user)
    return ok(
        request,
        {"rolled_back_to": payload.target_version, "new_version": version.version, "plan": plan.model_dump()},
        message="rolled_back",
    )


def _weight_for(db: Session, user: User) -> tuple[float, str]:
    """体重口径：最近一次打卡 > 画像 > 兜底 70kg（并在返回里标注来源）。"""
    last = db.scalar(
        select(Checkin)
        .where(Checkin.user_id == user.id, Checkin.weight_kg.is_not(None))
        .order_by(Checkin.date.desc())
        .limit(1)
    )
    if last is not None and last.weight_kg:
        return float(last.weight_kg), "checkin"
    profile = db.scalar(select(Profile).where(Profile.user_id == user.id))
    if profile is not None and profile.weight_kg:
        return float(profile.weight_kg), "profile"
    return 70.0, "default"


@router.post("/workout-logs")
def create_workout_log(
    payload: WorkoutLogIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """记一条训练。热量在读取时算，不落库，避免体重变了旧数据口径对不上。"""
    day = payload.date or date.today()
    log = WorkoutLog(
        user_id=user.id,
        date=day,
        action_ref=payload.action_ref,
        kind=payload.kind,
        duration_min=payload.duration_min,
        sets=payload.sets,
        reps=payload.reps,
        intensity=payload.intensity,
        note=payload.note,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    weight, source = _weight_for(db, user)
    row = calorie_stats.burn_for_log(log, weight)
    row["weight_source"] = source
    return ok(request, row, message="logged")


@router.get("/workout-logs")
def list_workout_logs(
    request: Request,
    days: int = 14,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    days = max(1, min(days, 90))
    since = date.today() - timedelta(days=days - 1)
    rows = db.scalars(
        select(WorkoutLog)
        .where(WorkoutLog.user_id == user.id, WorkoutLog.date >= since)
        .order_by(WorkoutLog.date.desc(), WorkoutLog.created_at.desc())
    ).all()
    weight, source = _weight_for(db, user)
    items = [calorie_stats.burn_for_log(row, weight) | {"weight_source": source} for row in rows]
    return ok(request, {"items": items, "days": days, "since": since.isoformat()})


@router.delete("/workout-logs/{log_id}")
def delete_workout_log(
    log_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    log = db.get(WorkoutLog, log_id)
    if log is None or log.user_id != user.id:
        raise HTTPException(404, "workout log not found")
    db.delete(log)
    db.commit()
    return ok(request, {"deleted": log_id})


@router.get("/stats/calories")
def calorie_stats_endpoint(
    request: Request,
    days: int = 7,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """只算消耗（burn_only）：训练记录 → MET → kcal，口径写在返回里。"""
    days = max(1, min(days, 90))
    since = date.today() - timedelta(days=days - 1)
    rows = db.scalars(
        select(WorkoutLog)
        .where(WorkoutLog.user_id == user.id, WorkoutLog.date >= since)
        .order_by(WorkoutLog.date.asc())
    ).all()
    weight, source = _weight_for(db, user)
    burned = [calorie_stats.burn_for_log(row, weight) for row in rows]
    data = calorie_stats.summarize(burned, weight)
    data["weight_source"] = source
    data["since"] = since.isoformat()
    data["days_window"] = days
    return ok(request, data)


@router.post("/checkins")
def create_checkin(
    payload: CheckinIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    day = payload.date or date.today()
    checkin = db.scalar(
        select(Checkin).where(Checkin.user_id == user.id, Checkin.date == day)
    )
    if checkin is None:
        checkin = Checkin(user_id=user.id, date=day)
        db.add(checkin)
    checkin.weight_kg = payload.weight_kg
    checkin.sleep_hours = payload.sleep_hours
    checkin.hunger_level = payload.hunger_level
    checkin.soreness_level = payload.soreness_level
    checkin.adherence_pct = payload.adherence_pct
    checkin.note = payload.note
    db.commit()
    db.refresh(checkin)
    return ok(request, CheckinOut.model_validate(checkin).model_dump(), message="saved")


@router.get("/checkins")
def list_checkins(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(Checkin)
        .where(Checkin.user_id == user.id)
        .order_by(Checkin.date.desc())
        .limit(14)
    ).all()
    return ok(request, [CheckinOut.model_validate(item).model_dump() for item in rows])

@router.post("/auth/register")
def register(payload: RegisterIn, request: Request, db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.phone == payload.phone)):
        raise HTTPException(409, "phone already registered")
    user = User(phone=payload.phone, nickname=payload.nickname,
                password_hash=hash_password(payload.password))
    db.add(user)
    db.flush()
    db.add(Profile(user_id=user.id))     # ★ 必须同时建画像，否则 /me/profile 404
    db.commit()
    return ok(request, TokenOut(access_token=issue_token(user, request.app.state.settings),
                                user_id=user.id).model_dump(), message="registered")

@router.post("/auth/login")
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.phone == payload.phone))
    if user is None or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "invalid credentials")   # 统一话术，别区分
    return ok(request, TokenOut(access_token=issue_token(user, request.app.state.settings),
                                user_id=user.id).model_dump())