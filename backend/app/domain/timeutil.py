"""用户时区与"今天"的统一口径。

项目里所有业务日期（今天只有 20 分钟、今天的训练、临时上下文过期）都应按
**用户所在时区**判断，而不是服务器进程时区。服务器的 UTC / 本地时区在部署后
可能和用户不一致，尤其是跨时区或容器默认 UTC 的场景。

约定：
- 数据库时间统一存 UTC（aware datetime）；
- `effective_date` / `day` 存用户本地日期（date）；
- 一天的边界用本地 00:00 换算成 UTC；
- 用户没有画像或 timezone 非法时回退 Asia/Shanghai。
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models import Profile

DEFAULT_TIMEZONE = "Asia/Shanghai"


def user_timezone(profile: Profile | None) -> ZoneInfo:
    raw = str(getattr(profile, "timezone", "") or DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def user_now(profile: Profile | None = None) -> dt.datetime:
    return dt.datetime.now(user_timezone(profile))


def user_today(profile: Profile | None = None) -> dt.date:
    return user_now(profile).date()


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite 读回来会丢时区：把 naive 值按 UTC 解释，避免比较时炸。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def day_start_utc(day: dt.date, profile: Profile | None = None) -> dt.datetime:
    tz = user_timezone(profile)
    local = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    return local.astimezone(dt.timezone.utc)


def day_end_utc(day: dt.date, profile: Profile | None = None) -> dt.datetime:
    """本地这一天的结束（次日 00:00 UTC），用于临时上下文失效。"""
    return day_start_utc(day + dt.timedelta(days=1), profile)


def iso(value: dt.datetime | None) -> str:
    value = as_utc(value)
    return value.isoformat(timespec="seconds") if value else ""
