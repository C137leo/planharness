"""REST 接口层（前缀 /api/v1）。

职责边界：本层只做「鉴权 → 调领域服务 → 包响应 / 把领域异常映射成 HTTP 状态码」，
业务规则（目标计算、安全门、版本提交）全部落在 app/domain 下。

统一响应格式：{code, message, data, trace_id}，前端只需判断 code 与 data。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth import get_current_user, get_db, hash_password, issue_token, verify_password
from app.domain.patches import (
    PatchValidationError,
    approve_patch,
    create_knee_patch,
    reject_patch,
    rollback_plan,
)
from app.domain.planning import PlanValidationError, generate_plan as generate_plan_service
from app.llm.deepseek import DeepSeekExplainer
from app.models import Checkin, Plan, PlanAction, PlanPatch, PlanVersion, Profile, User
from app.schemas import (
    ActionOut,
    CurrentPlanOut,
    ApprovalIn,
    CheckinIn,
    CheckinOut,
    DemoLoginIn,
    PatchCreateIn,
    PatchOut,
    ProfileOut,
    RollbackIn,
    TokenOut,
    WeChatLoginIn, RegisterIn, LoginIn,
)
from app.seed import DEMO_USER_ID

router = APIRouter(prefix="/api/v1")


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
    try:
        settings = request.app.state.settings
        explainer = DeepSeekExplainer(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            timeout_s=8,
        )
        patch = create_knee_patch(
            db,
            user,
            plan_id,
            available_days=payload.available_days,
            idempotency_key=payload.idempotency_key,
            explainer=explainer.explain,
        )
    except PatchValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors
        ) from exc
    return ok(request, _patch_payload(patch), message="pending_approval")


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
    try:
        _, version = approve_patch(db, user, patch_id, payload.comment)
    except PatchValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors
        ) from exc
    plan = _current_plan_payload(db, user)
    return ok(
        request,
        {"patch_id": patch_id, "committed_version": version.version, "plan": plan.model_dump()},
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
        patch = reject_patch(db, user, patch_id, payload.comment)
    except PatchValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors
        ) from exc
    return ok(request, _patch_payload(patch), message="rejected")


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