"""鉴权：签发/解析 JWT、请求级数据库会话、当前用户依赖。

P0 采用无状态 HS256 JWT，三种身份入口共用同一套 token：
- `/auth/demo`：预置账号，只用于演示与自动化测试；
- `/auth/register` + `/auth/login`：手机号 + 密码（scrypt 哈希）；
- `/auth/wechat`：待接入（当前返回 501）。

「无状态」带来的代价是 token 无法撤销，这里用 `users.token_version` 兜底：
改密码时把版本号 +1，所有旧 token 的 `ver` claim 立刻对不上 → 401。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import User

# auto_error=False：没带 token 时不直接 401，而是交给 get_current_user 给出更明确的报错
bearer = HTTPBearer(auto_error=False)


def issue_token(user: User, settings: Settings) -> str:
    """签发 access token：sub=用户 ID，ver=令牌版本（改密后 +1，旧 token 立即失效）。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
        "ver": user.token_version,
        "typ": "access",  # 将来有 refresh token 时用于区分
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_access_token(token: str, settings: Settings) -> dict:
    """验签 + 验过期，返回**完整 claims**（调用方自己取 sub / ver）。

    签名错误、过期、格式损坏都抛 401；故意用同一句 detail，不向调用方泄露差异。
    """
    try:
        return jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
        ) from exc


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

    依次校验四件事：
    1. 带了 token；
    2. 签名有效且未过期；
    3. 用户仍然存在（防止账号被删后旧 token 继续可用）；
    4. token 里的 `ver` 等于库里的 `token_version`（对不上说明已被吊销）。
    """
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")

    claims = decode_access_token(credentials.credentials, request.app.state.settings)
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")

    user = db.get(User, str(user_id))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")

    # 默认值 0 是故意的：老 token 没有 ver 字段，配上 token_version=0 仍可通过
    if int(claims.get("ver", 0)) != user.token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token revoked, please login again",
        )
    return user


def hash_password(password: str, *, n: int = 2 ** 14, r: int = 8, p: int = 1) -> str:
    """scrypt 加盐哈希；参数随结果一起存，将来提高强度也能兼容老数据。

    存储格式：`scrypt$n$r$p$salt_b64$hash_b64`，零第三方依赖（标准库实现）。
    """
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        n, r, p,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """校验密码；任何格式异常都按「不匹配」处理，绝不抛到接口层。"""
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p),
            dklen=len(base64.b64decode(hash_b64)),
        )
    except (ValueError, TypeError):
        return False
    # 用常数时间比较，避免通过响应时间反推密码
    return hmac.compare_digest(dk, base64.b64decode(hash_b64))
