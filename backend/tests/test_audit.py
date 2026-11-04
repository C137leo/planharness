"""审计哈希链：正常链 valid；篡改 payload / 删除中间事件必须被抓住。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.audit import GENESIS_HASH, verify_stream
from tests.conftest import register_user
from app.models import AuditEvent


def _make_events(client: TestClient, headers) -> str:
    """创建补丁 → 审批通过，产生 patch_created / approval_granted / patch_committed。"""
    plan = client.get("/api/v1/plans/current", headers=headers).json()["data"]
    patch = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=headers,
        json={"reason_code": "injury", "available_days": 2},
    ).json()["data"]
    client.post(
        f"/api/v1/patches/{patch['id']}/approve", headers=headers, json={"comment": "ok"}
    )
    return plan["plan_id"]


def test_chain_is_valid_after_normal_flow(client: TestClient, auth_headers) -> None:
    plan_id = _make_events(client, auth_headers)

    events = client.get(f"/api/v1/audit/{plan_id}", headers=auth_headers).json()["data"]
    assert [e["action"] for e in events] == [
        "patch_created",
        "approval_granted",
        "patch_committed",
    ]
    assert events[0]["prev_hash"] == GENESIS_HASH
    assert events[1]["prev_hash"] == events[0]["hash"]
    assert events[2]["prev_hash"] == events[1]["hash"]

    verdict = client.get(f"/api/v1/audit/{plan_id}/verify", headers=auth_headers).json()["data"]
    assert verdict["valid"] is True
    assert verdict["count"] == 3
    assert verdict["broken_at"] is None


def test_tampered_payload_is_detected(client: TestClient, auth_headers) -> None:
    """改 payload 但不重算 hash → 在第 1 条就断。"""
    plan_id = _make_events(client, auth_headers)
    app = client.app

    with app.state.session_factory() as session:
        event = session.scalar(
            select(AuditEvent).where(AuditEvent.stream_id == plan_id, AuditEvent.seq == 1)
        )
        tampered = dict(event.payload)
        tampered["churn"] = 0.99          # 伪造：把 churn 改小
        event.payload = tampered
        session.commit()

    verdict = client.get(f"/api/v1/audit/{plan_id}/verify", headers=auth_headers).json()["data"]
    assert verdict["valid"] is False
    assert verdict["broken_at"] == 1
    assert "payload_hash" in verdict["reason"]


def test_tampering_with_rehashed_event_breaks_link(client: TestClient, auth_headers) -> None:
    """改 payload 且重算自己的 hash → 下一条的 prev_hash 对不上。"""
    plan_id = _make_events(client, auth_headers)
    app = client.app

    from app.audit import payload_hash as compute_payload_hash

    with app.state.session_factory() as session:
        event = session.scalar(
            select(AuditEvent).where(AuditEvent.stream_id == plan_id, AuditEvent.seq == 1)
        )
        tampered = dict(event.payload)
        tampered["operations"] = 99
        event.payload = tampered
        event.payload_hash = compute_payload_hash(tampered)
        # 故意不更新 event.hash → 第 1 条自身 hash 就对不上
        session.commit()

    verdict = client.get(f"/api/v1/audit/{plan_id}/verify", headers=auth_headers).json()["data"]
    assert verdict["valid"] is False
    assert verdict["broken_at"] == 1


def test_deleting_middle_event_is_detected(client: TestClient, auth_headers) -> None:
    """删掉中间一条 → seq 断档 / prev_hash 对不上。"""
    plan_id = _make_events(client, auth_headers)
    app = client.app

    with app.state.session_factory() as session:
        session.execute(
            delete(AuditEvent).where(AuditEvent.stream_id == plan_id, AuditEvent.seq == 2)
        )
        session.commit()

        verdict = verify_stream(session, plan_id)

    assert verdict.valid is False
    assert verdict.broken_at == 3          # 第 3 条的 seq 期望是 2
    assert "seq gap" in verdict.reason


def test_reject_is_recorded(client: TestClient, auth_headers) -> None:
    """驳回也要进链。"""
    plan = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]
    patch = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=auth_headers,
        json={"reason_code": "injury", "available_days": 2},
    ).json()["data"]
    client.post(
        f"/api/v1/patches/{patch['id']}/reject", headers=auth_headers, json={"comment": "不同意"}
    )
    actions = [
        e["action"]
        for e in client.get(f"/api/v1/audit/{plan['plan_id']}", headers=auth_headers).json()["data"]
    ]
    assert actions == ["patch_created", "approval_rejected"]


def test_rollback_is_recorded(client: TestClient, auth_headers) -> None:
    """回滚进链，且链仍然完整。"""
    plan = client.get("/api/v1/plans/current", headers=auth_headers).json()["data"]
    patch = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=auth_headers,
        json={"reason_code": "injury", "available_days": 2},
    ).json()["data"]
    client.post(
        f"/api/v1/patches/{patch['id']}/approve", headers=auth_headers, json={"comment": "ok"}
    )
    # v2 之后回滚到 v1（当前版本是 2，才允许回滚到 1）
    rolled = client.post(
        f"/api/v1/plans/{plan['plan_id']}/rollback",
        headers=auth_headers,
        json={"target_version": 1},
    )
    assert rolled.status_code == 200, rolled.text

    actions = [
        e["action"]
        for e in client.get(f"/api/v1/audit/{plan['plan_id']}", headers=auth_headers).json()["data"]
    ]
    assert actions[-1] == "rollback_committed"
    verdict = client.get(f"/api/v1/audit/{plan['plan_id']}/verify", headers=auth_headers).json()["data"]
    assert verdict["valid"] is True


def test_audit_stream_of_other_user_is_404(client: TestClient, auth_headers) -> None:
    plan_id = _make_events(client, auth_headers)
    other = register_user(client, phone="13900002222", password="another-pass-2026").json()[
        "data"
    ]["access_token"]
    other_headers = {"Authorization": f"Bearer {other}"}
    assert client.get(f"/api/v1/audit/{plan_id}", headers=other_headers).status_code == 404
    assert client.get(f"/api/v1/audit/{plan_id}/verify", headers=other_headers).status_code == 404
