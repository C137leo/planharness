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

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agent.state import PatchState
from app.domain.patches import approve_patch, create_knee_patch, reject_patch
from app.models import User

# 与 patches.create_knee_patch 的 explainer 参数同签名：
# 吃「装配好的 messages + 操作 + diff」，吐一段解释文本
Explainer = Callable[[list[dict], list[dict], dict], str]


def build_patch_graph(session_factory, explainer: Explainer | None = None):
    """组装并编译图。

    `session_factory` 与 `explainer` 都从外面注入 —— 图不持有全局状态，
    测试里可以塞临时库 + 假 explainer，和生产共用同一份代码。
    """

    def prepare(state: PatchState) -> dict:
        """生成补丁草稿（不写计划版本，审批前不落盘）。"""
        with session_factory() as db:
            user = db.get(User, state["user_id"])
            if user is None:
                raise ValueError(f"user not found: {state['user_id']}")
            patch = create_knee_patch(
                db,
                user,
                state["plan_id"],
                available_days=state.get("available_days", 2),
                explainer=explainer,
            )
            validation = patch.validation or {}
            return {
                "patch_id": patch.id,
                "status": patch.status,
                "explanation": validation.get("explanation", ""),
                "diff": patch.diff or {},
            }

    def human_approval(state: PatchState) -> dict:
        """暂停等人审批；恢复时 interrupt() 返回 Command(resume=...) 的值。"""
        decision = interrupt(
            {
                "type": "approval_request",
                "patch_id": state["patch_id"],
                "plan_id": state["plan_id"],
                "status": state.get("status", "pending_approval"),
                "explanation": state.get("explanation", ""),
                "diff": state.get("diff", {}),
            }
        )
        payload = decision if isinstance(decision, dict) else {}
        return {
            "decision": payload.get("decision", "reject"),
            "comment": payload.get("comment", ""),
        }

    def commit(state: PatchState) -> dict:
        """审批通过：重跑安全门并提交新版本。"""
        with session_factory() as db:
            user = db.get(User, state["user_id"])
            _, version = approve_patch(
                db,
                user,
                state["patch_id"],
                state.get("comment", ""),
            )
            return {"version": version.version, "status": "committed"}

    def reject(state: PatchState) -> dict:
        """驳回：只改补丁状态，计划版本保持不变。"""
        with session_factory() as db:
            user = db.get(User, state["user_id"])
            reject_patch(db, user, state["patch_id"], state.get("comment", ""))
            return {"status": "rejected"}

    builder = StateGraph(PatchState)
    builder.add_node("prepare", prepare)
    builder.add_node("human_approval", human_approval)
    builder.add_node("commit", commit)
    builder.add_node("reject", reject)

    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "human_approval")
    builder.add_conditional_edges(
        "human_approval",
        lambda state: "commit" if state.get("decision") == "approve" else "reject",
        {"commit": "commit", "reject": "reject"},
    )
    builder.add_edge("commit", END)
    builder.add_edge("reject", END)

    # 内存 checkpointer：进程重启即丢，够演示用；生产应换 Postgres saver
    return builder.compile(checkpointer=InMemorySaver())
