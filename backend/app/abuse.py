"""防滥用：注册/登录的限流、二次校验挑战、密码策略。

为什么单独一层而不是写在路由里：
- **计数必须独立提交**。如果计数器跟着业务事务一起回滚，攻击者只要让请求失败
  （弱密码、重复手机号）就能无限重试，限流等于没写。
- **挑战必须服务端签名**。客户端自己报"我填了 3 秒"是伪造不了的谎言来源，
  所以时间戳由服务端签发 + HMAC，客户端只能原样带回。
- **表有上界**。所有键都带窗口前缀，过期窗口会被清理，不会随攻击量无限增长。

这里能挡住的：脚本批量注册、单 IP 撞库、暴力重试、表单机器提交。
挡不住的（要诚实）：分布式 IP 池、真人打码平台、短信轰炸——那需要短信验证码、
WAF 或风控服务，不是这一层能解决的。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuthChallenge, RateLimitBucket, User, new_id

# 中国大陆手机号：1 开头，第二位 3-9，共 11 位
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

# 常见弱口令（只放最常被撞库的那批，不做完整字典）
COMMON_PASSWORDS = {
    "12345678", "123456789", "1234567890", "password", "password1", "qwerty123",
    "11111111", "00000000", "abc12345", "iloveyou", "admin123", "test1234",
    "a1234567", "1qaz2wsx", "passw0rd", "woaini1314",
}


def normalize_phone(raw: str) -> str:
    """去掉空格/横线，方便同一号码不会被写成两个账号。"""
    return re.sub(r"[\s\-]", "", (raw or "").strip())


def password_problem(password: str, phone: str = "") -> str | None:
    """返回第一个不满足的策略；满足则返回 None。

    策略刻意保守但不过分：8-72 位、字母 + 数字、不是常见弱口令、
    不包含手机号本身。不做"必须大小写 + 符号"，那种规则带来的收益远小于
    用户把密码写在便利贴上造成的损失。
    """
    if not 8 <= len(password) <= 72:
        return "密码长度要在 8-72 位之间"
    if password.lower() in COMMON_PASSWORDS:
        return "这个密码太常见了，换一个"
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "密码要同时包含字母和数字"
    if phone and phone in password:
        return "密码里不要包含手机号"
    if len(set(password)) <= 3:
        return "密码重复字符太多，换一个"
    return None


# ---------------------------------------------------------------- 限流


@dataclass
class Decision:
    """一次限流判定的结果，直接映射到 429 响应。"""

    allowed: bool
    limit: int
    used: int
    retry_after: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


class RateLimiter:
    """固定窗口计数器，键里带窗口起点，过期窗口定期清掉。

    用独立 Session 提交：限流计数不能因为业务失败而回滚。
    """

    def __init__(self, session_factory: sessionmaker) -> None:
        self.session_factory = session_factory
        self._last_cleanup = 0.0
        self.cleanup_interval = 300.0  # 5 分钟清一次过期窗口

    def _bucket_key(self, key: str, window_start: int) -> str:
        return f"{key}@{window_start}"

    def hit(self, key: str, limit: int, window_seconds: int, *, now: float | None = None) -> Decision:
        """记一次命中并返回是否放行。now 可注入，测试里用来跨窗口。"""
        stamp = time.time() if now is None else now
        window_start = int(stamp // window_seconds * window_seconds)
        bucket_key = self._bucket_key(key, window_start)
        retry_after = int(window_start + window_seconds - stamp) + 1

        with self.session_factory() as db:
            self._maybe_cleanup(db, stamp)
            row = db.get(RateLimitBucket, bucket_key)
            if row is None:
                row = RateLimitBucket(key=bucket_key, window_start=window_start, count=1)
                db.add(row)
                try:
                    db.commit()
                except IntegrityError:
                    # 并发下另一个请求先插入了，改成自增
                    db.rollback()
                    row = db.get(RateLimitBucket, bucket_key)
                    if row is None:
                        return Decision(allowed=True, limit=limit, used=1)
                    row.count += 1
                    db.commit()
            else:
                row.count += 1
                db.commit()

            used = row.count
        return Decision(allowed=used <= limit, limit=limit, used=used,
                        retry_after=retry_after if used > limit else 0)

    def peek(self, key: str, limit: int, window_seconds: int, *,
             now: float | None = None) -> Decision:
        """只看当前窗口用了多少，不计数。

        用途：注册配额应该只统计**成功写入数据库**的请求。
        若连"提交太快""密码太弱"这种失败也扣成功配额，用户重试几次就把自己锁死了，
        攻击者反而可以用失败请求把别人的配额耗尽。
        """
        stamp = time.time() if now is None else now
        window_start = int(stamp // window_seconds * window_seconds)
        retry_after = int(window_start + window_seconds - stamp) + 1
        with self.session_factory() as db:
            row = db.get(RateLimitBucket, self._bucket_key(key, window_start))
            used = int(row.count) if row is not None else 0
        return Decision(allowed=used < limit, limit=limit, used=used,
                        retry_after=retry_after if used >= limit else 0)

    def clear(self, key: str) -> None:
        """成功登录后清掉失败计数（只清当前窗口，历史窗口不影响）。"""
        with self.session_factory() as db:
            db.execute(delete(RateLimitBucket).where(RateLimitBucket.key.like(f"{key}@%")))
            db.commit()

    def _maybe_cleanup(self, db: Session, stamp: float) -> None:
        """定期删掉过期窗口：表大小只和"活跃键"有关，不随攻击量增长。"""
        if stamp - self._last_cleanup < self.cleanup_interval:
            return
        self._last_cleanup = stamp
        horizon = datetime.fromtimestamp(stamp - 86400, tz=timezone.utc)
        db.execute(delete(RateLimitBucket).where(RateLimitBucket.window_start < int(horizon.timestamp())))
        db.execute(delete(AuthChallenge).where(AuthChallenge.expires_at < horizon))
        db.commit()


# ---------------------------------------------------------------- 二次校验挑战


@dataclass
class Challenge:
    challenge_id: str
    token: str
    expires_in: int
    min_fill_ms: int


def _sign(secret: str, challenge_id: str, purpose: str, issued_at: datetime) -> str:
    payload = f"{challenge_id}|{purpose}|{int(issued_at.timestamp() * 1000)}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:40]


def issue_challenge(session_factory: sessionmaker, settings, purpose: str, ip: str) -> Challenge:
    """签发一次性挑战：客户端必须原样带回 token，且服务端会检查"填表耗时"。"""
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=settings.auth_challenge_ttl_seconds)
    row = AuthChallenge(
        id=new_id(),
        purpose=purpose,
        ip=ip,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    with session_factory() as db:
        db.add(row)
        db.commit()
    return Challenge(
        challenge_id=row.id,
        token=_sign(settings.secret_key, row.id, purpose, issued_at),
        expires_in=settings.auth_challenge_ttl_seconds,
        min_fill_ms=settings.auth_min_fill_ms,
    )


def verify_challenge(
    session_factory: sessionmaker,
    settings,
    *,
    challenge_id: str,
    token: str,
    purpose: str,
    now: float | None = None,
) -> str | None:
    """校验挑战，失败返回原因字符串（调用方转 400/422），成功返回 None 并标记已用。"""
    if not challenge_id or not token:
        return "缺少校验票据，请刷新页面重试"

    stamp = time.time() if now is None else now
    with session_factory() as db:
        row = db.get(AuthChallenge, challenge_id)
        if row is None:
            return "校验票据无效，请刷新页面重试"
        if row.purpose != purpose:
            return "校验票据用途不匹配"
        if row.used_at is not None:
            return "校验票据已被使用，请刷新页面重试"
        if row.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            return "校验票据已过期，请刷新页面重试"
        expected = _sign(settings.secret_key, row.id, row.purpose, row.issued_at.replace(tzinfo=timezone.utc))
        if not hmac.compare_digest(expected, token):
            return "校验票据签名不正确"

        age_ms = (stamp - row.issued_at.replace(tzinfo=timezone.utc).timestamp()) * 1000
        if age_ms < settings.auth_min_fill_ms:
            # 机器提交通常几十毫秒就填完了；真人再快也很少低于 1 秒
            return "提交太快了，请稍后再试"

        row.used_at = datetime.now(timezone.utc)
        db.commit()
    return None


# ---------------------------------------------------------------- 账号总量


def user_count(session_factory: sessionmaker) -> int:
    """账号总数：数据库的硬天花板，超过就拒绝注册（保护磁盘与后续查询）。"""
    with session_factory() as db:
        return int(db.scalar(select(func.count()).select_from(User)) or 0)
