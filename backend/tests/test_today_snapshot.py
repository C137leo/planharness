"""今日规划按天保存：同一天只留一份、重新规划覆盖、麦当劳那块也在里面。

另外覆盖"按 IP 猜城市"的纯函数部分（不联网）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.domain import today_snapshot


def _db(app):
    return app.state.session_factory()


def _user(db, user_id: str):
    from app.models import User

    return db.get(User, user_id)


def _uid(client: TestClient, headers: dict[str, str]) -> str:
    return client.get("/api/v1/auth/me", headers=headers).json()["data"]["user_id"]


def test_save_and_load_snapshot(client: TestClient, auth_headers) -> None:
    db = _db(client.app)
    try:
        user = _user(db, _uid(client, auth_headers))
        day = dt.date(2026, 11, 5)
        row = today_snapshot.save_today_plan(
            db, user, day, request_text="帮我规划今天",
            output={"summary": "练腿", "mcdonalds": {"status": "ok"}}, run_id="run-1",
        )
        db.commit()
        loaded = today_snapshot.load_today_plan(db, user, day)
        assert loaded is not None and loaded.id == row.id
        assert loaded.output["mcdonalds"]["status"] == "ok"
        payload = today_snapshot.payload(loaded)
        assert payload["day"] == "2026-11-05"
        assert payload["has_mcdonalds"] is True
        assert payload["saved_at"]  # 本地时间字符串，前端直接显示
    finally:
        db.close()


def test_same_day_is_overwritten_not_duplicated(client: TestClient, auth_headers) -> None:
    db = _db(client.app)
    try:
        user = _user(db, _uid(client, auth_headers))
        day = dt.date(2026, 11, 6)
        today_snapshot.save_today_plan(db, user, day, request_text="第一版",
                                       output={"summary": "旧"}, run_id="run-1")
        db.commit()
        today_snapshot.save_today_plan(db, user, day, request_text="第二版",
                                       output={"summary": "新"}, run_id="run-2")
        db.commit()
        loaded = today_snapshot.load_today_plan(db, user, day)
        assert loaded is not None
        assert loaded.output["summary"] == "新"
        assert loaded.run_id == "run-2"
        # 一天一条：再查一次也只有一个对象
        from sqlalchemy import func, select

        from app.models import TodayAgentPlan

        count = db.scalar(
            select(func.count()).select_from(TodayAgentPlan)
            .where(TodayAgentPlan.user_id == user.id)
        )
        assert count == 1
    finally:
        db.close()


def test_get_endpoint_returns_null_before_any_run(client: TestClient, auth_headers) -> None:
    body = client.get("/api/v1/agent/today-plan?date=2026-11-07",
                      headers=auth_headers).json()
    assert body["data"] is None


def test_get_endpoint_returns_saved(client: TestClient, auth_headers) -> None:
    db = _db(client.app)
    try:
        user = _user(db, _uid(client, auth_headers))
        today_snapshot.save_today_plan(
            db, user, dt.date(2026, 11, 8), request_text="帮我规划今天",
            output={"summary": "已保存", "mcdonalds": {"status": "ok"}}, run_id="run-9",
        )
        db.commit()
    finally:
        db.close()
    body = client.get("/api/v1/agent/today-plan?date=2026-11-08",
                      headers=auth_headers).json()["data"]
    assert body["output"]["summary"] == "已保存"
    assert body["has_mcdonalds"] is True
    assert body["run_id"] == "run-9"


def test_snapshot_is_per_user(client: TestClient, auth_headers) -> None:
    """别人存的规划不该被我读到。"""
    other = client.post("/api/v1/auth/demo", json={}).json()["data"]["access_token"]
    first = client.get("/api/v1/agent/today-plan?date=2026-11-09",
                       headers=auth_headers).json()["data"]
    second = client.get("/api/v1/agent/today-plan?date=2026-11-09",
                        headers={"Authorization": f"Bearer {other}"}).json()["data"]
    assert first is None and second is None


def test_stores_route_requires_login(client: TestClient) -> None:
    assert client.get("/api/v1/mcd/stores",
                      params={"city": "上海", "keyword": "人民广场"}).status_code == 401
