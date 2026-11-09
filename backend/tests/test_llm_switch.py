"""运营总闸：只允许指定手机号暂停/恢复 DeepSeek，并在暂停时直接 503。"""

from __future__ import annotations

import pytest

from app.llm.guard import LLMDisabledError, PAUSED_MESSAGE
from app.llm.tool_client import ToolCallingClient
from app.models import User


def _make_admin(client, auth_headers, app):
    me = client.get("/api/v1/auth/me", headers=auth_headers).json()["data"]
    admin_phone = app.state.settings.llm_admin_phone
    with app.state.session_factory() as db:
        user = db.get(User, me["user_id"])
        user.phone = admin_phone
        db.commit()
    return client.get("/api/v1/auth/me", headers=auth_headers).json()["data"]


def test_llm_switch_rejects_non_admin(client, auth_headers) -> None:
    assert client.get("/api/v1/admin/llm-switch", headers=auth_headers).status_code == 403
    assert client.put(
        "/api/v1/admin/llm-switch", headers=auth_headers, json={"paused": True}
    ).status_code == 403


def test_llm_switch_pauses_today_plan(client, auth_headers, app_fixture) -> None:
    _, app = app_fixture
    me = _make_admin(client, auth_headers, app)
    assert me["is_llm_admin"] is True

    state = client.get("/api/v1/admin/llm-switch", headers=auth_headers).json()["data"]
    assert state["paused"] is False

    paused = client.put(
        "/api/v1/admin/llm-switch", headers=auth_headers, json={"paused": True}
    ).json()["data"]
    assert paused["paused"] is True

    public = client.get("/api/v1/system/status").json()["data"]
    assert public == {"paused": True, "message": PAUSED_MESSAGE}

    blocked = client.post(
        "/api/v1/agent/today-plan", headers=auth_headers, json={"text": "帮我规划今天"}
    )
    assert blocked.status_code == 503
    assert blocked.json()["detail"] == PAUSED_MESSAGE

    # 底层客户端也必须拦住：即使别的接口漏了依赖，也不能真的发 DeepSeek 请求
    with pytest.raises(LLMDisabledError):
        ToolCallingClient(api_key="test", base_url="http://127.0.0.1:9", model="test").complete([])

    resumed = client.put(
        "/api/v1/admin/llm-switch", headers=auth_headers, json={"paused": False}
    ).json()["data"]
    assert resumed["paused"] is False
    assert client.get("/api/v1/system/status").json()["data"]["paused"] is False


def test_llm_switch_blocks_plan_draft(client, auth_headers, app_fixture) -> None:
    _, app = app_fixture
    _make_admin(client, auth_headers, app)
    client.put("/api/v1/admin/llm-switch", headers=auth_headers, json={"paused": True})
    resp = client.post(
        "/api/v1/plans/drafts", headers=auth_headers, json={"instruction": "减脂，每周3天"}
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == PAUSED_MESSAGE


def test_skill_env_does_not_inherit_secrets(monkeypatch) -> None:
    from app.skill.bridge import _skill_env

    monkeypatch.setenv("DEEPSEEK_API_KEY", "should-not-leak")
    monkeypatch.setenv("SECRET_KEY", "should-not-leak")
    env = _skill_env("/tmp/skill")
    assert "DEEPSEEK_API_KEY" not in env
    assert "SECRET_KEY" not in env
    assert env["SKILL_DIR"] == "/tmp/skill"
    assert env["PATH"]


def test_settings_can_load_key_from_file(tmp_path) -> None:
    from app.config import Settings

    key_file = tmp_path / "deepseek.key"
    key_file.write_text("sk-from-file\n", encoding="utf-8")
    settings = Settings(
        deepseek_api_key="",
        deepseek_api_key_file=str(key_file),
        secret_key="test-secret-0123456789abcdef",
    )
    assert settings.deepseek_api_key == "sk-from-file"


def test_pause_keeps_auth_and_saved_reads(client, auth_headers, app_fixture) -> None:
    """维护开关只拦 Agent 调用，不能把登录/注册/查看已保存内容一起停掉。"""
    _, app = app_fixture
    from app.llm.guard import set_llm_paused

    # 先生成一份已保存的今日规划
    created = client.post(
        "/api/v1/agent/today-plan", headers=auth_headers, json={"text": "帮我规划今天"}
    ).json()["data"]
    run = client.get(f"/api/v1/runs/{created['run_id']}", headers=auth_headers).json()["data"]
    assert run["status"] == "succeeded"

    with app.state.session_factory() as db:
        set_llm_paused(db, True)
        db.commit()

    # 已注册用户仍能登录；新用户仍能注册
    from tests.conftest import register_and_login

    register_and_login(client, phone="13900000001")
    register_and_login(client, phone="13900000002")

    # 已保存内容仍可读；只是 Agent 生成被拦
    saved = client.get("/api/v1/agent/today-plan", headers=auth_headers)
    assert saved.status_code == 200
    assert saved.json()["data"] is not None
    blocked = client.post(
        "/api/v1/agent/today-plan", headers=auth_headers, json={"text": "帮我规划今天"}
    )
    assert blocked.status_code == 503
