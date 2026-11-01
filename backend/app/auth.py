"""鉴权：签发/解析 JWT、请求级数据库会话、当前用户依赖。

P0 采用无状态 HS256 JWT；demo 登录与（未来的）微信登录共用同一套 token。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import User

# auto_error=False：没带 token 时不直接 401，而是交给 get_current_user 给出更明确的报错
bearer = HTTPBearer(auto_error=False)


def create_access_token(user_id: str, settings: Settings) -> str:
    """签发 access token：sub 放用户 ID，iat/exp 用于过期校验。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_access_token(token: str, settings: Settings) -> str:
    """解析 token 并返回 user_id；签名错误/过期/缺少 sub 一律 401。"""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
        ) from exc
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    return str(user_id)


def get_db(request: Request):
    """FastAPI 依赖：每个请求一个 Session，请求结束必定关闭。

    Session 工厂挂在 app.state 上，测试可以注入指向临时库的实例。
    """
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    """FastAPI 依赖：从 Authorization 头解析出当前用户。

    依次校验：带了 token → token 合法 → 用户仍存在（防止用户删除后旧 token 继续可用）。
    """
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")
    user_id = decode_access_token(credentials.credentials, request.app.state.settings)
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")
    return user
