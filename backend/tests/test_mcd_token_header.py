"""MCD 用户 token：请求头透传 + 内置口令映射。"""

from __future__ import annotations

from app.food import mcd_order


def test_stores_endpoint_forwards_user_token(client, auth_headers, monkeypatch) -> None:
    captured: dict = {}

    def fake_find_stores(city, keyword, *, client=None, token=None):
        captured.update(city=city, keyword=keyword, token=token)
        return {"status": "ok", "data": {"stores": []}, "citations": []}

    monkeypatch.setattr(mcd_order, "find_stores", fake_find_stores)
    resp = client.get(
        "/api/v1/mcd/stores",
        params={"city": "北京", "keyword": "国贸"},
        headers={**auth_headers, "X-MCD-Token": "user-token"},
    )
    assert resp.status_code == 200, resp.text
    assert captured == {"city": "北京", "keyword": "国贸", "token": "user-token"}
