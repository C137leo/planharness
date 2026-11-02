"""补丁主流程测试：审批前不写库、审批后提交新版本、驳回、回滚。

对应 README 的核心演示剧本（3 个槽位 / churn 25%）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _client_and_token(tmp_path):
    """返回指向临时库的 TestClient 与已鉴权请求头。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        secret_key="test-secret-0123456789abcdef0123456789",
    )
    client = TestClient(create_app(settings))
    token = client.post("/api/v1/auth/demo", json={}).json()["data"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return client, headers


def test_knee_patch_is_pending_until_approved(tmp_path) -> None:
    """创建补丁不得改动计划；审批通过后才出现 v2 且禁忌动作被替换。"""
    client, headers = _client_and_token(tmp_path)
    plan = client.get("/api/v1/plans/current", headers=headers).json()["data"]
    assert plan["version"] == 1

    response = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=headers,
        json={"reason_code": "injury", "available_days": 2},
    )
    assert response.status_code == 200, response.text
    patch = response.json()["data"]
    assert patch["status"] == "pending_approval"
    # 补丁只动 3 个槽位，churn 应为 3/12 = 0.25
    assert patch["diff"]["changed_slots"] == 3
    assert patch["diff"]["churn"] == 0.25

    # No write before approval.
    still_v1 = client.get("/api/v1/plans/current", headers=headers).json()["data"]
    assert still_v1["version"] == 1

    approved = client.post(
        f"/api/v1/patches/{patch['id']}/approve",
        headers=headers,
        json={"comment": "ok"},
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()["data"]
    assert body["committed_version"] == 2
    assert body["plan"]["version"] == 2
    refs = {item["action_ref"] for item in body["plan"]["actions"]}
    assert "goblet_squat" not in refs
    assert "glute_bridge" in refs
    assert "easy_walk" in refs


def test_reject_keeps_old_version(tmp_path) -> None:
    """驳回只改补丁状态，计划仍停在 v1。"""
    client, headers = _client_and_token(tmp_path)
    plan = client.get("/api/v1/plans/current", headers=headers).json()["data"]
    patch = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=headers,
        json={"reason_code": "injury", "available_days": 2},
    ).json()["data"]
    rejected = client.post(
        f"/api/v1/patches/{patch['id']}/reject",
        headers=headers,
        json={"comment": "不同意"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["data"]["status"] == "rejected"
    current = client.get("/api/v1/plans/current", headers=headers).json()["data"]
    assert current["version"] == 1


def test_rollback_creates_new_version(tmp_path) -> None:
    """回滚是"再提交一个版本"：v2 之后回滚到 v1 应得到 v3。"""
    client, headers = _client_and_token(tmp_path)
    plan = client.get("/api/v1/plans/current", headers=headers).json()["data"]
    patch = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=headers,
        json={"reason_code": "injury", "available_days": 2},
    ).json()["data"]
    client.post(f"/api/v1/patches/{patch['id']}/approve", headers=headers, json={"comment": "ok"})

    rolled = client.post(
        f"/api/v1/plans/{plan['plan_id']}/rollback",
        headers=headers,
        json={"target_version": 1},
    )
    assert rolled.status_code == 200, rolled.text
    body = rolled.json()["data"]
    assert body["new_version"] == 3
    assert body["plan"]["version"] == 3
    assert len(body["plan"]["actions"]) == 12
