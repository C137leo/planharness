"""某一天的 Agent 规划快照：一天一条，重新规划覆盖当天。

为什么单独一张表：`AgentRun` 是"每次运行"的观测日志（trace、token、耗时），
而"今天吃什么、麦当劳推荐哪几样"是**当天的事实**，一天只该有一份。
把它存下来，用户重开页面看到的是同一份结果，而不是空着等重新生成。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Plan, PlanVersion, TodayAgentPlan, User, new_id


def _active_version(db: Session, user: User) -> tuple[Plan | None, PlanVersion | None]:
    plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    if plan is None:
        return None, None
    version = db.scalar(
        select(PlanVersion).where(
            PlanVersion.plan_id == plan.id, PlanVersion.version == plan.current_version
        )
    )
    return plan, version


def save_today_plan(
    db: Session,
    user: User,
    day: dt.date,
    *,
    request_text: str,
    output: dict,
    run_id: str | None = None,
) -> TodayAgentPlan:
    """写/覆盖当天的规划快照（同一天只留最新一份）。"""
    plan, version = _active_version(db, user)
    row = db.scalar(
        select(TodayAgentPlan).where(
            TodayAgentPlan.user_id == user.id, TodayAgentPlan.day == day
        )
    )
    if row is None:
        row = TodayAgentPlan(id=new_id(), user_id=user.id, day=day)
        db.add(row)
    row.plan_id = plan.id if plan is not None else None
    row.plan_version = version.version if version is not None else None
    row.request_text = (request_text or "")[:200]
    # 存副本：SQLAlchemy 的 JSON 字段是原地可变对象，直接塞引用容易和 run.output 串味
    row.output = dict(output or {})
    row.run_id = run_id
    row.updated_at = dt.datetime.now(dt.timezone.utc)
    db.flush()
    return row


def load_today_plan(db: Session, user: User, day: dt.date) -> TodayAgentPlan | None:
    return db.scalar(
        select(TodayAgentPlan).where(
            TodayAgentPlan.user_id == user.id, TodayAgentPlan.day == day
        )
    )


def _local_text(value: dt.datetime | None) -> str:
    """库里是 UTC（SQLite 读回来会丢时区），返回时统一成本地时间给前端显示。"""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def payload(row: TodayAgentPlan) -> dict:
    """接口返回用：把行拍平成前端要的形状。"""
    return {
        "day": row.day.isoformat(),
        "plan_version": row.plan_version,
        "request_text": row.request_text,
        "output": row.output or {},
        "run_id": row.run_id,
        "saved_at": _local_text(row.updated_at or row.created_at),
        "has_mcdonalds": bool((row.output or {}).get("mcdonalds")),
    }
