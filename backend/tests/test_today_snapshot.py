"""今日规划按天保存：同一天只留一份、重新规划覆盖、麦当劳那块也在里面。

另外覆盖"按 IP 猜城市"的纯函数部分（不联网）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.domain import today_snapshot
from app.food import geo


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


# ---------------------------------------------------------------- 定位

def test_city_for_search_strips_suffix() -> None:
    assert geo.city_for_search("济南市") == "济南"
    assert geo.city_for_search("北京市") == "北京"
    assert geo.city_for_search("延边朝鲜族自治州") == "延边朝鲜族"
    assert geo.city_for_search("上海") == "上海"


def test_mask_hides_last_octet() -> None:
    assert geo.mask("120.244.23.39") == "120.244.23.xxx"
    assert geo.mask("2001:db8::1") == "2001:db8:xxxx"
    assert geo.mask("nope") == ""


def test_private_ip_detection() -> None:
    assert geo.is_private("127.0.0.1") is True
    assert geo.is_private("192.168.1.8") is True
    assert geo.is_private("10.0.0.2") is True
    assert geo.is_private("120.244.23.39") is False
    assert geo.is_private("") is True


def test_locate_prefers_client_ip(monkeypatch) -> None:
    monkeypatch.setattr(geo, "_lookup", lambda ip: {
        "status": "success", "city": "上海市", "regionName": "上海", "query": ip or "1.1.1.1"})

    class Req:
        headers = {"x-forwarded-for": "120.244.23.39, 10.0.0.1"}

        class client:
            host = "10.0.0.1"

    found = geo.locate_from_request(Req())
    assert found["source"] == "ip"
    assert found["city"] == "上海市"
    assert found["city_for_search"] == "上海"
    assert found["ip"] == "120.244.23.xxx"


def test_locate_falls_back_to_server_ip(monkeypatch) -> None:
    """本机开发（127.0.0.1）拿不到位置，就用服务器出口 IP 兜底。"""
    monkeypatch.setattr(geo, "_lookup", lambda ip: {
        "status": "success", "city": "北京", "regionName": "北京市", "query": "120.244.23.39"})

    class Req:
        headers = {}

        class client:
            host = "127.0.0.1"

    found = geo.locate_from_request(Req())
    assert found["source"] == "server_ip"
    assert found["city_for_search"] == "北京"
    # 展示的是真正用来定位的那个出口 IP，而不是 127.0.0.1
    assert found["ip"] == "120.244.23.xxx"


def test_locate_unknown_when_lookup_fails(monkeypatch) -> None:
    monkeypatch.setattr(geo, "_lookup", lambda ip: {})

    class Req:
        headers = {}

        class client:
            host = "127.0.0.1"

    assert geo.locate_from_request(Req())["source"] == "unknown"


@pytest.mark.parametrize("city,keyword", [("上海", "人民广场")])
def test_stores_route_requires_login(client: TestClient, city: str, keyword: str) -> None:
    assert client.get("/api/v1/mcd/stores",
                      params={"city": city, "keyword": keyword}).status_code == 401
