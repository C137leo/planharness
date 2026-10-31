from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth import create_access_token, get_current_user, get_db
from app.models import Plan, PlanAction, PlanVersion, Profile, User
from app.schemas import (
    ActionOut,
    CurrentPlanOut,
    DemoLoginIn,
    ProfileOut,
    TokenOut,
    WeChatLoginIn,
)
from app.seed import DEMO_USER_ID

router = APIRouter(prefix="/api/v1")


def ok(request: Request, data, message: str = "ok"):
    return {
        "code": 0,
        "message": message,
        "data": data,
        "trace_id": getattr(request.state, "trace_id", ""),
    }


@router.get("/health")
def health(request: Request, db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return ok(request, {"status": "ok", "service": "planharness-api"})


@router.post("/auth/demo", response_model=None)
def demo_login(payload: DemoLoginIn, request: Request, db: Session = Depends(get_db)):
    user_id = payload.user_id or DEMO_USER_ID
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="demo user not found")
    token = create_access_token(user.id, request.app.state.settings)
    return ok(request, TokenOut(access_token=token, user_id=user.id).model_dump())


@router.post("/auth/wechat")
def wechat_login(payload: WeChatLoginIn):
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
    profile = db.get(Profile, user.id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="profile not found")
    return ok(request, ProfileOut.model_validate(profile).model_dump())


@router.get("/plans/current")
def current_plan(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
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

    actions = db.scalars(
        select(PlanAction)
        .where(PlanAction.plan_version_id == version.id)
        .order_by(PlanAction.day_index, PlanAction.slot, PlanAction.seq)
    ).all()

    data = CurrentPlanOut(
        plan_id=plan.id,
        status=plan.status,
        version=version.version,
        calorie_target=version.calorie_target,
        protein_target_g=version.protein_target_g,
        step_target=version.step_target,
        summary=version.summary,
        actions=[ActionOut.model_validate(item) for item in actions],
    )
    return ok(request, data.model_dump())
