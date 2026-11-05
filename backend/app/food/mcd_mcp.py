"""麦当劳 MCP（open.mcd.cn）接入：菜单/价格查询，作为登录墙比价的替代演示。

协议按 MCP 的 JSON-RPC over HTTP 处理（streamable HTTP）：
    POST {MCD_MCP_URL}  {"jsonrpc":"2.0","id":1,"method":"tools/call",
                         "params":{"name":"...","arguments":{...}}}
也兼容 SSE 响应（`data: {...}` 行）。

没有配置 MCD_MCP_URL / MCD_MCP_TOKEN 时直接返回 unavailable——不注册给模型，
避免它反复调用一个不可用的工具。
"""

from __future__ import annotations

import json
import os

import httpx

# 模块级常量优先（单元测试会直接改它）；否则读 Settings（backend/.env），
# 最后才退回环境变量，保证"配置文件"和"export"两种方式都能用。
MCD_MCP_URL = ""
MCD_MCP_TOKEN = ""
DEFAULT_TOOL = ""
TIMEOUT = 0.0


def _config() -> tuple[str, str, str, float]:
    from app.config import get_settings

    settings = get_settings()
    url = MCD_MCP_URL or settings.mcd_mcp_url or os.environ.get("MCD_MCP_URL", "")
    token = MCD_MCP_TOKEN or settings.mcd_mcp_token or os.environ.get("MCD_MCP_TOKEN", "")
    tool = DEFAULT_TOOL or settings.mcd_mcp_tool or os.environ.get("MCD_MCP_TOOL", "search_menu")
    timeout = TIMEOUT or settings.mcd_mcp_timeout or float(os.environ.get("MCD_MCP_TIMEOUT", "15"))
    return url, token, tool, float(timeout)


def available() -> bool:
    return bool(_config()[0])


def _parse_response(text: str) -> dict:
    """兼容两种返回：纯 JSON，或 SSE 的 data: 行。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("mcp response is not json")


def call_tool(name: str, arguments: dict, *, client: httpx.Client | None = None) -> dict:
    url, token, _tool, timeout = _config()
    if not url:
        return {"status": "error", "error": "mcd_mcp_unavailable", "retryable": False}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": name, "arguments": arguments}}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        http = client or httpx.Client(timeout=timeout, trust_env=False)
        response = http.post(url, json=payload, headers=headers, timeout=timeout)
        if response.status_code != 200:
            return {"status": "error", "error": f"mcd_http_{response.status_code}",
                    "retryable": True}
        data = _parse_response(response.text)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"mcd_error:{type(exc).__name__}",
                "retryable": True}
    if data.get("error"):
        return {"status": "error", "error": "mcd_tool_error",
                "detail": str(data["error"])[:200], "retryable": False}
    content = (data.get("result") or {}).get("content") or []
    items: list = []
    for part in content:
        text = part.get("text") if isinstance(part, dict) else None
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            items.append({"text": text[:200]})
            continue
        items.append(parsed)
    return {"status": "ok", "data": {"items": items, "raw": data.get("result")},
            "citations": []}


def search_menu(keyword: str = "", store: str = "", *, client=None) -> dict:
    """查麦当劳菜单/价格。字段名按 MCP 工具参数拼，失败也不抛。"""
    args = {}
    if keyword:
        args["keyword"] = keyword
    if store:
        args["store"] = store
    _url, _token, tool, _timeout = _config()
    return call_tool(tool, args, client=client)
