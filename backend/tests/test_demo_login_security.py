"""演示登录只能显式开启并签发固定账号，不能绕过真实账号登录。"""
import pytest

from app.config import Settings
from app.seed import DEMO_USER_ID
from tests.conftest import register_and_login


def test_demo_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DEMO_LOGIN_ENABLED", raising=False)
    assert Settings(_env_file=None).demo_login_enabled is False


def test_enabled_demo_only_logs_into_seed_user(client):
    assert client.get("/api/v1/auth/options").json()["data"] == {"demo_login_enabled": True}
    response = client.post("/api/v1/auth/demo", json={})
    assert response.status_code == 200
    assert response.json()["data"]["user_id"] == DEMO_USER_ID


@pytest.mark.parametrize("target", [DEMO_USER_ID, "another-account", None])
def test_demo_rejects_client_selected_identity(client, target):
    response = client.post("/api/v1/auth/demo", json={"user_id": target})
    assert response.status_code == 422
    assert "access_token" not in response.json()


def test_closed_demo_preserves_phone_login_and_admin_switch(client):
    client.app.state.settings.demo_login_enabled = False
    assert client.get("/api/v1/auth/options").json()["data"] == {"demo_login_enabled": False}
    assert client.post("/api/v1/auth/demo", json={}).status_code == 404
    headers = register_and_login(client, phone="13772692821")
    assert client.get("/api/v1/auth/me", headers=headers).json()["data"]["is_llm_admin"] is True
    response = client.put("/api/v1/admin/llm-switch", headers=headers, json={"paused": True})
    assert response.status_code == 200
    assert response.json()["data"]["paused"] is True
    assert client.get("/api/v1/admin/llm-switch", headers=headers).status_code == 200
    assert client.put("/api/v1/admin/llm-switch", headers=headers, json={"paused": False}).status_code == 200
