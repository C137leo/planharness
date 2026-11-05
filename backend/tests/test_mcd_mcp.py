"""麦当劳 MCP 接入：协议容错 + 未配置时降级。"""

from __future__ import annotations

import json

import pytest

from app.food import mcd_mcp


class FakeResponse:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class FakeHttp:
    def __init__(self, response):
        self.response = response

    def post(self, url, json=None, headers=None, timeout=None):
        self.seen = {"url": url, "json": json, "headers": headers}
        return self.response


def test_unavailable_without_config(monkeypatch) -> None:
    monkeypatch.setattr(mcd_mcp, "MCD_MCP_URL", "")
    result = mcd_mcp.search_menu("麦香鱼")
    assert result["status"] == "error"
    assert result["error"] == "mcd_mcp_unavailable"


def test_tool_call_parses_json(monkeypatch) -> None:
    monkeypatch.setattr(mcd_mcp, "MCD_MCP_URL", "https://open.mcd.cn/mcp")
    monkeypatch.setattr(mcd_mcp, "MCD_MCP_TOKEN", "tok")
    body = {"jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text",
                                    "text": json.dumps([{"name": "麦香鱼", "price": 14.5}])}]}}
    http = FakeHttp(FakeResponse(200, json.dumps(body)))
    result = mcd_mcp.search_menu("麦香鱼", client=http)
    assert result["status"] == "ok"
    assert result["data"]["items"][0][0]["price"] == 14.5
    assert http.seen["headers"]["Authorization"] == "Bearer tok"
    assert http.seen["json"]["method"] == "tools/call"


def test_sse_response_is_parsed(monkeypatch) -> None:
    monkeypatch.setattr(mcd_mcp, "MCD_MCP_URL", "https://open.mcd.cn/mcp")
    body = {"result": {"content": [{"text": json.dumps({"name": "麦乐鸡"})}]}}
    http = FakeHttp(FakeResponse(200, f"event: message\ndata: {json.dumps(body)}\n"))
    assert mcd_mcp.search_menu("麦乐鸡", client=http)["status"] == "ok"


def test_http_error_is_retryable(monkeypatch) -> None:
    monkeypatch.setattr(mcd_mcp, "MCD_MCP_URL", "https://open.mcd.cn/mcp")
    http = FakeHttp(FakeResponse(502, "bad gateway"))
    result = mcd_mcp.search_menu("麦香鱼", client=http)
    assert result["status"] == "error"
    assert result["retryable"] is True
