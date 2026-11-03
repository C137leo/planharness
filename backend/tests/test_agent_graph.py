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
        Settings(env="dev", database_url=f"sqlite:///{tmp_path}/graph.db", secret_key="t" * 40)
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


# ---------------- 提交后的独立验证 ----------------

from app.agent.verify import verify_commit  # noqa: E402
from app.models import AgentRun, Approval  # noqa: E402


def _commit_via_graph(graph, thread: str):
    out, config = _start(graph, thread=thread)
    resumed = graph.invoke(Command(resume={"decision": "approve", "comment": "ok"}), config)
    return out, resumed


def test_commit_records_verification_on_run(graph_env) -> None:
    """提交后跑验证器，把结论写进 AgentRun（今后端可查"为什么算完成"）。"""
    graph, app = graph_env
    out, resumed = _commit_via_graph(graph, "thread-verify-ok")

    assert resumed["status"] == "committed"
    assert resumed["verification"]["passed"] is True

    passed_names = {c["name"] for c in resumed["verification"]["checks"] if c["passed"]}
    assert {
        "patch_committed",
        "approval_recorded",
        "current_version_matches",
        "no_forbidden_actions",
    } <= passed_names

    with app.state.session_factory() as session:
        run = session.get(AgentRun, out["run_id"])
        assert run is not None
        assert run.status == "succeeded"
        assert run.error_code is None
        assert run.output["verification"]["passed"] is True
        assert run.output["stop_reason"] == "completed"
        assert run.finished_at is not None


def test_verify_detects_missing_approval_record(graph_env) -> None:
    """把审批记录删掉后，验证器必须报错（环境事实不成立）。"""
    graph, app = graph_env
    out, resumed = _commit_via_graph(graph, "thread-verify-tamper")

    with app.state.session_factory() as session:
        session.query(Approval).filter(Approval.patch_id == out["patch_id"]).delete()
        session.commit()

        result = verify_commit(
            session,
            plan_id=DEMO_PLAN_ID,
            patch_id=out["patch_id"],
            expected_version=resumed["version"],
        )

    assert result.passed is False
    assert "approval_recorded" in result.failed_names()


def test_verify_failure_triggers_auto_rollback(graph_env, monkeypatch) -> None:
    """验证不通过 → 自动回滚到提交前的内容，run 标 failed 并记录回滚版本。"""
    import app.agent.patch_graph as pg
    from app.agent.verify import Check, VerificationResult
    from app.models import PlanVersion

    def fake_verify(db, *, plan_id, patch_id, expected_version):
        return VerificationResult(
            passed=False, checks=[Check("forced_failure", False, "测试注入")]
        )

    monkeypatch.setattr(pg, "verify_commit", fake_verify)

    graph, app = graph_env
    out, config = _start(graph, thread="thread-rollback")
    resumed = graph.invoke(
        Command(resume={"decision": "approve", "comment": "ok"}), config
    )

    # 图的状态：验证失败 → 已回滚
    assert resumed["verified"] is False
    assert resumed["status"] == "rolled_back"
    assert resumed["version"] == 2                 # 失败的那次提交仍是 v2
    assert resumed["rollback_version"] == 3        # 回滚产生 v3

    # 计划内容退回提交前（v1 的 12 个动作）
    with app.state.session_factory() as session:
        plan = session.scalar(select(Plan))
        assert plan.current_version == 3

        v3 = session.scalar(
            select(PlanVersion).where(PlanVersion.version == 3)
        )
        assert v3.parent_version == 2, "回滚版本应接在失败版本之后，历史不被改写"

        run = session.get(AgentRun, out["run_id"])
        assert run.status == "failed"
        assert run.error_code == "VERIFICATION_FAILED"
        assert run.output["rolled_back_to"] == 1
        assert run.output["rollback_version"] == 3
        assert run.output["stop_reason"] == "verification_failed_rolled_back"

    current = client_plan(client_of(app))
    assert len(current["actions"]) == 12, "回滚后动作数应恢复为提交前的 12 个"


def client_of(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def client_plan(client):
    token = client.post("/api/v1/auth/demo", json={}).json()["data"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return client.get("/api/v1/plans/current", headers=headers).json()["data"]
