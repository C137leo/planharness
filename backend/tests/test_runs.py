"""Run 观测接口：列表、详情、trace 时间线、越权隔离。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _create_and_approve(client: TestClient, headers) -> tuple[str, str]:
    plan = client.get("/api/v1/plans/current", headers=headers).json()["data"]
    patch = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=headers,
        json={"reason_code": "injury", "available_days": 2},
    ).json()["data"]
    client.post(
        f"/api/v1/patches/{patch['id']}/approve",
        headers=headers,
        json={"comment": "ok"},
    )
    return plan["plan_id"], patch["id"]


def test_runs_list_then_detail_has_full_evidence(client: TestClient, auth_headers) -> None:
    """列表能拿到 run；详情里有 trace、diff、上下文快照、验证结论。"""
    _create_and_approve(client, auth_headers)

    runs = client.get("/api/v1/runs", headers=auth_headers).json()["data"]
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "succeeded"
    assert run["stop_reason"] == "completed"
    assert run["patch_id"]

    detail = client.get(f"/api/v1/runs/{run['id']}", headers=auth_headers).json()["data"]

    # trace：节点顺序就是图跑过的顺序
    nodes = [t["node"] for t in detail["trace"]]
    assert nodes[0] == "prepare"
    assert nodes.index("human_approval") < nodes.index("commit") < nodes.index("verify")
    assert detail["trace"][0]["detail"]["operations"] == 3

    # 补丁证据
    assert detail["patch"]["diff"]["churn"] == 0.25
    assert detail["patch"]["status"] == "committed"

    # 上下文快照
    assert detail["context"]["token_count"] > 0
    assert detail["context"]["token_budget"] > 0

    # 验证结论
    assert detail["verification"]["passed"] is True
    assert len(detail["verification"]["checks"]) >= 8


def test_reject_run_records_stop_reason(client: TestClient, auth_headers) -> None:
    """驳回路径也要能在 trace 里看出来。"""
    plan = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]
    patch = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=auth_headers,
        json={"reason_code": "injury", "available_days": 2},
    ).json()["data"]
    client.post(
        f"/api/v1/patches/{patch['id']}/reject",
        headers=auth_headers,
        json={"comment": "不同意"},
    )

    run = client.get("/api/v1/runs", headers=auth_headers).json()["data"][0]
    assert run["status"] == "rejected"
    assert run["stop_reason"] == "approval_rejected"

    detail = client.get(f"/api/v1/runs/{run['id']}", headers=auth_headers).json()["data"]
    assert any(t["node"] == "reject" and t["status"] == "rejected" for t in detail["trace"])


def test_unknown_run_is_404(client: TestClient, auth_headers) -> None:
    assert client.get("/api/v1/runs/does-not-exist", headers=auth_headers).status_code == 404


def test_run_of_another_user_is_404(client: TestClient, auth_headers) -> None:
    """越权访问返回 404，不暴露「这个 run 存在」."""
    _create_and_approve(client, auth_headers)
    run_id = client.get("/api/v1/runs", headers=auth_headers).json()["data"][0]["id"]

    other = register_user(client, phone="13900001111", password="another-pass-2026")
    assert other.status_code == 200, other.text
    other_headers = {"Authorization": f"Bearer {other.json()['data']['access_token']}"}

    assert client.get(f"/api/v1/runs/{run_id}", headers=other_headers).status_code == 404
    assert client.get("/api/v1/runs", headers=other_headers).json()["data"] == []


def test_patch_list_and_pending_filter(client: TestClient, auth_headers) -> None:
    """审批页靠这个接口判断「还有没有待审批的」。"""
    plan = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]
    patch = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=auth_headers,
        json={"reason_code": "injury", "available_days": 2},
    ).json()["data"]

    pending = client.get("/api/v1/patches?status=pending_approval", headers=auth_headers).json()["data"]
    assert [p["id"] for p in pending] == [patch["id"]]
    assert pending[0]["diff"]["churn"] == 0.25

    client.post(
        f"/api/v1/patches/{patch['id']}/approve", headers=auth_headers, json={"comment": "ok"}
    )

    # 审批后不再有待审批项，历史里还能看到它
    assert client.get("/api/v1/patches?status=pending_approval", headers=auth_headers).json()["data"] == []
    history = client.get("/api/v1/patches", headers=auth_headers).json()["data"]
    assert history[0]["status"] == "committed"


def test_runs_can_be_filtered_by_status(client: TestClient, auth_headers) -> None:
    _create_and_approve(client, auth_headers)
    ok = client.get("/api/v1/runs?status=succeeded", headers=auth_headers).json()["data"]
    assert len(ok) == 1
    none = client.get("/api/v1/runs?status=failed", headers=auth_headers).json()["data"]
    assert none == []


def test_second_patch_returns_existing_pending(client: TestClient, auth_headers) -> None:
    """连点两次「生成补丁」不应产生两条待审批记录。"""
    plan = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]
    first = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=auth_headers,
        json={"reason_code": "injury", "available_days": 2},
    ).json()["data"]
    second = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=auth_headers,
        json={"reason_code": "injury", "available_days": 2},
    ).json()["data"]

    assert second["id"] == first["id"]
    assert len(client.get("/api/v1/patches?status=pending_approval", headers=auth_headers).json()["data"]) == 1


def test_patch_after_approval_requires_regenerate(client: TestClient, auth_headers) -> None:
    """补丁打完后，再次打补丁应给出可执行的提示；重新生成计划后循环才能继续。"""
    plan_id, _ = _create_and_approve(client, auth_headers)

    again = client.post(
        f"/api/v1/plans/{plan_id}/patches",
        headers=auth_headers,
        json={"reason_code": "injury", "available_days": 2},
    )
    assert again.status_code == 422
    assert "重新生成计划" in "".join(again.json()["detail"])

    # 全量重新生成 → 槽位回来了 → 可以再打补丁
    regen = client.post("/api/v1/plans/generate", headers=auth_headers)
    assert regen.status_code == 200
    third = client.post(
        f"/api/v1/plans/{plan_id}/patches",
        headers=auth_headers,
        json={"reason_code": "injury", "available_days": 2},
    )
    assert third.status_code == 200
    assert third.json()["data"]["status"] == "pending_approval"


# ---------------- 成本 / 预算（走完整链路） ----------------

import json as _json  # noqa: E402

import httpx as _httpx  # noqa: E402
from sqlalchemy import select as _select  # noqa: E402

from app.agent.patch_graph import build_patch_graph  # noqa: E402
from app.llm.deepseek import DeepSeekExplainer  # noqa: E402
from app.llm.types import LlmBudget  # noqa: E402
from tests.conftest import register_user
from app.models import AgentRun  # noqa: E402


def _mock_explainer(usage: dict):
    def handler(request):
        return _httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"summary":"s","why":"w","risk":"r","next":"n"}'}}],
                "usage": usage,
            },
        )

    return DeepSeekExplainer(
        api_key="test",
        base_url="https://mock/v1",
        model="deepseek-flash",
        timeout_s=1,
        client=_httpx.Client(transport=_httpx.MockTransport(handler)),
    ).explain


def test_run_records_real_tokens_and_cost(client: TestClient, auth_headers) -> None:
    """真实 usage 写进 run：tokens_in/out、cost、stop_reason。"""
    app = client.app
    app.state.patch_graph = build_patch_graph(
        app.state.session_factory,
        explainer=_mock_explainer({"prompt_tokens": 812, "completion_tokens": 96}),
        checkpointer=app.state.checkpointer,
        token_budget=app.state.settings.context_token_budget,
        llm_budget=LlmBudget(max_tokens_per_run=20000, max_cost_cny_per_run=0.05),
    )

    plan = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]
    client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=auth_headers,
        json={"reason_code": "injury", "available_days": 2},
    )

    with app.state.session_factory() as session:
        run = session.scalar(_select(AgentRun).order_by(AgentRun.created_at.desc()))
        assert run.tokens_in == 812
        assert run.tokens_out == 96
        assert run.output["cost_cny"] == pytest.approx(0.002392, rel=1e-6)
        assert run.output["stop_reason"] == "waiting_approval"
        assert run.output["budget"]["exceeded"] is None


def test_budget_exceeded_skips_llm_and_marks_run(client: TestClient, auth_headers) -> None:
    """prompt 估算超上限 → 不调模型，run 标 budget_exceeded，补丁照常生成。"""
    app = client.app
    called = {"n": 0}

    def _boom(messages, operations, diff):
        called["n"] += 1
        raise AssertionError("预算已超，不应调用模型")

    app.state.patch_graph = build_patch_graph(
        app.state.session_factory,
        explainer=_boom,
        checkpointer=app.state.checkpointer,
        token_budget=2000,
        llm_budget=LlmBudget(max_tokens_per_run=10),  # 比 prompt 估算小得多
    )

    plan = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]
    resp = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=auth_headers,
        json={"reason_code": "injury", "available_days": 2},
    )
    assert resp.status_code == 200
    assert called["n"] == 0

    with app.state.session_factory() as session:
        run = session.scalar(_select(AgentRun).order_by(AgentRun.created_at.desc()))
        assert run.status == "budget_exceeded"
        assert run.error_code == "BUDGET_EXCEEDED"
        assert run.tokens_in == 0 and run.tokens_out == 0
        assert run.output["stop_reason"] == "budget_exceeded"
        assert "max_tokens_per_run" in (run.output["budget"]["exceeded"] or "")


# ---------------- 场景回放（Console） ----------------

def test_list_eval_scenarios(client: TestClient, auth_headers) -> None:
    data = client.get("/api/v1/evals/scenarios", headers=auth_headers).json()["data"]
    ids = [s["id"] for s in data]
    assert ids[:8] == ["E01", "E02", "E03", "E04", "E05", "E06", "E07", "E08"]
    assert all(s["title"] for s in data)


def test_replay_scenario_returns_checks(client: TestClient, auth_headers) -> None:
    resp = client.post("/api/v1/evals/scenarios/E04/run", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["id"] == "E04"
    assert body["passed"] is True
    assert any(c["name"] == "独立验证通过" for c in body["checks"])
    assert body["metrics"]["tokens_in"] > 0

    # 回放用的是临时库，不影响调用者的数据
    assert client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]["version"] == 1
    assert client.get("/api/v1/runs", headers=auth_headers).json()["data"] == []


def test_replay_unknown_scenario_404(client: TestClient, auth_headers) -> None:
    assert client.post("/api/v1/evals/scenarios/E99/run", headers=auth_headers).status_code == 404


def test_run_detail_exposes_plan_id_for_audit(client: TestClient, auth_headers) -> None:
    """Console 要靠 plan_id 去拉审计链。"""
    plan_id, _ = _create_and_approve(client, auth_headers)
    run = client.get("/api/v1/runs", headers=auth_headers).json()["data"][0]
    assert run["plan_id"] == plan_id

    detail = client.get(f"/api/v1/runs/{run['id']}", headers=auth_headers).json()["data"]
    assert detail["plan_id"] == plan_id

    events = client.get(f"/api/v1/audit/{plan_id}", headers=auth_headers).json()["data"]
    assert [e["action"] for e in events] == [
        "patch_created",
        "approval_granted",
        "patch_committed",
    ]
    assert client.get(f"/api/v1/audit/{plan_id}/verify", headers=auth_headers).json()["data"]["valid"] is True


# ---------------- A/B/C 对比 ----------------

def test_compare_knee_modes_returns_real_numbers(client: TestClient, auth_headers) -> None:
    """三模式对比：A 有违规且无审批；B 无违规但改动大；C 改动最小。"""
    data = client.post("/api/v1/compare/knee-patch", headers=auth_headers).json()["data"]

    assert data["scenario"].startswith("第 2 周膝痛")
    assert "fixture" in data["fixture_note"]

    modes = {m["mode"]: m for m in data["modes"]}
    assert set(modes) == {"A", "B", "C"}

    a, b, c = modes["A"], modes["B"], modes["C"]
    assert a["violations"] > 0, "A 必须暴露违规"
    assert a["approval"] is False, "A 不走审批"
    assert b["violations"] == 0 and b["approval"] is True
    assert c["violations"] == 0 and c["approval"] is True

    # C 的改动必须小于 B（这就是最小补丁的价值）
    assert c["changed_slots"] < b["changed_slots"]
    assert c["churn"] == 0.25
    assert b["unnecessary_churn"] <= a["unnecessary_churn"]


def test_compare_does_not_touch_user_plan(client: TestClient, auth_headers) -> None:
    before = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]["version"]
    client.post("/api/v1/compare/knee-patch", headers=auth_headers)
    after = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]["version"]
    assert before == after == 1
    assert client.get("/api/v1/patches", headers=auth_headers).json()["data"] == []
