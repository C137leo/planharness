"""系统级开关：大模型总闸与维护提示。

只有运营手机号能改；所有用户都能读公开状态，前端用它显示维护提示。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.abuse import normalize_phone
from app.api.routes import get_current_user, get_db, ok
from app.audit import append_event
from app.llm.guard import PAUSED_MESSAGE, is_llm_paused, set_llm_paused
from app.models import User

router = APIRouter(prefix="/api/v1")


class LlmSwitchIn(BaseModel):
    paused: bool


def _is_admin(user: User, request: Request) -> bool:
    expected = normalize_phone(request.app.state.settings.llm_admin_phone)
    return bool(user.phone and expected and normalize_phone(user.phone) == expected)


def _admin(user: User, request: Request) -> None:
    if not _is_admin(user, request):
        raise HTTPException(403, "只有运营账号可以操作这个开关")


@router.get("/system/status")
def system_status(request: Request, db: Session = Depends(get_db)):
    """公开状态：登录页也能读；只暴露是否暂停和维护文案。"""
    paused = is_llm_paused(db)
    return ok(request, {
        "paused": paused,
        "message": PAUSED_MESSAGE if paused else "",
    })


@router.get("/admin/llm-switch")
def get_llm_switch(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin(user, request)
    return ok(request, {
        "paused": is_llm_paused(db),
        "message": PAUSED_MESSAGE,
    })


@router.put("/admin/llm-switch")
def set_llm_switch(
    payload: LlmSwitchIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """paused=true 停止所有 DeepSeek 请求；false 恢复。"""
    _admin(user, request)
    set_llm_paused(db, payload.paused, updated_by=user.id)
    append_event(
        db, "system:llm",
        "llm_paused" if payload.paused else "llm_resumed",
        {"paused": payload.paused, "message": PAUSED_MESSAGE if payload.paused else ""},
        actor_type="user", actor_id=user.id,
    )
    db.commit()
    return ok(request, {
        "paused": payload.paused,
        "message": PAUSED_MESSAGE if payload.paused else "",
    })
