"""pytest 共享 fixture。

`client` 是绝大多数用例的入口：每个用例一份**独立的临时 SQLite 库**，
用完即弃，用例之间不会互相污染（seed 会在每个库里重灌 demo 用户）。
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
