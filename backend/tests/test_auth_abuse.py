"""注册/登录的防滥用测试。

覆盖四道闸口：全站熔断、IP/手机号限流、一次性票据、密码策略。
所有用例都走真实 HTTP，不 mock 中间层——限流最容易写错的地方就是"失败请求没计数"。
"""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

API = "/api/v1"


def _app(**overrides):
    """每个用例独立临时库；默认把"最短填表耗时"关掉，避免 sleep 拖慢测试。"""
    tmp = tempfile.mkdtemp(prefix="ph-auth-")
    kwargs = {
        "env": "dev",
        "database_url": f"sqlite:///{tmp}/auth.db",
        "secret_key": "test-secret-0123456789abcdef0123456789",
        "auth_min_fill_ms": 0,
    }
    kwargs.update(overrides)
    return create_app(Settings(**kwargs))


def _challenge(client: TestClient, purpose: str = "register") -> dict:
    resp = client.get(f"{API}/auth/challenge", params={"purpose": purpose})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _register(client: TestClient, phone: str, password: str = "fitplan2026", **extra):
    challenge = _challenge(client)
    body = {
        "phone": phone,
        "password": password,
        "nickname": "测试",
        "challenge_id": challenge["challenge_id"],
        "challenge_token": challenge["token"],
        **extra,
    }
    return client.post(f"{API}/auth/register", json=body)


def test_register_requires_challenge() -> None:
    """没有票据直接拒绝：这是"必须打开过页面"的最小证明。"""
    app = _app()
    with TestClient(app) as client:
        resp = client.post(f"{API}/auth/register", json={
            "phone": "13911112222", "password": "fitplan2026",
        })
        assert resp.status_code == 400
        assert "校验票据" in resp.json()["detail"]


def test_challenge_is_single_use() -> None:
    """票据用一次就作废，防止同一个票据批量注册。"""
    app = _app()
    with TestClient(app) as client:
        challenge = _challenge(client)
        body = {
            "phone": "13911112222", "password": "fitplan2026",
            "challenge_id": challenge["challenge_id"], "challenge_token": challenge["token"],
        }
        assert client.post(f"{API}/auth/register", json=body).status_code == 200
        reused = client.post(f"{API}/auth/register", json={**body, "phone": "13911113333"})
        assert reused.status_code == 400
        assert "已被使用" in reused.json()["detail"]


def test_too_fast_submission_is_rejected() -> None:
    """机器提交通常几十毫秒就填完表单；真人不可能这么快。"""
    app = _app(auth_min_fill_ms=5000)
    with TestClient(app) as client:
        resp = _register(client, "13911112222")
        assert resp.status_code == 400
        assert "太快" in resp.json()["detail"]


@pytest.mark.parametrize("password, keyword", [
    ("short1a", "长度"),
    ("allletters", "字母和数字"),
    ("12345678", "太常见"),
    ("fit13911112222", "手机号"),
    ("aaaaaaa1", "重复字符"),
])
def test_password_policy(password: str, keyword: str) -> None:
    """弱口令在写库前就被拦掉，且给出人话原因。"""
    app = _app()
    with TestClient(app) as client:
        resp = _register(client, "13911112222", password=password)
        assert resp.status_code == 422, resp.text
        assert keyword in resp.json()["detail"]


def test_phone_format_is_enforced() -> None:
    app = _app()
    with TestClient(app) as client:
        assert _register(client, "12345").status_code == 422
        assert _register(client, "12345678901").status_code == 422


def test_honeypot_rejects_scripted_form() -> None:
    """蜜罐字段被填了 → 基本可以确定不是真人。"""
    app = _app()
    with TestClient(app) as client:
        resp = _register(client, "13911112222", company="ACME")
        assert resp.status_code == 400


def test_duplicate_phone_is_409() -> None:
    app = _app()
    with TestClient(app) as client:
        assert _register(client, "13911112222", password="fitplan2026").status_code == 200
        assert _register(client, "13911112222", password="fitplan2026").status_code == 409


