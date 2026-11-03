"""LangGraph 版的补丁审批流程（与 REST 接口并存，实验性）。

节点顺序：

    START → prepare → human_approval ─┬─ approve → commit → END
                                      └─ reject  → reject → END

和 REST 路径的区别只在「谁驱动状态机」：
- REST：前端分多次请求（创建补丁 / 审批 / 驳回），状态存在数据库里；
- 这里：`interrupt()` 把「等人工审批」变成图里的一个挂起点，checkpointer
  记住现场，恢复时用 `Command(resume=...)` 继续。

两条路径共用同一批 domain 函数（create_knee_patch / approve_patch / reject_patch），
所以业务规则不会分叉 —— 图只是另一种「驱动方式」，不是另一套逻辑。
"""

from __future__ import annotations

from typing import Callable

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy.orm import Session

from app.agent.state import PatchState
from app.audit import append_event
from app.agent.verify import verify_commit
from app.domain.patches import (
    approve_patch,
    create_knee_patch,
    reject_patch,
    rollback_plan,
)
from app.models import AgentRun, PlanPatch, User
from app.models import utcnow

# 与 patches.create_knee_patch 的 explainer 参数同签名：
# 吃「装配好的 messages + 操作 + diff」，吐一段解释文本
Explainer = Callable[[list[dict], list[dict], dict], str]


