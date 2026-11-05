"""麦当劳点餐链路：官方返回的解析、餐名对商品、算价/建单/取消、以及路由的护栏。

全程不联网：用假的 httpx client 回放官方那种"说明 + JSON + 备注"的文本。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.food import mcd_mcp, mcd_order

TOKEN = "tok"
URL = "https://mcp.mcd.cn"


class FakeResponse:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class FakeHttp:
    """按顺序回放响应，并记录每次请求。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


def _init() -> FakeResponse:
    return FakeResponse(200, json.dumps({"jsonrpc": "2.0", "id": 1,
                                         "result": {"protocolVersion": "2025-06-18"}}))


def _initialized() -> FakeResponse:
    return FakeResponse(202, "")


def _tool_text(payload: dict | str) -> FakeResponse:
    """官方 tools/call 返回：content 里塞一段"说明 + JSON"的文本。"""
    text = payload if isinstance(payload, str) else (
        "## Response Structure\n- **code**: 错误码\n\n## Original Response\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n## 展示结果时，每个门店的 storeCode 必须完整展示"
    )
    body = {"jsonrpc": "2.0", "id": 2,
            "result": {"content": [{"type": "text", "text": text}], "isError": False}}
    return FakeResponse(200, json.dumps(body, ensure_ascii=False))


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(mcd_mcp, "MCD_MCP_URL", URL)
    monkeypatch.setattr(mcd_mcp, "MCD_MCP_TOKEN", TOKEN)
    monkeypatch.setattr(mcd_mcp, "DEFAULT_TOOL", "")
    monkeypatch.setattr(mcd_mcp, "_CACHE", {"at": 0.0, "items": [], "fetched_at": ""})
    yield


STORES = {"success": True, "code": 200, "message": "请求成功", "data": [
    {"storeCode": "1450713", "storeName": "麦当劳上海黄浦华旭国际大厦餐厅",
     "address": "西藏中路268号", "distance": 355, "businessStatus": True,
     "businessStartTime": "06:30", "businessEndTime": "23:00"},
    {"storeCode": "1450441", "storeName": "麦当劳上海福州路餐厅",
     "address": "福州路666号", "distance": 412, "businessStatus": True,
     "businessStartTime": "07:00", "businessEndTime": "23:00"},
]}

MENU = {"success": True, "code": 200, "message": "请求成功", "data": {
    "categories": [{"name": "蘸酱炸鸡\n小食", "meals": [{"code": "520445"}, {"code": "4810"}]}],
    "meals": {
        "520445": {"name": "薄皮焦香V翅", "currentPrice": "16", "originalPrice": "16",
                   "productType": 1, "image": "https://menu-img.mcd.cn/a.png"},
        "4810": {"name": "薯条", "currentPrice": "13.5", "originalPrice": "15",
                 "productType": 1, "image": "https://menu-img.mcd.cn/b.png"},
    },
}}

QUOTE = {"success": True, "code": 200, "message": "请求成功", "data": {
    "price": 2950, "originalPrice": 3100, "discount": 150,
    "takeWayList": [{"code": "eat-in", "title": "堂食", "subtitle": ""},
                    {"code": "locker-out", "title": "外带", "subtitle": ""}],
    "productList": [{"productCode": "520445", "productName": "薄皮焦香V翅",
                     "quantity": 1, "subtotal": 1600}],
}}

ORDER = {"success": True, "code": 200, "message": "请求成功", "data": {
    "orderId": "1030722560000808903093418679", "payId": "111038555175742332928",
    "payH5Url": "https://m.mcd.cn/mcp/scanToPay?orderId=1030722560000808903093418679",
    "orderDetail": {
        "orderId": "1030722560000808903093418679", "orderStatus": "待支付",
        "storeName": "麦当劳上海黄浦华旭国际大厦餐厅", "totalAmount": "2.95",
        "totalDiscountAmount": "1.5", "takeWay": "外带",
        "expirePayTime": "2026-11-05 21:02:29",
        "orderProductList": [{"productName": "薄皮焦香V翅", "quantity": 1, "price": "16"}],
    },
}}

CANCEL = {"success": True, "code": 200, "message": "请求成功",
          "data": {"cancelResult": True, "orderId": "1030722560000808903093418679"}}


# ---------------------------------------------------------------- 解析

def test_find_stores_requires_city_and_keyword() -> None:
    result = mcd_order.find_stores("", "人民广场")
    assert result["status"] == "error"
    assert result["error"] == "mcd_need_city_and_keyword"


def test_find_stores_parses_list() -> None:
    http = FakeHttp([_init(), _initialized(), _tool_text(STORES)])
    result = mcd_order.find_stores("上海", "人民广场", client=http)
    assert result["status"] == "ok"
    stores = result["data"]["stores"]
    assert stores[0]["store_code"] == "1450713"
    assert stores[0]["hours"] == "06:30-23:00"
    assert stores[0]["open"] is True
    # 到店自取：不传 beCode（传了官方会报错）
    call = http.calls[-1]["json"]
    assert call["params"]["name"] == "query-nearby-stores"
    assert "beCode" not in call["params"]["arguments"]


