from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        secret_key="test-secret-0123456789abcdef0123456789",
        cors_origins="http://localhost:5173",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient) -> str:
    response = client.post("/api/v1/auth/demo", json={})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 0
    return payload["data"]["access_token"]


def test_health(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["status"] == "ok"
    assert body["trace_id"]
    assert response.headers["x-trace-id"] == body["trace_id"]


def test_demo_login_and_profile(client: TestClient) -> None:
    token = _login(client)
    response = client.get(
        "/api/v1/me/profile",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    profile = response.json()["data"]
    assert profile["height_cm"] == 178
    assert profile["weekly_days"] == 3


def test_current_plan_has_twelve_actions(client: TestClient) -> None:
    token = _login(client)
    response = client.get(
        "/api/v1/plans/current",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    plan = response.json()["data"]
    assert plan["version"] == 1
    assert plan["calorie_target"] == 1800
    assert len(plan["actions"]) == 12
    assert plan["actions"][0]["slot_key"]


def test_missing_token_is_401(client: TestClient) -> None:
    response = client.get("/api/v1/me/profile")
    assert response.status_code == 401


def test_wechat_is_not_configured(client: TestClient) -> None:
    response = client.post("/api/v1/auth/wechat", json={"code": "x"})
    assert response.status_code == 501
