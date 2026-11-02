"""LangGraph 审批流程的冒烟测试：挂起等人 → 恢复 → 提交 / 驳回。

重点验证两件事：
1. `interrupt()` 之后计划仍然是 v1（审批前不落盘）；
2. 用 `Command(resume=...)` 恢复后，走的还是同一批 domain 函数（v2 / rejected）。
"""

from __future__ import annotations

import pytest
from langgraph.types import Command
from sqlalchemy import select

from app.agent.patch_graph import build_patch_graph
from app.config import Settings
from app.main import create_app
from app.models import Plan
from app.seed import DEMO_PLAN_ID, DEMO_USER_ID


@pytest.fixture()
def graph_env(tmp_path):
    """临时库上的图和 app；每个用例一份。"""
    app = create_app(
        Settings(database_url=f"sqlite:///{tmp_path}/graph.db", secret_key="t" * 40)
    )
    return build_patch_graph(app.state.session_factory), app


def _start(graph, thread: str = "thread-1"):
    config = {"configurable": {"thread_id": thread}}
    out = graph.invoke(
        {"user_id": DEMO_USER_ID, "plan_id": DEMO_PLAN_ID, "available_days": 2},
        config,
    )
    return out, config


def _current_version(app) -> int:
    with app.state.session_factory() as session:
        return session.scalar(select(Plan)).current_version


def test_graph_suspends_then_commits_on_approve(graph_env) -> None:
    """approve：挂起时计划不变，恢复后提交 v2。"""
    graph, app = graph_env
    out, config = _start(graph)

    assert out["status"] == "pending_approval"
    assert "__interrupt__" in out, "应该挂起等待人工审批"
    interrupt_payload = out["__interrupt__"][0].value
    assert interrupt_payload["type"] == "approval_request"
    assert interrupt_payload["patch_id"] == out["patch_id"]
    assert _current_version(app) == 1, "审批前不能写新版本"

    resumed = graph.invoke(Command(resume={"decision": "approve", "comment": "ok"}), config)

    assert resumed["status"] == "committed"
    assert resumed["version"] == 2
    assert _current_version(app) == 2


def test_graph_records_reject_without_touching_plan(graph_env) -> None:
    """reject：计划仍停在 v1，补丁状态变 rejected。"""
    graph, app = graph_env
    out, config = _start(graph, thread="thread-2")

    resumed = graph.invoke(Command(resume={"decision": "reject", "comment": "不同意"}), config)

    assert resumed["status"] == "rejected"
    assert _current_version(app) == 1
