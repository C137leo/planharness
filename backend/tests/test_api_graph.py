"""REST 路径的补丁全流程（创建 → 审批/驳回）。

登录头用 conftest 里的 `auth_headers`：它走 demo 登录，不依赖任何预置账号。
"""

from __future__ import annotations


def test_api_uses_graph_for_create_and_approve(client, auth_headers):
    plan = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]

    created = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=auth_headers,
        json={"reason_code": "injury", "available_days": 2},
    )
    assert created.status_code == 200
    patch = created.json()["data"]
    assert patch["status"] == "pending_approval"

    still = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]
    assert still["version"] == 1

    approved = client.post(
        f"/api/v1/patches/{patch['id']}/approve",
        headers=auth_headers,
        json={"comment": "ok"},
    )
    assert approved.status_code == 200

    after = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]
    assert after["version"] == 2
    assert len(after["actions"]) == 11

def test_api_reject_keeps_version(client, auth_headers):
    plan = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]

    created = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=auth_headers,
        json={"reason_code": "injury", "available_days": 2},
    )
    assert created.status_code == 200
    patch = created.json()["data"]

    rejected = client.post(
        f"/api/v1/patches/{patch['id']}/reject",
        headers=auth_headers,
        json={"comment": "not needed"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["data"]["status"] == "rejected"

    # 拒绝后版本不变
    after = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]
    assert after["version"] == 1


def test_approve_unknown_patch_returns_404(client, auth_headers):
    resp = client.post(
        "/api/v1/patches/99999999/approve",
        headers=auth_headers,
        json={"comment": "ok"},
    )
    assert resp.status_code == 404

def test_approve_returns_409_and_rolls_back_when_verification_fails(
    client, auth_headers, monkeypatch
) -> None:
    """接口层：验证失败 → 409，且计划已被自动回滚到提交前。"""
    import app.agent.patch_graph as pg
    from app.agent.verify import Check, VerificationResult

    monkeypatch.setattr(
        pg,
        "verify_commit",
        lambda *a, **k: VerificationResult(
            passed=False, checks=[Check("forced_failure", False, "测试注入")]
        ),
    )

    plan = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]
    patch = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=auth_headers,
        json={"reason_code": "injury", "available_days": 2},
    ).json()["data"]

    resp = client.post(
        f"/api/v1/patches/{patch['id']}/approve",
        headers=auth_headers,
        json={"comment": "ok"},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["rolled_back"] is True
    assert detail["rollback_version"] == 3
    assert detail["failed_checks"] == ["forced_failure"]

    after = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]
    assert after["version"] == 3
    assert len(after["actions"]) == 12, "回滚后应恢复为提交前的 12 个动作"
