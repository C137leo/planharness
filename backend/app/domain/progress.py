"""进展洞察：从真实训练记录生成「教练发现」。

只使用 WorkoutSession / WorkoutLog / Checkin 的事实，不调用模型、不编造。
前端点击某条发现后，把 `suggested_prompt` 送进统一 Agent Workspace，
由 Agent 再决定是否调整计划/今日训练。
"""

from __future__ import annotations

import datetime as dt
from statistics import mean

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Checkin, User, WorkoutLog, WorkoutSession

FINDING_LABELS = {
    "missed": "完成度",
    "fatigue": "疲劳",
    "gap": "训练间隔",
    "progress": "进步",
    "empty": "开始",
    "consistency": "坚持",
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _sessions(db: Session, user: User, days: int) -> list[WorkoutSession]:
    since = dt.date.today() - dt.timedelta(days=max(1, days) - 1)
    return list(db.scalars(
        select(WorkoutSession)
        .where(
            WorkoutSession.user_id == user.id,
            WorkoutSession.status == "finished",
            WorkoutSession.date >= since,
        )
        .order_by(WorkoutSession.date.desc(), WorkoutSession.finished_at.desc())
    ).all())


def _logs(db: Session, user: User, days: int) -> list[WorkoutLog]:
    since = dt.date.today() - dt.timedelta(days=max(1, days) - 1)
    return list(db.scalars(
        select(WorkoutLog)
        .where(WorkoutLog.user_id == user.id, WorkoutLog.date >= since)
        .order_by(WorkoutLog.date.desc(), WorkoutLog.created_at.desc())
    ).all())


def build_progress_insights(db: Session, user: User, *, days: int = 14) -> dict:
    days = max(7, min(90, int(days)))
    sessions = _sessions(db, user, days)
    logs = _logs(db, user, days)
    completions = [float(item.completion_pct or 0) for item in sessions]
    difficulties = [int(item.difficulty) for item in sessions if item.difficulty]
    fatigues = [int(item.fatigue) for item in sessions if item.fatigue]
    durations = [int(item.actual_minutes or 0) for item in sessions]

    consecutive = 0
    for item in sessions:
        if float(item.completion_pct or 0) >= 60:
            break
        consecutive += 1

    avg_completion = round(mean(completions), 1) if completions else 0.0
    avg_difficulty = round(mean(difficulties), 1) if difficulties else None
    avg_fatigue = round(mean(fatigues), 1) if fatigues else None
    total_minutes = sum(durations)
    today = dt.date.today()
    last_date = sessions[0].date if sessions else None
    gap_days = (today - last_date).days if last_date else None

    findings: list[dict] = []
    if not sessions:
        findings.append({
            "id": "empty",
            "category": "empty",
            "title": "还没有完成一次训练",
            "detail": "先完成一场最小训练，教练才能根据你的真实完成情况调整后面的安排。",
            "evidence": "最近没有已完成的 WorkoutSession",
            "suggested_prompt": "我想开始训练，先给我安排一个 20 分钟就能完成的入门训练",
            "tone": "info",
        })
    else:
        if consecutive >= 2:
            findings.append({
                "id": "missed",
                "category": "missed",
                "title": f"连续 {consecutive} 次训练没有完成",
                "detail": "最近两次完成度偏低，继续按原训练量安排可能越来越难坚持。建议让教练把接下来一周的训练量降一档。",
                "evidence": f"最近 {len(sessions)} 次平均完成率 {avg_completion:.0f}%，连续 {consecutive} 次低于 60%",
                "suggested_prompt": "最近连续几次没完成训练，帮我调整一下下一周的训练量",
                "tone": "warn",
            })
        if avg_fatigue is not None and avg_fatigue >= 4:
            findings.append({
                "id": "fatigue",
                "category": "fatigue",
                "title": "近期训练疲劳偏高",
                "detail": "平均疲劳达到 4/5，恢复可能跟不上。下一次训练优先保证动作质量，必要时减组或增加休息。",
                "evidence": f"最近 {len(sessions)} 次平均疲劳 {avg_fatigue}/5，平均难度 {avg_difficulty or '—'}/5",
                "suggested_prompt": "最近训练有点累，帮我把接下来的训练强度调低一点",
                "tone": "warn",
            })
        if avg_completion < 70 and consecutive < 2:
            findings.append({
                "id": "consistency",
                "category": "consistency",
                "title": "完成率还有提升空间",
                "detail": "最近平均完成率低于 70%，可以先从缩短单次时长或减少动作数量开始。",
                "evidence": f"最近 {len(sessions)} 次平均完成率 {avg_completion:.0f}%",
                "suggested_prompt": "最近训练总是做不完，帮我把每次训练缩短到更容易完成的长度",
                "tone": "info",
            })
        if gap_days is not None and gap_days >= 7:
            findings.append({
                "id": "gap",
                "category": "gap",
                "title": f"已经 {gap_days} 天没有训练记录",
                "detail": "不用一次补回来，先安排一场低门槛训练重新启动。",
                "evidence": f"最近一次完成训练是 {last_date.isoformat()}",
                "suggested_prompt": "我有一段时间没练了，帮我安排一个重新开始的训练",
                "tone": "info",
            })
        if avg_completion >= 90 and (avg_difficulty is None or avg_difficulty <= 2) and len(sessions) >= 3:
            findings.append({
                "id": "progress",
                "category": "progress",
                "title": "完成得很稳，可以考虑进阶",
                "detail": "最近完成率高、难度不高，可以试着增加一组或稍微提高训练量，但只加一个变量。",
                "evidence": f"最近 {len(sessions)} 次平均完成率 {avg_completion:.0f}%，平均难度 {avg_difficulty or '—'}/5",
                "suggested_prompt": "最近训练完成得比较轻松，帮我把下一周的训练稍微加一点强度",
                "tone": "good",
            })

    # 训练趋势（按天聚合，前端可画折线/柱状）
    by_date: dict[str, dict] = {}
    for item in sessions:
        key = item.date.isoformat()
        row = by_date.setdefault(key, {"date": key, "sessions": 0, "minutes": 0, "completion": []})
        row["sessions"] += 1
        row["minutes"] += int(item.actual_minutes or 0)
        row["completion"].append(float(item.completion_pct or 0))
    trend = [
        {"date": key, "sessions": row["sessions"], "minutes": row["minutes"],
         "completion_pct": round(mean(row["completion"]), 1) if row["completion"] else 0}
        for key, row in sorted(by_date.items())
    ]

    checkins = list(db.scalars(
        select(Checkin).where(Checkin.user_id == user.id).order_by(Checkin.date.desc()).limit(7)
    ).all())
    weights = [item.weight_kg for item in checkins if item.weight_kg is not None]

    return {
        "days": days,
        "session_count": len(sessions),
        "log_count": len(logs),
        "weekly": {
            "sessions": len(sessions),
            "total_minutes": total_minutes,
            "avg_completion_pct": avg_completion,
            "avg_difficulty": avg_difficulty,
            "avg_fatigue": avg_fatigue,
            "consecutive_incomplete": consecutive,
            "last_training_date": last_date.isoformat() if last_date else "",
            "gap_days": gap_days,
        },
        "trend": trend,
        "weight": {
            "latest": weights[0] if weights else None,
            "change": round(weights[0] - weights[-1], 1) if len(weights) >= 2 else None,
        },
        "findings": findings,
        "recent_sessions": [
            {
                "session_id": item.id,
                "date": item.date.isoformat(),
                "completion_pct": item.completion_pct,
                "actual_minutes": item.actual_minutes,
                "difficulty": item.difficulty,
                "fatigue": item.fatigue,
                "note": item.note,
                "status": item.status,
            }
            for item in sessions[:8]
        ],
        "note": "教练发现基于真实训练会话与训练记录，不包含自动生成的诊断。",
    }


def coach_insight_payload(db: Session, user: User, *, days: int = 14) -> dict:
    return build_progress_insights(db, user, days=days)