def build_patch_graph(
    session_factory,
    explainer: Explainer | None = None,
    checkpointer=None,
    token_budget: int = 2000,
    llm_budget=None,
):
    """组装并编译图。

    `session_factory` / `explainer` / `checkpointer` 全部从外面注入 ——
    图不持有全局状态，测试里可以塞临时库 + 假 explainer，和生产共用同一份代码。

    `checkpointer=None` 时用内存版（仅适合测试/演示）；生产的 Postgres saver
    由 `app/agent/checkpointer.py` 按环境选择后传进来。
    """

    def _finish_run(
        db: Session,
        run_id: str | None,
        *,
        status: str,
        error_code: str | None = None,
        extra: dict | None = None,
    ) -> None:
        """落一次 run 的终态；run_id 缺失时静默跳过（图可以脱离 AgentRun 单独跑）。"""
        if not run_id:
            return
        run = db.get(AgentRun, run_id)
        if run is None:
            return
        run.status = status
        run.error_code = error_code
        run.output = {**(run.output or {}), **(extra or {})}
        run.finished_at = utcnow()

    def _trace(db: Session, run_id: str | None, node: str, status: str, detail: dict | None = None) -> None:
        """往 AgentRun.trace 追加一步。

        - 连续重复的 (node, status) 会被忽略：`interrupt` 恢复时节点会重跑一遍，
          不判重就会看到两条一模一样的 `human_approval / waiting_approval`。
        - 只 append、不覆盖，时间线天然有序。
        """
        if not run_id:
            return
        run = db.get(AgentRun, run_id)
        if run is None:
            return
        entries = list(run.trace or [])
        entry = {"node": node, "status": status, "ts": utcnow().isoformat(timespec="milliseconds")}
        if detail:
            entry["detail"] = detail
        if entries and entries[-1].get("node") == node and entries[-1].get("status") == status:
            return
        entries.append(entry)
        run.trace = entries

    def prepare(state: PatchState, config: RunnableConfig) -> dict:
        """生成补丁草稿（不写计划版本，审批前不落盘）。

        `config` 由 LangGraph 自动注入，里面带 `configurable.thread_id`。
        让 patch_id 默认等于 thread_id，是为了「审批时能按 patch_id resume」——
        两个 id 不一致的话，图上找不到对应的 thread。
        """
        with session_factory() as db:
            user = db.get(User, state["user_id"])
            if user is None:
                raise ValueError(f"user not found: {state['user_id']}")
            patch_id = state.get("patch_id") or config["configurable"]["thread_id"]
            try:
                patch = create_knee_patch(
                    db,
                    user,
                    state["plan_id"],
                    patch_id=patch_id,
                    available_days=state.get("available_days", 2),
                    explainer=explainer,
                    token_budget=token_budget,
                    llm_budget=llm_budget,
                )
            except Exception as exc:
                _finish_run(
                    db,
                    state.get("run_id"),
                    status="failed",
                    error_code="PREPARE_FAILED",
                    extra={"stop_reason": "prepare_failed", "error": str(exc)[:200]},
                )
                db.commit()
                raise
            validation = patch.validation or {}
            # run_id 是 create_knee_patch 里才生成的，所以 prepare 的 trace
            # 只能在补丁创建完之后写 —— 这也解释了为什么它是时间线第一项。
            run_id = validation.get("run_id") or state.get("run_id") or ""
            _trace(
                db,
                run_id,
                "prepare",
                "ok",
                {"patch_id": patch.id, "operations": len(patch.operations or [])},
            )
            db.commit()
            return {
                "patch_id": patch.id,
                "run_id": run_id,
                "status": patch.status,
                "explanation": validation.get("explanation", ""),
                "diff": patch.diff or {},
                "run_id":(patch.validation or {}).get("run_id",""),
            }

    def human_approval(state: PatchState) -> dict:
        """暂停等人审批；恢复时 interrupt() 返回 Command(resume=...) 的值。"""
        # 注意：interrupt 之后节点会被重跑，_trace 会去重
        with session_factory() as db:
            _trace(db, state.get("run_id"), "human_approval", "waiting_approval")
            db.commit()

        decision = interrupt(
            {
                "type": "approval_request",
                "patch_id": state["patch_id"],
                "run_id": state.get("run_id", ""),
                "plan_id": state["plan_id"],
                "status": state.get("status", "pending_approval"),
                "explanation": state.get("explanation", ""),
                "diff": state.get("diff", {}),
            }
        )
        payload = decision if isinstance(decision, dict) else {}
        with session_factory() as db:
            _trace(
                db,
                state.get("run_id"),
                "human_approval",
                "decided",
                {"decision": payload.get("decision", "reject")},
            )
            db.commit()
        return {
            "decision": payload.get("decision", "reject"),
            "comment": payload.get("comment", ""),
        }

    def commit(state: PatchState) -> dict:
        """审批通过：提交新版本。验证交给下一个节点，这里不做判断。

        保持单一职责：commit 只负责「写」，verify 只负责「查」。
        """
        with session_factory() as db:
            user = db.get(User, state["user_id"])
            _trace(db, state.get("run_id"), "commit", "running")
            try:
                plan, version = approve_patch(
                    db,
                    user,
                    state["patch_id"],
                    state.get("comment", ""),
                )
            except Exception as exc:
                _finish_run(
                    db,
                    state.get("run_id"),
                    status="failed",
                    error_code="COMMIT_FAILED",
                    extra={"stop_reason": "commit_failed", "error": str(exc)[:200]},
                )
                db.commit()
                raise

            _trace(db, state.get("run_id"), "commit", "committed", {"version": version.version})
            _finish_run(
                db,
                state.get("run_id"),
                status="running",
                extra={
                    "version": version.version,
                    "stop_reason": "committed_pending_verification",
                },
            )
            db.commit()
            return {"version": version.version, "status": "committed"}

    def verify(state: PatchState) -> dict:
        """独立验证：只查数据库事实，不信任 commit 的返回值。

        - 通过 → 把 run 收尾为 succeeded，图结束；
        - 不通过 → 交给 rollback 节点，把计划退回提交前的版本。
        """
        with session_factory() as db:
            result = verify_commit(
                db,
                plan_id=state["plan_id"],
                patch_id=state["patch_id"],
                expected_version=state["version"],
            )
            _trace(
                db,
                state.get("run_id"),
                "verify",
                "passed" if result.passed else "failed",
                {"failed_checks": result.failed_names()},
            )
            if not result.passed:
                append_event(
                    db,
                    state["plan_id"],
                    "verification_failed",
                    {
                        "patch_id": state["patch_id"],
                        "version": state["version"],
                        "failed_checks": result.failed_names(),
                    },
                    actor_type="system",
                    actor_id="verifier",
                )
            if result.passed:
                _finish_run(
                    db,
                    state.get("run_id"),
                    status="succeeded",
                    extra={
                        "version": state["version"],
                        "verification": result.to_dict(),
                        "stop_reason": "completed",
                    },
                )
            else:
                # 失败时先不改 run 状态：交给 rollback 节点，把回滚结果一起记进去
                _finish_run(
                    db,
                    state.get("run_id"),
                    status="running",
                    extra={
                        "version": state["version"],
                        "verification": result.to_dict(),
                        "stop_reason": "verification_failed",
                    },
                )
            db.commit()
        return {"verified": result.passed, "verification": result.to_dict()}

    def rollback(state: PatchState) -> dict:
        """验证不通过 → 自动回滚到提交前的版本。

        回滚也是「再提交一个新版本」（append-only），
        所以历史里同时留有「失败的提交」和「回滚后的正确状态」，可追溯。
        """
        with session_factory() as db:
            user = db.get(User, state["user_id"])
            patch = db.get(PlanPatch, state["patch_id"])
            target = patch.base_version if patch is not None else None
            try:
                new_version = rollback_plan(db, user, state["plan_id"], target)
            except Exception as exc:
                _finish_run(
                    db,
                    state.get("run_id"),
                    status="failed",
                    error_code="ROLLBACK_FAILED",
                    extra={
                        "verification": state.get("verification", {}),
                        "rolled_back_to": target,
                        "stop_reason": "rollback_failed",
                        "error": str(exc)[:200],
                    },
                )
                db.commit()
                raise

            _trace(
                db,
                state.get("run_id"),
                "rollback",
                "rolled_back",
                {"target": target, "new_version": new_version.version},
            )
            # 注意：domain 的 rollback_plan 已经记过一条 rollback_committed，
            # 这里只补「因为验证失败才回滚」的上下文，避免同一次回滚记两条。
            _finish_run(
                db,
                state.get("run_id"),
                status="failed",
                error_code="VERIFICATION_FAILED",
                extra={
                    "verification": state.get("verification", {}),
                    "rolled_back_to": target,
                    "rollback_version": new_version.version,
                    "stop_reason": "verification_failed_rolled_back",
                },
            )
            db.commit()
        return {
            "verified": False,
            "status": "rolled_back",
            "rollback_version": new_version.version,
        }

    def reject(state: PatchState) -> dict:
        """驳回：只改补丁状态，计划版本保持不变。"""
        with session_factory() as db:
            user = db.get(User, state["user_id"])
            reject_patch(db, user, state["patch_id"], state.get("comment", ""))
            _trace(db, state.get("run_id"), "reject", "rejected")
            _finish_run(
                db,
                state.get("run_id"),
                status="rejected",
                extra={"stop_reason": "approval_rejected"},
            )
            db.commit()
            return {"status": "rejected"}

    builder = StateGraph(PatchState)
    builder.add_node("prepare", prepare)
    builder.add_node("human_approval", human_approval)
    builder.add_node("commit", commit)
    builder.add_node("verify", verify)
    builder.add_node("rollback", rollback)
    builder.add_node("reject", reject)

    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "human_approval")
    builder.add_conditional_edges(
        "human_approval",
        lambda state: "commit" if state.get("decision") == "approve" else "reject",
        {"commit": "commit", "reject": "reject"},
    )
    builder.add_edge("commit", "verify")
    builder.add_conditional_edges(
        "verify",
        lambda state: "ok" if state.get("verified") else "rollback",
        {"ok": END, "rollback": "rollback"},
    )
    builder.add_edge("rollback", END)
    builder.add_edge("reject", END)

    # 没注入就用内存版：进程重启即丢，只适合测试/演示
    return builder.compile(checkpointer=checkpointer or InMemorySaver())
