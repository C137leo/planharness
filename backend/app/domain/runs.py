"""领域层也要留下 run 记录。

Console 的「运行记录」以前只显示 LangGraph 走出来的 run；而
一句话改计划、审批、撤销、生成计划这些走的是领域函数，控制台里就什么都看不到。
这里给它们补一条轻量的 AgentRun：trace 写清每一步，output 带版本号/补丁 id。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import AgentRun, User


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_run(
    db: Session,
    user: User,
    *,
    kind: str,
    trace: list[dict],
    output: dict | None = None,
    mode: str = "patch",
    status: str = "succeeded",
    input_payload: dict | None = None,
    latency_ms: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> AgentRun:
    """写一条 run（不 commit，由调用方的事务一起提交）。"""
    run = AgentRun(
        user_id=user.id,
        kind=kind,
        mode=mode,
        status=status,
        input=input_payload or {},
        output=output or {},
        trace=trace,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        finished_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()
    return run


def step(node: str, status: str = "ok", **detail) -> dict:
    """一步 trace（规则：node + status + ts + detail，和 LangGraph 的格式一致）。"""
    return {"node": node, "status": status, "ts": _now(), "detail": detail}
