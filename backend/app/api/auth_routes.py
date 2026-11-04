"""注册 / 登录：带防滥用的一层。

路由里只做编排，所有判定都落在 `app/abuse.py`：
限流 → 票据校验 → 密码策略 → 唯一性 → 落库。

一个刻意的顺序：**先花调限额，再验票据，最后才查库**。
这样攻击者无论怎么失败，成本都记在限流器上；数据库只承担"通过限流的那部分"请求。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.abuse import (
    PHONE_RE,
    RateLimiter,
    issue_challenge,
    normalize_phone,
    password_problem,
    user_count,
    verify_challenge,
)
from app.api.routes import get_current_user, get_db, ok
from app.auth import hash_password, issue_token, verify_password
from app.models import Profile, User
from app.schemas import ChallengeOut, LoginIn, MeOut, RegisterIn, TokenOut

router = APIRouter(prefix="/api/v1")

PURPOSES = ("register", "login")


def client_ip(request: Request) -> str:
    """取客户端 IP。

    只有显式配置 `TRUST_PROXY=true` 时才信 X-Forwarded-For：
    否则任何客户端都能伪造这个头绕过 IP 限流。
    """
    settings = request.app.state.settings
    if getattr(settings, "trust_proxy", False):
        forwarded = request.headers.get("x-forwarded-for") or ""
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def limiter_for(request: Request) -> RateLimiter:
    return RateLimiter(request.app.state.session_factory)


def _too_many(retry_after: int, message: str) -> HTTPException:
    """429 一定带 Retry-After：前端据此倒计时，而不是盲目重试。"""
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=message,
        headers={"Retry-After": str(max(1, retry_after))},
    )


def _seconds_until(now: float, minutes: int) -> int:
    return minutes * 60


@router.get("/auth/challenge")
def auth_challenge(request: Request, response: Response, purpose: str = "register"):
    """签发一次性票据。

    限流也作用于这里：没有票据就没法注册/登录，所以这里本身也会被刷，
    按 IP 每分钟 30 次封顶。
    """
    if purpose not in PURPOSES:
        raise HTTPException(422, f"purpose must be one of {PURPOSES}")

    ip = client_ip(request)
    decision = limiter_for(request).hit(f"challenge:ip:{ip}", 30, 60)
    if not decision.allowed:
        raise _too_many(decision.retry_after, "请求太频繁，稍后再试")

    challenge = issue_challenge(
        request.app.state.session_factory, request.app.state.settings, purpose, ip
    )
    response.headers["Cache-Control"] = "no-store"
    return ok(request, ChallengeOut(
        challenge_id=challenge.challenge_id,
        token=challenge.token,
        expires_in=challenge.expires_in,
        min_fill_ms=challenge.min_fill_ms,
    ).model_dump())


@router.post("/auth/register")
def register(payload: RegisterIn, request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    limiter = limiter_for(request)
    ip = client_ip(request)
    phone = normalize_phone(payload.phone)

    if not phone:
        raise HTTPException(422, "手机号格式不正确")
    # 注册入口严格校验中国大陆手机号；登录只做归一化，避免老账号被拦
    if not PHONE_RE.match(phone):
        raise HTTPException(422, "手机号格式不正确（11 位中国大陆号码）")

    # 0) 蜜罐：真人看不到这个字段，脚本会把所有 input 填满
    if (payload.company or "").strip():
        limiter.hit(f"register:honeypot:{ip}", 3, 3600)
        raise HTTPException(400, "请求无效")

    # 1) 全站熔断 + 账号总量硬上限（保护数据库本身）
    if user_count(request.app.state.session_factory) >= settings.max_users:
        raise HTTPException(503, "演示环境已满，暂时无法注册")

    # 2) 限流：三层，任何一层超限都直接 429，不再查库
    global_decision = limiter.hit("register:global", settings.register_global_hour, 3600)
    if not global_decision.allowed:
        raise _too_many(global_decision.retry_after, "注册通道繁忙，请稍后再试")

    for key, limit, window in (
        (f"register:ip:{ip}", settings.register_per_ip_hour, 3600),
        (f"register:ipday:{ip}", settings.register_per_ip_day, 86400),
        (f"register:phone:{phone}", settings.register_per_phone_day, 86400),
    ):
        decision = limiter.hit(key, limit, window)
        if not decision.allowed:
            raise _too_many(decision.retry_after, "注册太频繁，请稍后再试")

    # 3) 票据：证明这一次提交来自真实页面，且不是机器瞬时填表
    challenge_error = verify_challenge(
        request.app.state.session_factory,
        settings,
        challenge_id=payload.challenge_id,
        token=payload.challenge_token,
        purpose="register",
    )
    if challenge_error:
        raise HTTPException(400, challenge_error)

    # 4) 密码策略
    problem = password_problem(payload.password, phone)
    if problem:
        raise HTTPException(422, problem)

    # 5) 唯一性（放在最后：前面都过了才值得查库）
    if db.scalar(select(User).where(User.phone == phone)):
        raise HTTPException(409, "手机号已注册")

    user = User(
        phone=phone,
        nickname=(payload.nickname or "新用户").strip()[:64] or "新用户",
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.flush()
    db.add(Profile(user_id=user.id))  # 必须同时建画像，否则 /me/profile 404
    db.commit()

    return ok(
        request,
        TokenOut(
            access_token=issue_token(user, settings), user_id=user.id
        ).model_dump(),
        message="registered",
    )


@router.post("/auth/login")
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)):
    settings = request.app.state.settings
    limiter = limiter_for(request)
    ip = client_ip(request)
    phone = normalize_phone(payload.phone)

    # 先看锁：同 IP + 同手机号的失败次数，超过阈值就锁一段时间
    lock_key = f"login:fail:{ip}:{phone}"
    decision = limiter.hit(lock_key, settings.login_fail_per_window,
                           settings.login_window_minutes * 60)
    if not decision.allowed:
        raise _too_many(
            decision.retry_after,
            f"失败次数过多，请 {settings.login_lock_minutes} 分钟后再试",
        )

    user = db.scalar(select(User).where(User.phone == phone))
    if user is None or not user.password_hash or not verify_password(payload.password, user.password_hash):
        # 统一话术，不区分"没这个号"和"密码错"
        raise HTTPException(401, "手机号或密码不正确")

    limiter.clear(lock_key)
    return ok(
        request,
        TokenOut(access_token=issue_token(user, settings), user_id=user.id).model_dump(),
    )


@router.get("/auth/me")
def whoami(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """当前登录账号（前端在"我的"页展示手机号 / 昵称）。"""
    profile = db.get(Profile, user.id)
    return ok(request, MeOut(
        user_id=user.id,
        phone=user.phone,
        nickname=user.nickname,
        goal=(profile.goal if profile is not None else ""),
        has_profile=profile is not None,
    ).model_dump())
