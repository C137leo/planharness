"""接口层冒烟测试：响应包装、trace_id、鉴权、demo 数据可用性。

每个用例都用临时 SQLite 库（tmp_path），互不干扰。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    """构造指向临时库的 TestClient；每个用例一份全新数据库。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        secret_key="test-secret-0123456789abcdef0123456789",
        cors_origins="http://localhost:5173",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient) -> str:
    """走 demo 登录拿 JWT，供需要鉴权的用例复用。"""
    response = client.post("/api/v1/auth/demo", json={})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 0
    return payload["data"]["access_token"]


def test_health(client: TestClient) -> None:
    """健康检查应真实连通数据库，且响应体与响应头的 trace_id 一致。"""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["status"] == "ok"
    assert body["trace_id"]
    assert response.headers["x-trace-id"] == body["trace_id"]


def test_demo_login_and_profile(client: TestClient) -> None:
    """demo 登录后能读到预置画像（身高/周训练天数与 seed 一致）。"""
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
    """seed 的 v1 计划应是 12 个槽位、1800 kcal 的固定内容。"""
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
    """不带 token 访问受保护接口必须 401，不能靠默认用户兜底。"""
    response = client.get("/api/v1/me/profile")
    assert response.status_code == 401


def test_wechat_is_not_configured(client: TestClient) -> None:
    """无 AppID 时微信登录返回 501，明确告知未实现而非假装成功。"""
    response = client.post("/api/v1/auth/wechat", json={"code": "x"})
    assert response.status_code == 501


def test_docs_uses_local_swagger_assets(client: TestClient) -> None:
    """/docs 必须从本站加载 Swagger UI。

    jsdelivr 在国内会被重置，走 CDN 时浏览器只拿到一张白页；
    这个用例把"离线可用"钉死，防止以后有人把 docs_url 改回默认值。
    """
    page = client.get("/docs")
    assert page.status_code == 200
    html = page.text
    assert "/static/swagger/swagger-ui-bundle.js" in html
    assert "/static/swagger/swagger-ui.css" in html
    assert "cdn.jsdelivr.net" not in html
    assert client.get("/static/swagger/swagger-ui-bundle.js").status_code == 200
