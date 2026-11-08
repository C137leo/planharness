"""TodayAgent 接口：异步运行 + 复用 /runs 查看 trace。

为什么异步：模型可能要调 3-6 次工具，5-15 秒，前端轮询 /runs/{id} 更稳。
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes import get_current_user, get_db, ok
from app.agent.today_agent import run as run_today
from app.llm.tool_client import tool_client_from_settings
from app.models import AgentRun, User

router = APIRouter(prefix="/api/v1")


class TodayPlanIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    date: str | None = None
    # 用户自己的麦当劳 MCP token；joe123456 会映射到服务端 token
    mcd_token: str | None = Field(default=None, max_length=512)


class FeedbackIn(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    mcd_token: str | None = Field(default=None, max_length=512)


class ApplyIn(BaseModel):
    selections: list[dict] = Field(default_factory=list)
    date: str | None = None


def _run_agent(run_id: str, user_id: str, day: dt.date, text: str, session_factory, settings,
               mcd_token: str | None = None) -> None:
    """后台执行：独立 session，结束后写回 run。

    trace 边跑边写：每完成一步就 commit 一次，前端轮询 /runs/{id} 能看到进度，
    而不是盯着"正在启动模型…"等十几秒。
    """
    with session_factory() as db:
        run = db.get(AgentRun, run_id)
        if run is None:
            return
        user = db.get(User, user_id)
        client = tool_client_from_settings(settings)

        def _stamp(status: str) -> dict:
            return {"node": "agent", "status": status,
                    "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "detail": {}}

        def on_step(step: dict) -> None:
            run.trace = [*(run.trace or []), step]
            db.commit()

        run.trace = [_stamp("queued")]
        db.commit()
        try:
            result = run_today(
                db, user, day, text,
                client=client if client.available() else None,
                on_step=on_step,
                mcd_token=mcd_token,
            )
            run.status = "rejected" if result.refused else "succeeded"
            run.output = result.final or {}
            run.trace = result.trace
            run.tokens_in = int(result.usage.get("prompt_tokens") or 0)
            run.tokens_out = int(result.usage.get("completion_tokens") or 0)
            run.error_code = (result.error or "")[:64] or None
            # 成本护栏：今天这条链路也要跟 planner 一样记账，超预算在 Console 里标出来
            cost = (
                run.tokens_in / 1_000_000 * float(settings.llm_price_in_cny_per_mtok)
                + run.tokens_out / 1_000_000 * float(settings.llm_price_out_cny_per_mtok)
            )
            budget = float(getattr(settings, "max_cost_cny_per_run", 0) or 0)
            if budget and cost > budget:
                run.output = {**run.output, "budget": {
                    "exceeded": f"本次成本 {cost:.4f} 元，超过单次预算 {budget:.2f} 元",
                    "cost_cny": round(cost, 4),
                }}
            # 和"这一天"绑定：重开页面就能拿到同一份（含麦当劳那块）。
            # 红旗拒答不覆盖当天快照：那次运行的结论是"今天别练"，但当天仍然有一份
            # 可执行的计划；拒答本身留在 run 历史和 Console 里，不需要顶掉计划。
            if result.final and not result.refused:
                from app.domain.today_snapshot import save_today_plan

                save_today_plan(db, user, day, request_text=text,
                                output=result.final, run_id=run.id)
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把进程带崩
            run.status = "failed"
            run.error_code = type(exc).__name__
            run.output = {"error": str(exc)[:200]}
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        # 顺手把耗时算出来：Console 的 latency 列之前对 today_plan 一直是 0
        if run.created_at:
            created = run.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=dt.timezone.utc)
            run.latency_ms = max(0, int((run.finished_at - created).total_seconds() * 1000))
        db.commit()


def _create_run(db, user: User, text: str) -> AgentRun:
    run = AgentRun(
        user_id=user.id,
        kind="today_plan",
        mode="agent",
        status="running",
        input={"text": text},
        trace=[],
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


@router.post("/agent/today-plan", status_code=202)
def create_today_plan(
    payload: TodayPlanIn,
    request: Request,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    day = dt.date.fromisoformat(payload.date) if payload.date else dt.date.today()
    run = _create_run(db, user, payload.text)
    background.add_task(
        _run_agent, run.id, user.id, day, payload.text,
        request.app.state.session_factory, request.app.state.settings,
        payload.mcd_token,
    )
    return ok(request, {"run_id": run.id, "status": run.status}, message="accepted")


@router.get("/agent/today-plan")
def get_today_plan(
    request: Request,
    date: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取某一天已保存的规划（默认今天）。没有就返回 null，让前端走"重新生成"。"""
    from app.domain.today_snapshot import load_today_plan, payload as snapshot_payload

    try:
        day = dt.date.fromisoformat(date) if date else dt.date.today()
    except ValueError as exc:
        raise HTTPException(422, "date 需要 YYYY-MM-DD") from exc
    row = load_today_plan(db, user, day)
    return ok(request, snapshot_payload(row) if row is not None else None)


@router.post("/agent/today-plan/{run_id}/apply")
def apply_today_plan(
    run_id: str,
    payload: ApplyIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把选中的餐食写回计划（新版本，可回滚）。"""
    from app.domain.meal_choice import apply_meal_choices
    from app.domain.patches import PatchValidationError

    run = db.get(AgentRun, run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(404, "run not found")
    # 计划 id 从当前 active 计划取（一个用户一份）
    from app.models import Plan
    plan = db.query(Plan).filter(Plan.user_id == user.id, Plan.status == "active").first()
    if plan is None:
        raise HTTPException(404, "no active plan")
    day = dt.date.fromisoformat(payload.date) if payload.date else dt.date.today()
    try:
        version = apply_meal_choices(db, user, plan.id, payload.selections, day)
    except PatchValidationError as exc:
        raise HTTPException(422, detail=exc.errors) from exc
    return ok(request, {"new_version": version.version}, message="applied")


@router.post("/agent/today-plan/{run_id}/feedback", status_code=202)
def today_plan_feedback(
    run_id: str,
    payload: FeedbackIn,
    request: Request,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    previous = db.get(AgentRun, run_id)
    if previous is None or previous.user_id != user.id:
        raise HTTPException(404, "run not found")
    text = f"{previous.input.get('text') or ''}\n修改意见：{payload.text}"
    run = _create_run(db, user, text)
    background.add_task(
        _run_agent, run.id, user.id, dt.date.today(), text,
        request.app.state.session_factory, request.app.state.settings,
        payload.mcd_token,
    )
    return ok(request, {"run_id": run.id, "status": run.status}, message="accepted")
