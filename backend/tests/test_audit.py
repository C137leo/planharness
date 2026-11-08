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

def test_audit_stream_id_can_hold_user_prefix() -> None:
    """用户级审计流是 user:<36>，列宽不能只按 plan_id 的 36 设计。

    SQLite 不校验 VARCHAR 长度，所以这个 bug 只在 Postgres 上炸；
    这里直接校验模型列宽，避免再次部署才暴露。
    """
    from app.models import AuditEvent

    width = AuditEvent.__table__.c.stream_id.type.length
    assert width is not None and width >= len("user:") + 36


def test_verify_returns_head_and_signature(client: TestClient, auth_headers) -> None:
    """链头 + 签名：把这三个值抄到库外，就能发现"整链重写/尾部截断"。"""
    from app.audit import sign_head

    header = auth_headers
    plan = client.get("/api/v1/plans/current", headers=header).json()["data"]
    client.post(f"/api/v1/plans/{plan['plan_id']}/patch-requests", headers=header,
                json={"text": "今天想休息", "quick_action": "rest"})
    result = client.get(f"/api/v1/audit/{plan['plan_id']}/verify", headers=header).json()["data"]

    assert result["valid"] is True
    assert result["head_seq"] == result["count"]
    assert len(result["head_hash"]) == 64
    assert result["signature_alg"].startswith("HMAC-SHA256")
    expected = sign_head(client.app.state.settings.secret_key, plan["plan_id"],
                         result["head_seq"], result["head_hash"])
    assert result["signature"] == expected


def test_signature_changes_when_stream_is_rewritten(client: TestClient, auth_headers) -> None:
    """尾部截断在链内是"合法的"，但链头/签名会变——这就是锚点的意义。"""
    from app.audit import sign_head
    from app.models import AuditEvent

    header = auth_headers
    plan = client.get("/api/v1/plans/current", headers=header).json()["data"]
    client.post(f"/api/v1/plans/{plan['plan_id']}/patch-requests", headers=header,
                json={"text": "今天想休息", "quick_action": "rest"})
    first = client.get(f"/api/v1/audit/{plan['plan_id']}/verify", headers=header).json()["data"]

    with client.app.state.session_factory() as db:
        last = db.query(AuditEvent).filter(AuditEvent.stream_id == plan["plan_id"]) \
            .order_by(AuditEvent.seq.desc()).first()
        db.delete(last)          # 模拟"删掉最后一条"
        db.commit()

    second = client.get(f"/api/v1/audit/{plan['plan_id']}/verify", headers=header).json()["data"]
    assert second["valid"] is True          # 链内看仍然合法
    assert second["head_seq"] != first["head_seq"]
    assert second["head_hash"] != first["head_hash"]
    assert second["signature"] != first["signature"]
    # 旧签名只对旧的 (seq, hash) 有效，拿来验现在的链头会失败
    assert sign_head(client.app.state.settings.secret_key, plan["plan_id"],
                     first["head_seq"], first["head_hash"]) == first["signature"]
