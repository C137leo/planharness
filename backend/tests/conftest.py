"""pytest 共享 fixture。

`client` 是绝大多数用例的入口：每个用例一份**独立的临时 SQLite 库**，
用完即弃，用例之间不会互相污染（seed 会在每个库里重灌 demo 用户）。
"""

from __future__ import annotations

import os

# 在导入 app 之前把环境"钉住"：pytest 会在导入测试模块前先导入 conftest，
# 所以这里设置的变量对所有用例生效。
# 原因：app/main.py 在导入时会执行 `app = create_app()`，如果外部 shell
# 里恰好 export 了 DATABASE_URL（比如指向某个没启动的 Postgres），
# 整个测试收集阶段就会因为连不上库而失败。测试不该依赖开发机环境。
os.environ["ENV"] = "dev"
os.environ["DATABASE_URL"] = "sqlite://"          # 纯内存，导入期建表用，跑完即弃
os.environ["DEEPSEEK_API_KEY"] = ""               # 测试绝不联网、不花钱
os.environ.pop("CHECKPOINTER_BACKEND", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    """构造指向临时库的 TestClient；每个用例一份全新数据库。"""
    settings = Settings(
        env="dev",
        database_url=f"sqlite:///{tmp_path}/test.db",
        secret_key="test-secret-0123456789abcdef0123456789",
        cors_origins="http://localhost:5173",
        # 一次性票据的"最短填表耗时"由 tests/test_auth_abuse.py 专门测；
        # 其余用例不需要为此真的 sleep 一两秒。
        auth_min_fill_ms=0,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


DEMO_PHONE = "13800000000"
DEMO_PASSWORD = "test-pass-2026"


def register_user(
    client: TestClient,
    phone: str = DEMO_PHONE,
    password: str = DEMO_PASSWORD,
    **extra,
):
    """申请一次性票据再注册（注册接口要求票据，测试也走真实路径）。"""
    challenge = client.get("/api/v1/auth/challenge", params={"purpose": "register"}).json()["data"]
    return client.post(
        "/api/v1/auth/register",
        json={
            "phone": phone,
            "password": password,
            "nickname": "测试用户",
            "challenge_id": challenge["challenge_id"],
            "challenge_token": challenge["token"],
            **extra,
        },
    )


def register_and_login(
    client: TestClient,
    phone: str = DEMO_PHONE,
    password: str = DEMO_PASSWORD,
) -> dict[str, str]:
    """注册一个新账号并登录，返回可以直接用的 Authorization 头。

    为什么必须"先注册再登录"：`/auth/login` 只做校验，不会创建账号；
    库里没有这个手机号时，登录一定是 401。
    """
    reg = register_user(client, phone=phone, password=password)
    assert reg.status_code == 200, f"注册失败：{reg.status_code} {reg.text}"

    resp = client.post(
        "/api/v1/auth/login",
        json={"phone": phone, "password": password},
    )
    assert resp.status_code == 200, f"登录失败：{resp.status_code} {resp.text}"
    body = resp.json()
    assert body["code"] == 0, body
    return {"Authorization": f"Bearer {body['data']['access_token']}"}


@pytest.fixture()
def auth_headers(client: TestClient) -> dict[str, str]:
    """需要"已登录用户"的用例直接声明这个 fixture。

    用 seed 里预置的 demo 账号，不额外注册；要测注册流程本身请用
    `register_and_login()`。
    """
    resp = client.post("/api/v1/auth/demo", json={})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture()
def new_user_headers(client: TestClient) -> dict[str, str]:
    """注册一个新账号并登录（测"真实账号"路径时用）。"""
    return register_and_login(client)


import pytest as _pytest


@_pytest.fixture()
def app_fixture(client):
    """同时拿到 TestClient 和 app（需要 app.state.session_factory 的用例用）。"""
    return client, client.app
