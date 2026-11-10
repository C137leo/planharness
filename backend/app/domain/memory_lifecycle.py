"""记忆生命周期维护：短期记忆过期、临时上下文清理、行为规律提炼。

核心边界：

- **只清理 Agent 记忆和临时上下文**；`WorkoutSession` / `WorkoutLog` 是真实训练
  历史，永远不在本模块的删除/归档范围内。
- 普通短期记忆到期 → `status=expired`（保留行和变更记录），不是物理删除。
- 有长期价值的信息（连续未完成、长期疲劳、重复时间压力）先提炼成
  `long_term` 的 `behavior` 记忆，再让原始短期记忆过期。
- 临时上下文只影响当天，绝不因重复出现就自动改写 Profile 的长期偏好。
- 读取接口本身也过滤过期数据，定时任务只是"日常维护"，不是正确性的唯一来源。
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import append_event
from app.domain.memory import (
    IMPORTANT_SHORT_TERM_CATEGORIES,
    SHORT_TERM,
    LONG_TERM,
    TEMPORARY_RETENTION_DAYS,
    SHORT_TERM_RETENTION_DAYS,
    record_memory_revision,
    upsert_memory,
)
from app.domain.profile_sync import reconcile_profile_and_memory
from app.domain.timeutil import as_utc, user_today
from app.models import (
    Memory,
    PlanAdjustment,
    Profile,
    TemporaryContext,
    User,
    WorkoutSession,
)

logger = logging.getLogger("planharness.memory")

ACTIVE_TEMP_STATUSES = ("active", "completed", "reset")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _expire_memory(db: Session, row: Memory, reason: str) -> bool:
    if row.status != "active":
        return False
    old_value, old_status = row.value or "", row.status
    row.status = "expired"
    row.updated_at = _now()
    db.add(row)
    record_memory_revision(
        db, row,
        old_value=old_value, new_value=row.value,
        old_status=old_status, new_status="expired",
        source="lifecycle", reason=reason,
    )
    return True


def distill_training_behavior(db: Session, user: User) -> Memory | None:
    """把真实训练历史提炼成长期行为总结；不删除任何训练记录。"""
    now = _now()
    since = now - dt.timedelta(days=90)
    sessions = list(db.scalars(
        select(WorkoutSession)
        .where(
            WorkoutSession.user_id == user.id,
            WorkoutSession.status == "finished",
            WorkoutSession.date >= since.date(),
        )
        .order_by(WorkoutSession.date.desc(), WorkoutSession.finished_at.desc())
        .limit(20)
    ).all())
    if not sessions:
        return None
    completions = [float(item.completion_pct or 0) for item in sessions]
    difficulties = [int(item.difficulty) for item in sessions if item.difficulty]
    fatigues = [int(item.fatigue) for item in sessions if item.fatigue]
    consecutive = 0
    for item in sessions:
        if float(item.completion_pct or 0) >= 60:
            break
        consecutive += 1
    avg_completion = round(sum(completions) / len(completions), 1)
    avg_difficulty = round(sum(difficulties) / len(difficulties), 1) if difficulties else None
    avg_fatigue = round(sum(fatigues) / len(fatigues), 1) if fatigues else None

    parts = [
        f"最近 {len(sessions)} 次训练平均完成率 {avg_completion:.0f}%",
    ]
    if avg_difficulty is not None:
        parts.append(f"平均难度 {avg_difficulty}/5")
    if avg_fatigue is not None:
        parts.append(f"平均疲劳 {avg_fatigue}/5")
    if consecutive >= 2:
        parts.append(f"连续 {consecutive} 次完成率偏低，建议降低训练负担")
    if avg_fatigue is not None and avg_fatigue >= 4:
        parts.append("长期疲劳趋势偏高，恢复优先")

    structured = {
        "sessions": len(sessions),
        "consecutive_incomplete": consecutive,
        "avg_completion_pct": avg_completion,
        "avg_difficulty": avg_difficulty,
        "avg_fatigue": avg_fatigue,
        "load_reduced": consecutive >= 2 or (avg_fatigue is not None and avg_fatigue >= 4),
        "window_days": 90,
        "derived_from": "workout_sessions",
        "canonical": False,
    }
    return upsert_memory(
        db, user,
        kind=LONG_TERM,
        category="behavior",
        key="training_pattern",
        value="；".join(parts),
        structured=structured,
        source="derived",
        evidence="由最近 90 天真实训练记录自动提炼",
        confidence=0.75,
        importance=0.7,
        expires_at=None,
        last_confirmed_at=as_utc(sessions[0].finished_at) or now,
        audit=False,
        reason="distill_training_behavior",
    )


def distill_temporary_patterns(
    db: Session,
    user: User,
    *,
    retention_days: int = TEMPORARY_RETENTION_DAYS,
    now: dt.datetime | None = None,
) -> list[Memory]:
    """从临时上下文中提炼重复行为，但绝不写回 Profile 长期偏好。

    例："最近 5 次有 4 次把训练压到 30 分钟内" 是一条行为规律，
    不等于"用户长期每次只练 30 分钟"；因此只写 behavior，不写 Profile。
    """
    now = as_utc(now) or _now()
    profile = db.get(Profile, user.id)
    cutoff = user_today(profile) - dt.timedelta(days=max(1, retention_days))
    rows = list(db.scalars(
        select(TemporaryContext).where(
            TemporaryContext.user_id == user.id,
            (TemporaryContext.effective_date >= cutoff)
            | ((TemporaryContext.effective_date.is_(None))
               & (TemporaryContext.day >= cutoff)),
            TemporaryContext.status.in_(ACTIVE_TEMP_STATUSES),
        )
    ).all())
    saved: list[Memory] = []
    if not rows:
        return saved

    minutes: list[int] = []
    locations: list[str] = []
    busy = 0
    discomfort: Counter[str] = Counter()
    for row in rows:
        value = row.value or {}
        if row.key == "available_minutes" and value.get("minutes"):
            try:
                minutes.append(int(value["minutes"]))
            except (TypeError, ValueError):
                pass
        elif row.key == "location":
            loc = str(value.get("location") or "")
            if loc:
                locations.append(loc)
        elif row.key == "equipment_busy" and value.get("busy"):
            busy += 1
        elif row.key == "discomfort":
            for region in value.get("regions") or []:
                discomfort[str(region)] += 1

    if len(minutes) >= 3:
        median = int(statistics.median(minutes))
        if median <= 35:
            saved.append(upsert_memory(
                db, user,
                kind=LONG_TERM, category="behavior", key="time_pressure_pattern",
                value=(
                    f"最近 {len(minutes)} 次临时缩短训练时间（中位 {median} 分钟）；"
                    "这是时间压力规律，不等于长期单次时长偏好"
                ),
                structured={"occurrences": len(minutes), "median_minutes": median,
                            "window_days": retention_days, "canonical": False},
                source="derived", evidence="由临时上下文重复出现提炼",
                confidence=0.7, importance=0.6, expires_at=None, audit=False,
                reason="distill_time_pressure",
            ))
    if locations:
        common, count = Counter(locations).most_common(1)[0]
        if count >= 3:
            saved.append(upsert_memory(
                db, user,
                kind=LONG_TERM, category="behavior", key="location_pattern",
                value=f"最近 {count} 次临时改到 {common} 训练；尚未自动修改长期训练地点",
                structured={"location": common, "occurrences": count,
                            "window_days": retention_days, "canonical": False},
                source="derived", evidence="由临时上下文重复出现提炼",
                confidence=0.65, importance=0.5, expires_at=None, audit=False,
                reason="distill_location_pattern",
            ))
    if busy >= 3:
        saved.append(upsert_memory(
            db, user,
            kind=LONG_TERM, category="behavior", key="equipment_conflict_pattern",
            value=f"最近 {busy} 次遇到器械被占用，备选徒手动作可能更实用",
            structured={"occurrences": busy, "window_days": retention_days,
                        "canonical": False},
            source="derived", evidence="由临时上下文重复出现提炼",
            confidence=0.6, importance=0.5, expires_at=None, audit=False,
            reason="distill_equipment_pattern",
        ))
    for region, count in discomfort.items():
        if count >= 2:
            saved.append(upsert_memory(
                db, user,
                kind=LONG_TERM, category="behavior", key=f"discomfort_{region}",
                value=f"近期 {count} 次提到 {region} 不适；安排动作时应持续避开或降负荷",
                structured={"region": region, "occurrences": count,
                            "window_days": retention_days, "canonical": False},
                source="derived", evidence="由临时上下文重复出现提炼",
                confidence=0.6, importance=0.7, expires_at=None, audit=False,
                reason="distill_discomfort_pattern",
            ))
    return saved


def cleanup_short_term_memories(
    db: Session,
    user: User,
    *,
    retention_days: int = SHORT_TERM_RETENTION_DAYS,
    now: dt.datetime | None = None,
) -> dict:
    """普通短期记忆到期归档；真实训练记录不在查询范围内。"""
    now = as_utc(now) or _now()
    cutoff = now - dt.timedelta(days=max(1, retention_days))
    rows = list(db.scalars(
        select(Memory).where(
            Memory.user_id == user.id,
            Memory.kind == SHORT_TERM,
            Memory.status == "active",
        )
    ).all())
    expired = 0
    distilled = 0
    for row in rows:
        expires = as_utc(row.expires_at)
        updated = as_utc(row.updated_at or row.created_at) or now
        if expires is not None:
            due = expires <= now
        else:
            due = updated <= cutoff
        if not due:
            continue
        # 有长期价值的短期记忆先提炼，再过期
        if row.category in IMPORTANT_SHORT_TERM_CATEGORIES or float(row.importance or 0) >= 0.7:
            if row.category in ("training_state", "session"):
                if distill_training_behavior(db, user) is not None:
                    distilled += 1
            else:
                # 普通高重要度条目转成 behavior 摘要，保留原值作为证据
                upsert_memory(
                    db, user,
                    kind=LONG_TERM, category="behavior",
                    key=f"distilled_{row.category}_{row.key}"[:64],
                    value=f"从短期记忆提炼：{row.value}",
                    structured={"source_memory_id": row.id, "canonical": False},
                    source="derived", evidence=row.evidence or row.value,
                    confidence=min(1.0, float(row.confidence or 0.7)),
                    importance=0.65, expires_at=None, audit=False,
                    reason="distill_short_term",
                )
                distilled += 1
        if _expire_memory(db, row, f"short_term_retention_{retention_days}d"):
            expired += 1
    return {"expired": expired, "distilled": distilled}


def _expire_temporary(db: Session, row: TemporaryContext, reason: str) -> bool:
    if row.status not in ACTIVE_TEMP_STATUSES:
        return False
    row.status = "expired"
    row.updated_at = _now()
    db.add(row)
    return True


def cleanup_temporary_contexts(
    db: Session,
    user: User,
    *,
    retention_days: int = TEMPORARY_RETENTION_DAYS,
    now: dt.datetime | None = None,
    distill_patterns: bool = True,
) -> dict:
    """保留最近 N 天，把更早或已失效的临时上下文标 expired（不删除）。

    distill_patterns=True 时先提炼重复行为，再清理原始上下文；这也是
    "先提炼、再清理"的默认路径。
    """
    now = as_utc(now) or _now()
    distilled = distill_temporary_patterns(
        db, user, retention_days=retention_days, now=now,
    ) if distill_patterns else []
    profile = db.get(Profile, user.id)
    today = user_today(profile)
    cutoff = today - dt.timedelta(days=max(1, retention_days))
    rows = list(db.scalars(
        select(TemporaryContext).where(
            TemporaryContext.user_id == user.id,
            TemporaryContext.status.in_(ACTIVE_TEMP_STATUSES),
        )
    ).all())
    expired = 0
    for row in rows:
        effective = row.effective_date or row.day
        expires = as_utc(row.expires_at)
        too_old = effective < cutoff
        stale = expires is not None and expires <= now and effective < today
        if not (too_old or stale):
            continue
        if _expire_temporary(db, row, "temporary_retention"):
            expired += 1
    return {
        "expired": expired,
        "distilled": len(distilled),
        "cutoff": cutoff.isoformat(),
        "today": today.isoformat(),
    }


def reconcile_profiles(db: Session, users: list[User]) -> dict:
    synced = 0
    errors = 0
    for user in users:
        try:
            synced += reconcile_profile_and_memory(db, user)
        except Exception:  # noqa: BLE001
            errors += 1
            logger.exception("profile/memory reconcile failed user=%s", user.id)
    return {"synced": synced, "errors": errors}


def run_lifecycle_cleanup(
    db: Session,
    *,
    now: dt.datetime | None = None,
    short_term_retention_days: int = SHORT_TERM_RETENTION_DAYS,
    temporary_retention_days: int = TEMPORARY_RETENTION_DAYS,
) -> dict:
    """执行一次完整维护；异常按用户隔离，不阻断其它用户。"""
    current = as_utc(now) or _now()
    users = list(db.scalars(select(User)).all())
    totals = {
        "users": len(users),
        "short_term_expired": 0,
        "short_term_distilled": 0,
        "temporary_expired": 0,
        "temporary_distilled": 0,
        "profiles_synced": 0,
        "errors": 0,
    }
    for user in users:
        try:
            with db.begin_nested():
                profile = db.get(Profile, user.id)
                if profile is None:
                    continue
                short = cleanup_short_term_memories(
                    db, user, retention_days=short_term_retention_days, now=current,
                )
                temp = cleanup_temporary_contexts(
                    db, user, retention_days=temporary_retention_days, now=current,
                )
                reconcile_profile_and_memory(db, user)
                totals["short_term_expired"] += short["expired"]
                totals["short_term_distilled"] += short["distilled"]
                totals["temporary_expired"] += temp["expired"]
                totals["temporary_distilled"] += temp.get("distilled", 0)
                totals["profiles_synced"] += 1
            # 每个用户单独提交：某个用户失败时不会拖垮已完成的用户
            db.commit()
        except Exception:  # noqa: BLE001 - 单个用户失败不能拖垮其它用户
            totals["errors"] += 1
            db.rollback()
            logger.exception("memory lifecycle failed for user=%s", user.id)
    try:
        append_event(
            db,
            "system:memory",
            "memory_lifecycle_run",
            {
                **totals,
                "short_term_retention_days": short_term_retention_days,
                "temporary_retention_days": temporary_retention_days,
            },
            actor_type="system",
            actor_id="memory_lifecycle",
        )
    except Exception:  # noqa: BLE001
        totals["errors"] += 1
        logger.exception("failed to write memory lifecycle audit event")
    db.commit()
    logger.info("memory lifecycle done: %s", totals)
    return totals


def run_lifecycle_with_factory(session_factory, **kwargs) -> dict:
    """给后台任务/脚本用：独立 session，异常显式记录。"""
    try:
        with session_factory() as db:
            return run_lifecycle_cleanup(db, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.exception("memory lifecycle background run failed")
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