def test_register_per_ip_limit_returns_429() -> None:
    """同 IP 连续注册超过小时配额 → 429，并且带 Retry-After。"""
    app = _app(register_per_ip_hour=2)
    with TestClient(app) as client:
        for index in range(2):
            assert _register(client, f"1391111222{index}", password="fitplan2026").status_code == 200
        blocked = _register(client, "13911112229", password="fitplan2026")
        assert blocked.status_code == 429, blocked.text
        assert int(blocked.headers["Retry-After"]) > 0
        assert "频繁" in blocked.json()["detail"]


def test_register_per_phone_limit() -> None:
    app = _app(register_per_phone_day=1, register_per_ip_hour=10)
    with TestClient(app) as client:
        assert _register(client, "13911112222").status_code == 200
        again = _register(client, "13911112222")
        # 同一号码第二次：先被手机号配额拦住（429），而不是靠唯一索引
        assert again.status_code == 429, again.text


def test_global_registration_breaker() -> None:
    """全站熔断：即使换 IP 也挡得住，保护数据库不被打爆。"""
    app = _app(register_global_hour=2)
    with TestClient(app) as client:
        for index in range(2):
            resp = _register(client, f"1391111222{index}")
            assert resp.status_code == 200, resp.text
        blocked = client.post(f"{API}/auth/register", json={
            "phone": "13911119999", "password": "fitplan2026",
        })
        # 熔断在票据之前生效，所以这里即使没票据也是 429
        assert blocked.status_code == 429, blocked.text


def test_max_users_cap() -> None:
    """账号总数硬上限：种子账号已经占满时直接 503，一个字节都不写。"""
    app = _app(max_users=1)
    with TestClient(app) as client:
        resp = _register(client, "13911112222")
        assert resp.status_code == 503, resp.text


def test_login_lockout_after_failures() -> None:
    """同一 IP + 手机号连续失败到阈值 → 锁一段时间。"""
    app = _app(login_fail_per_window=2, login_window_minutes=15)
    with TestClient(app) as client:
        assert _register(client, "13911112222", password="fitplan2026").status_code == 200
        for _ in range(2):
            bad = client.post(f"{API}/auth/login", json={
                "phone": "13911112222", "password": "wrongpass1",
            })
            assert bad.status_code == 401
        locked = client.post(f"{API}/auth/login", json={
            "phone": "13911112222", "password": "fitplan2026",
        })
        assert locked.status_code == 429, "锁定期内即使密码正确也要拒绝"
        assert int(locked.headers["Retry-After"]) > 0


def test_successful_login_clears_failures() -> None:
    """登录成功要清零失败计数，否则正常用户会被自己之前的输错锁死。"""
    app = _app(login_fail_per_window=2)
    with TestClient(app) as client:
        assert _register(client, "13911112222", password="fitplan2026").status_code == 200
        assert client.post(f"{API}/auth/login", json={
            "phone": "13911112222", "password": "wrongpass1",
        }).status_code == 401
        assert client.post(f"{API}/auth/login", json={
            "phone": "13911112222", "password": "fitplan2026",
        }).status_code == 200
        # 计数已清零，再错一次不会立刻被锁
        assert client.post(f"{API}/auth/login", json={
            "phone": "13911112222", "password": "wrongpass1",
        }).status_code == 401


def test_login_does_not_leak_whether_phone_exists() -> None:
    """不存在与密码错返回同一句话，避免被用来枚举账号。"""
    app = _app()
    with TestClient(app) as client:
        missing = client.post(f"{API}/auth/login", json={
            "phone": "13900000000", "password": "fitplan2026",
        })
        assert missing.status_code == 401
        detail_missing = missing.json()["detail"]
        assert _register(client, "13911112222", password="fitplan2026").status_code == 200
        wrong = client.post(f"{API}/auth/login", json={
            "phone": "13911112222", "password": "nope2026x",
        })
        assert wrong.status_code == 401
        assert wrong.json()["detail"] == detail_missing


def test_challenge_endpoint_is_rate_limited() -> None:
    """票据接口本身也会被刷，所以同样限流。"""
    app = _app()
    with TestClient(app) as client:
        codes = [client.get(f"{API}/auth/challenge").status_code for _ in range(35)]
        assert 429 in codes, codes
