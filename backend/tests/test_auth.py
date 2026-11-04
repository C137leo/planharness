"""账号体系与 JWT 的测试：注册、登录、鉴权、token 撤销。

对应 docs/12-architecture.md 里「身份体系」那一节：
- 注册即建画像（否则 /me/profile 404）
- 登录失败话术统一（不泄露手机号是否注册）
- token_version +1 → 旧 token 立刻失效（4.4 的核心机制）
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import User
from tests.conftest import register_user

PHONE = "13800000000"
PASSWORD = "s3cure-pass-2026"


def _register(
    client: TestClient, phone: str = PHONE, password: str = PASSWORD
):
    """注册一个账号，返回原始响应（不同用例关心不同字段）。"""
    return register_user(client, phone=phone, password=password)


def _login(client: TestClient, phone: str = PHONE, password: str = PASSWORD):
    return client.post("/api/v1/auth/login", json={"phone": phone, "password": password})


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_register_creates_user_and_profile(client: TestClient) -> None:
    """注册成功应直接返回可用 token，且画像已自动建好。"""
    response = _register(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0
    assert body["message"] == "registered"

    token = body["data"]["access_token"]
    me = client.get("/api/v1/me/profile", headers=_auth(token))
    assert me.status_code == 200, me.text
    profile = me.json()["data"]
    # Profile 的默认值来自模型定义；这里只钉住"画像存在且可用"
    assert profile["weekly_days"] == 3
    assert profile["sex"] == "male"


def test_register_duplicate_phone_is_409(client: TestClient) -> None:
    """同一手机号重复注册必须 409，而不是建出第二个账号。"""
    assert _register(client).status_code == 200
    duplicate = _register(client)
    assert duplicate.status_code == 409
    assert "已注册" in duplicate.json()["detail"]


def test_register_rejects_short_password(client: TestClient) -> None:
    """密码长度由 Pydantic 挡在入口（422），不该进到哈希那一步。"""
    assert _register(client, password="123").status_code == 422


def test_login_failure_messages_are_identical(client: TestClient) -> None:
    """密码错与手机号不存在返回同一句话，防止被用来枚举手机号。"""
    _register(client)

    wrong_password = _login(client, password="wrong-pass-123")
    unknown_phone = _login(client, phone="13900000000")

    assert wrong_password.status_code == 401
    assert unknown_phone.status_code == 401
    assert wrong_password.json()["detail"] == unknown_phone.json()["detail"]


def test_protected_route_requires_valid_token(client: TestClient) -> None:
    """没带 token、带垃圾 token 都要 401；带合法 token 才 200。"""
    assert client.get("/api/v1/me/profile").status_code == 401
    assert client.get("/api/v1/me/profile", headers=_auth("not-a-jwt")).status_code == 401

    token = _register(client).json()["data"]["access_token"]
    assert client.get("/api/v1/me/profile", headers=_auth(token)).status_code == 200


def test_token_is_revoked_after_version_bump(client: TestClient) -> None:
    """token_version +1 后旧 token 立刻 401，重新登录的新 token 可用。

    这是「无状态 JWT 怎么实现撤销」的最小验证：
    改密码 / 强制下线时只要把 token_version 加一，所有旧 token 全部作废。
    """
    token = _register(client).json()["data"]["access_token"]
    assert client.get("/api/v1/me/profile", headers=_auth(token)).status_code == 200

    # 模拟「改密码」：直接把版本号加一
    with client.app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.phone == PHONE))
        assert user is not None
        user.token_version += 1
        session.commit()

    revoked = client.get("/api/v1/me/profile", headers=_auth(token))
    assert revoked.status_code == 401
    assert "revoked" in revoked.json()["detail"]

    # 重新登录拿到的新 token 带上了新的 ver，立即可用
    fresh = _login(client)
    assert fresh.status_code == 200
    new_token = fresh.json()["data"]["access_token"]
    assert client.get("/api/v1/me/profile", headers=_auth(new_token)).status_code == 200