def test_store_menu_parses_items_and_price() -> None:
    http = FakeHttp([_init(), _initialized(), _tool_text(MENU)])
    result = mcd_order.store_menu("1450713", client=http)
    assert result["status"] == "ok"
    items = {item["name"]: item for item in result["data"]["items"]}
    assert items["薄皮焦香V翅"]["product_code"] == "520445"
    assert items["薯条"]["price"] == 13.5
    assert result["data"]["categories"][0]["name"] == "蘸酱炸鸡 小食"


def test_match_items_exact_then_contains() -> None:
    menu_items = [
        {"product_code": "1", "name": "薄皮焦香V翅", "price": 16},
        {"product_code": "2", "name": "麦辣鸡腿汉堡三件套", "price": 33.5},
    ]
    result = mcd_order.match_items(menu_items, ["薄皮焦香V翅", "麦辣鸡腿汉堡", "玉米杯"])
    assert [item["product_code"] for item in result["matched"]] == ["1", "2"]
    assert result["missing"] == ["玉米杯"]


def test_quote_converts_cents_to_yuan() -> None:
    http = FakeHttp([_init(), _initialized(), _tool_text(QUOTE)])
    result = mcd_order.quote("1450713", [{"product_code": "520445"}], client=http)
    assert result["status"] == "ok"
    assert result["data"]["price"] == "29.50"
    assert result["data"]["discount"] == "1.50"
    assert [item["code"] for item in result["data"]["take_way"]] == ["eat-in", "locker-out"]


def test_business_error_is_propagated() -> None:
    failure = {"success": False, "code": 600058, "message": "城市名或者关键词不能为空"}
    http = FakeHttp([_init(), _initialized(), _tool_text(failure)])
    result = mcd_order.find_stores("上海", "人民广场", client=http)
    assert result["status"] == "error"
    assert result["error"] == "mcd_api_error"
    assert result["code"] == 600058


# ---------------------------------------------------------------- 建单 / 取消

def test_create_order_picks_take_way_from_quote_then_creates() -> None:
    http = FakeHttp([_init(), _initialized(), _tool_text(QUOTE),
                     _init(), _initialized(), _tool_text(ORDER)])
    result = mcd_order.create_order("1450713", [{"product_code": "520445"}], client=http)
    assert result["status"] == "ok"
    data = result["data"]
    assert data["order_id"] == "1030722560000808903093418679"
    assert data["pay_url"].startswith("https://m.mcd.cn/mcp/scanToPay")
    assert data["status"] == "待支付"
    assert data["total"] == 2.95
    # 没传取餐方式时：先算价（拿到 takeWayList），再用官方候选建单
    assert http.calls[2]["json"]["params"]["name"] == "calculate-price"
    assert http.calls[5]["json"]["params"]["name"] == "create-order"
    assert http.calls[5]["json"]["params"]["arguments"]["takeWayCode"] == "locker-out"


def test_create_order_uses_given_take_way() -> None:
    http = FakeHttp([_init(), _initialized(), _tool_text(ORDER)])
    mcd_order.create_order("1450713", [{"product_code": "520445"}],
                           take_way_code="eat-in", client=http)
    assert http.calls[-1]["json"]["params"]["arguments"]["takeWayCode"] == "eat-in"


def test_cancel_order_returns_flag() -> None:
    http = FakeHttp([_init(), _initialized(), _tool_text(CANCEL)])
    result = mcd_order.cancel_order("1030722560000808903093418679", client=http)
    assert result["status"] == "ok"
    assert result["data"]["cancelled"] is True


# ---------------------------------------------------------------- 路由

@pytest.fixture()
def auth(client: TestClient) -> dict[str, str]:
    from tests.conftest import register_and_login

    return register_and_login(client)


def test_status_requires_login(client: TestClient) -> None:
    assert client.get("/api/v1/mcd/status").status_code == 401


def test_status_reports_availability(client: TestClient, auth) -> None:
    body = client.get("/api/v1/mcd/status", headers=auth).json()["data"]
    assert "available" in body
    assert "allow_order" in body


def test_create_order_is_blocked_when_disabled(client: TestClient, auth, monkeypatch) -> None:
    monkeypatch.setattr("app.api.mcd_routes.get_settings",
                        lambda: type("S", (), {"mcd_mcp_allow_order": False})())
    response = client.post("/api/v1/mcd/orders", headers=auth,
                           json={"store_code": "1450713", "items": []})
    assert response.status_code == 403


def test_create_order_cooldown(client: TestClient, auth, monkeypatch) -> None:
    """同一用户 60 秒内第二单直接 429：连点两下不该在麦当劳那边留两张待支付单。"""
    monkeypatch.setattr("app.api.mcd_routes.get_settings",
                        lambda: type("S", (), {"mcd_mcp_allow_order": True})())
    fake = {"status": "ok", "data": {
        "order_id": "o-1", "pay_id": "", "pay_url": "https://m.mcd.cn/pay", "status": "待支付",
        "store_name": "测试店", "total": 3.0, "discount": 0, "take_way": "外带",
        "expire_at": "2026-11-05 21:00:00", "items": []}, "citations": []}
    monkeypatch.setattr("app.api.mcd_routes.mcd_order.create_order", lambda *a, **k: fake)
    payload = {"store_code": "1450713",
               "items": [{"product_code": "520445", "quantity": 1}]}

    assert client.post("/api/v1/mcd/orders", headers=auth, json=payload).status_code == 200
    second = client.post("/api/v1/mcd/orders", headers=auth, json=payload)
    assert second.status_code == 429
    assert "秒后再试" in second.json()["detail"]
