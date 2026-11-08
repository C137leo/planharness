"""麦当劳 MCP 接入：握手顺序、营养表解析、预算搭配、失败降级。

不联网：用一个假的 httpx.Client 记录发出去的 JSON-RPC，按顺序返回 initialize →
initialized → tools/call 的响应。
"""

from __future__ import annotations

import json

import pytest

from app.food import mcd_mcp

TOKEN = "tok"
URL = "https://mcp.mcd.cn"


class FakeResponse:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class FakeHttp:
    """按请求顺序返回预设响应，并把每次请求记录下来。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


def _init_ok() -> FakeResponse:
    return FakeResponse(200, json.dumps({"jsonrpc": "2.0", "id": 1,
                                         "result": {"protocolVersion": "2025-06-18"}}))


def _initialized_ok() -> FakeResponse:
    return FakeResponse(202, "")


def _nutrition_body(rows: str) -> str:
    """官方返回：一段说明 + 一个 JSON，Nutrition 表在 data 字符串里。"""
    payload = {
        "success": True,
        "code": 200,
        "data": "[2]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,"
                "carbohydrate,sodium,calcium}:\n" + rows,
    }
    return json.dumps({"jsonrpc": "2.0", "id": 2, "result": {
        "content": [{"type": "text", "text": "说明文字\n" + json.dumps(payload, ensure_ascii=False)}],
        "isError": False,
    }}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """每个用例都从"已配置 + 空缓存"开始，避免互相污染。"""
    monkeypatch.setattr(mcd_mcp, "MCD_MCP_URL", URL)
    monkeypatch.setattr(mcd_mcp, "MCD_MCP_TOKEN", TOKEN)
    monkeypatch.setattr(mcd_mcp, "DEFAULT_TOOL", "")
    monkeypatch.setattr(mcd_mcp, "_CACHE", {"at": 0.0, "items": [], "fetched_at": ""})
    yield


def test_unavailable_without_token(monkeypatch) -> None:
    monkeypatch.setattr(mcd_mcp, "MCD_MCP_TOKEN", "")
    monkeypatch.setattr(mcd_mcp, "MCD_MCP_URL", "")
    monkeypatch.setattr("app.config.get_settings",
                        lambda: type("S", (), {"mcd_mcp_url": "", "mcd_mcp_token": "",
                                               "mcd_mcp_tool": "", "mcd_mcp_timeout": 15.0})())
    assert mcd_mcp.available() is False
    result = mcd_mcp.fetch_nutrition()
    assert result["status"] == "error"
    assert result["error"] == "mcd_mcp_unavailable"


def test_user_token_alias_and_own_token() -> None:
    """joe123456 用服务端 token；其它输入用用户自己的 token；空值回落服务端。"""
    assert mcd_mcp.resolve_token("joe123456") == TOKEN
    assert mcd_mcp.resolve_token("user-token") == "user-token"
    assert mcd_mcp.resolve_token("") == TOKEN
    assert mcd_mcp.resolve_token(None) == TOKEN


def test_call_tool_uses_user_token_header() -> None:
    http = FakeHttp([_init_ok(), _initialized_ok(),
                     FakeResponse(200, json.dumps({"jsonrpc": "2.0", "id": 2,
                                                   "result": {"content": []}}))])
    assert mcd_mcp.call_tool("list-nutrition-foods", {}, client=http,
                             token="user-token")["status"] == "ok"
    assert http.calls[0]["headers"]["Authorization"] == "Bearer user-token"


def test_call_tool_does_handshake_first() -> None:
    """顺序必须是 initialize → notifications/initialized → tools/call。"""
    http = FakeHttp([_init_ok(), _initialized_ok(),
                     FakeResponse(200, json.dumps({"jsonrpc": "2.0", "id": 2,
                                                   "result": {"content": []}}))])
    result = mcd_mcp.call_tool("list-nutrition-foods", {}, client=http)
    assert result["status"] == "ok"
    methods = [call["json"]["method"] for call in http.calls]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    assert http.calls[0]["json"]["params"]["protocolVersion"] == "2025-06-18"
    assert http.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert http.calls[0]["url"] == URL


def test_fetch_nutrition_parses_table_and_caches() -> None:
    rows = ("  猪柳麦满分,null,1288,308,16,16,24,781,213\n"
            "  中薯条,null,1210,289,4,12,38,165,18\n"
            "  坏行,只有两个字段\n")
    http = FakeHttp([_init_ok(), _initialized_ok(), FakeResponse(200, _nutrition_body(rows))])
    first = mcd_mcp.fetch_nutrition(client=http)
    assert first["status"] == "ok"
    items = first["data"]["items"]
    assert [item["name"] for item in items] == ["猪柳麦满分", "中薯条"]
    assert items[0]["kcal"] == 308 and items[0]["protein_g"] == 16.0
    assert first["data"]["fetched_at"]

    # 第二次命中缓存，不再发请求
    second = mcd_mcp.fetch_nutrition(client=FakeHttp([]))
    assert second["data"]["items"] == items


def test_sse_response_is_parsed() -> None:
    sse = "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": 2,
                                                 "result": {"content": []}}) + "\n"
    http = FakeHttp([_init_ok(), _initialized_ok(), FakeResponse(200, sse)])
    assert mcd_mcp.call_tool("list-nutrition-foods", {}, client=http)["status"] == "ok"


def test_unauthorized_is_not_retryable() -> None:
    http = FakeHttp([FakeResponse(401, "{}")])
    result = mcd_mcp.call_tool("list-nutrition-foods", {}, client=http)
    assert result["status"] == "error"
    assert result["error"] == "mcd_unauthorized"
    assert result["retryable"] is False


def test_http_error_is_retryable() -> None:
    http = FakeHttp([FakeResponse(502, "bad gateway")])
    result = mcd_mcp.call_tool("list-nutrition-foods", {}, client=http)
    assert result["status"] == "error"
    assert result["retryable"] is True


def test_build_block_fits_combo_into_budget() -> None:
    items = [
        {"name": "大薯条", "kcal": 379, "protein_g": 6.0, "fat_g": 16.0, "carb_g": 50.0},
        {"name": "板烧鸡腿堡", "kcal": 500, "protein_g": 26.0, "fat_g": 22.0, "carb_g": 44.0},
        {"name": "麦乐鸡5块", "kcal": 213, "protein_g": 12.0, "fat_g": 12.0, "carb_g": 13.0},
        {"name": "玉米杯", "kcal": 87, "protein_g": 4.0, "fat_g": 1.0, "carb_g": 12.0},
        {"name": "美式咖啡", "kcal": 10, "protein_g": 1.0, "fat_g": 0.0, "carb_g": 1.0},
    ]
    block = mcd_mcp.build_block(items, target_kcal=2360, plan_kcal=1900,
                                target_protein=176, plan_protein=140, fetched_at="2026-11-05 20:00")
    assert block["status"] == "ok"
    assert block["budget"]["remaining_kcal"] == 460
    combo = block["combo"]
    # 主食按"蛋白质密度"选（麦乐鸡 5.6 g/100kcal > 板烧 5.2），配菜只从 50 kcal 以上里挑
    assert combo["items"][0]["name"] == "麦乐鸡5块"
    # 严格不超预算：宁可少吃一份，也不给出"还剩 -13 kcal"的搭配
    assert combo["kcal"] <= 460
    assert combo["left_kcal"] >= 0
    assert "美式咖啡" not in [item["name"] for item in combo["items"]]
    assert len(block["options"]) == 3
    assert block["options"][0]["why"]
    # 有可跳转的官方订餐入口（用户自己登录/选门店/付款，我们不代下单）
    assert block["links"]["order"].startswith("https://")
    assert "登录" in block["order_hint"]


def test_build_block_without_budget_still_ranks() -> None:
    items = [{"name": "板烧鸡腿堡", "kcal": 500, "protein_g": 26.0, "fat_g": 22.0, "carb_g": 44.0}]
    block = mcd_mcp.build_block(items, target_kcal=2000, plan_kcal=2000)
    assert block["budget"]["remaining_kcal"] == 0
    assert block["combo"] is None  # 额度用完就不推荐"再来一份"
    assert block["options"]


def test_build_block_rejects_empty_menu() -> None:
    block = mcd_mcp.build_block([], target_kcal=2000, plan_kcal=1000)
    # 空表不该炸，但也不该给出编造的推荐
    assert block["combo"] is None
    assert block["options"] == []
