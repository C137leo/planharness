"""麦当劳 MCP（mcp.mcd.cn）接入：把官方营养库接进规划结果。

为什么是营养库而不是"菜单价格"：
- 麦当劳 MCP 的点餐类工具（query-nearby-stores → query-meals → calculate-price →
  create-order）要先选门店、选就餐方式，属于"代下单"链路；这个项目只做推荐、
  不做代下单，所以只取 `list-nutrition-foods`（无需登录/门店/地址）。
- 好处是每个数字都能对上官方营养表：热量、蛋白质、脂肪、碳水、钠、钙，
  可以拿来算"今天还剩多少额度、能点哪几样"，比价反而是在编数据。

协议要点（官方 guide：https://open.mcd.cn/mcp/guide.md）：
1. 接入地址就是 `https://mcp.mcd.cn` 根路径，加 `/mcp`、`/sse` 会 404；
2. Streamable HTTP：先 `initialize`，再发 `notifications/initialized`，之后才能 `tools/call`；
3. 请求头带 `Authorization: Bearer <MCP_TOKEN>`；
4. 响应可能是纯 JSON，也可能是 SSE（`data: {...}` 行），两种都要能吃。

没配 token 时 `available()` 返回 False：工具不会暴露给模型，规划流程照常，
避免模型去调一个必然失败的接口。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time

import httpx

# 模块级常量优先（单元测试直接改它）；否则读 Settings（backend/.env），
# 最后退回环境变量，保证「配置文件」和「export」两种部署方式都能用。
MCD_MCP_URL = ""
MCD_MCP_TOKEN = ""
DEFAULT_TOOL = ""
TIMEOUT = 0.0

# 用户输入这个口令时，后端使用服务端配置的 MCD_MCP_TOKEN；
# 其它输入一律当作用户自己的 token，不落库、不写日志。
DEMO_TOKEN_ALIAS = "joe123456"

# 营养表几乎不变：缓存一段时间，省掉每个用户每次规划都打一次官方接口
CACHE_TTL_S = 6 * 3600
_CACHE: dict = {"at": 0.0, "items": [], "fetched_at": "", "key": ""}

NUTRITION_TOOL = "list-nutrition-foods"

# 官方网上订餐入口（"麦当劳网上订餐"，需用户自己用手机号登录 → 选门店 → 加购物车）。
# 我们不代下单、不收款：这里只给一个可跳转的入口，付款在麦当劳页面完成。
ORDER_URL = "https://www.mcd.cn/"
ORDER_HINT = "自己登录 → 选门店/地址 → 搜餐名加购物车 → 想付款再付"

NOTE = ("热量/蛋白质为麦当劳官方营养表数值，不是价格，也不代表门店当前供应；"
        "这里只做推荐，不代下单。")


def _configured_token() -> str:
    from app.config import get_settings

    settings = get_settings()
    return MCD_MCP_TOKEN or settings.mcd_mcp_token or os.environ.get("MCD_MCP_TOKEN", "")


def resolve_token(user_token: str | None = None) -> str:
    """解析本次请求实际使用的 token。

    - 空：用服务端配置（兼容原来的行为）；
    - `joe123456`：用服务端配置，方便演示时不用把真 token 发给用户；
    - 其它：用用户自己输入的 token。
    """
    supplied = (user_token or "").strip()[:512]
    configured = _configured_token().strip()
    if supplied == DEMO_TOKEN_ALIAS:
        return configured
    return supplied or configured


def _config(user_token: str | None = None) -> tuple[str, str, str, float]:
    from app.config import get_settings

    settings = get_settings()
    url = MCD_MCP_URL or settings.mcd_mcp_url or os.environ.get("MCD_MCP_URL", "")
    token = resolve_token(user_token)
    tool = DEFAULT_TOOL or settings.mcd_mcp_tool or os.environ.get(
        "MCD_MCP_TOOL", NUTRITION_TOOL)
    timeout = TIMEOUT or settings.mcd_mcp_timeout or float(os.environ.get("MCD_MCP_TIMEOUT", "15"))
    return url, token, tool, float(timeout)


def available(user_token: str | None = None) -> bool:
    """地址和 token 都有才算可用：只有地址会 403，等于每次规划都白等超时。"""
    url, token, _tool, _timeout = _config(user_token)
    return bool(url and token)


def _messages(text: str) -> list[dict]:
    """把响应体拆成 JSON-RPC 消息列表（兼容纯 JSON、多行 JSON、SSE）。"""
    out: list[dict] = []
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
            out.append(data)
    if not out and (text or "").strip().startswith("{"):
        # 整个 body 就是一条 JSON，但里面有换行
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                out.append(data)
        except json.JSONDecodeError:
            pass
    return out


def _rpc(client: httpx.Client, url: str, headers: dict, payload: dict) -> list[dict]:
    """发一条 JSON-RPC；HTTP 层错误转成异常，由调用方映射成错误码。"""
    response = client.post(url, json=payload, headers=headers, timeout=_config()[3])
    if response.status_code == 401:
        raise PermissionError("mcd_unauthorized")
    if response.status_code == 429:
        raise RuntimeError("mcd_rate_limited")
    if response.status_code not in (200, 202, 204):
        raise RuntimeError(f"mcd_http_{response.status_code}")
    return _messages(response.text)


def call_tool(name: str, arguments: dict, *, client: httpx.Client | None = None,
              token: str | None = None) -> dict:
    """完整走一遍握手再调工具，返回 {status, data, citations} 信封。"""
    url, resolved_token, _tool, timeout = _config(token)
    if not url or not resolved_token:
        return {"status": "error", "error": "mcd_mcp_unavailable", "retryable": False}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {resolved_token}",
    }
    own = client is None
    http = client or httpx.Client(timeout=timeout, trust_env=False)
    try:
        # 1) initialize：声明协议版本，服务端回 serverInfo
        init = _rpc(http, url, headers, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",  # 官方支持 2025-06-18 及更早
                "capabilities": {},
                "clientInfo": {"name": "planharness", "version": "1.0"},
            },
        })
        if init and init[0].get("error"):
            return {"status": "error", "error": "mcd_initialize_failed",
                    "detail": str(init[0]["error"])[:200], "retryable": True}
        # 2) 通知服务端"初始化完成"（无 id，不期待返回）
        _rpc(http, url, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        # 3) 调工具
        messages = _rpc(http, url, headers, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
    except PermissionError:
        return {"status": "error", "error": "mcd_unauthorized", "retryable": False}
    except Exception as exc:  # noqa: BLE001 - 网络/超时/限流都归一类，不炸主流程
        code = "mcd_rate_limited" if "429" in str(exc) else f"mcd_error:{type(exc).__name__}"
        return {"status": "error", "error": code, "retryable": True}
    finally:
        if own:
            http.close()

    if not messages:
        return {"status": "error", "error": "mcd_empty_response", "retryable": True}
    data = messages[-1]
    if data.get("error"):
        return {"status": "error", "error": "mcd_tool_error",
                "detail": str(data["error"])[:200], "retryable": False}
    result = data.get("result") or {}
    texts = [part.get("text") or "" for part in result.get("content") or []
             if isinstance(part, dict)]
    return {"status": "ok",
            "data": {"text": "\n".join(texts), "is_error": bool(result.get("isError"))},
            "citations": []}


def extract_json(text: str) -> dict | None:
    """从官方"说明 + JSON + 备注"的混合文本里抽出第一个完整 JSON 对象。

    麦当劳的返回是「字段说明 + Original Response + ## 展示要求」拼起来的，
    不是纯 JSON；用括号配平来定位，避免正则被嵌套对象坑到。
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:index + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(data, dict):
                        return data
                    break
        start = text.find("{", start + 1)
    return None


def call_tool_json(name: str, arguments: dict, *, client=None,
                   token: str | None = None) -> dict:
    """调工具并把返回体解析成 JSON；业务失败（success=false）也当错误返回。"""
    result = call_tool(name, arguments, client=client, token=token)
    if result["status"] != "ok":
        return result
    payload = extract_json(result["data"].get("text") or "")
    if payload is None:
        return {"status": "error", "error": "mcd_bad_json", "retryable": True}
    if payload.get("success") is False:
        return {"status": "error", "error": "mcd_api_error",
                "detail": str(payload.get("message") or "")[:200],
                "code": payload.get("code"), "retryable": False}
    return {"status": "ok", "data": payload, "citations": []}


# ---------------------------------------------------------------- 营养表

def _parse_nutrition(text: str) -> list[dict]:
    """官方返回是一段带说明的文本，正文在 JSON 的 data 字段里，形如：

        [160]{productName,nutritionDescription,energyKj,energyKcal,protein,...}:
          猪柳麦满分,null,1288,308,16,16,24,781,213

    行内是逗号分隔的固定列，这里按列位解析；解析不出来的行直接跳过，不编数据。
    """
    payload = None
    for message in _messages(text):
        if isinstance(message.get("data"), str) and message["data"].strip().startswith("["):
            payload = message["data"]
    if payload is None:
        return []
    items: list[dict] = []
    for line in payload.splitlines():
        line = line.strip()
        if not line or line.startswith("["):
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 9:
            continue
        name = parts[0]
        try:
            kcal = int(float(parts[3]))
            protein = float(parts[4])
            fat = float(parts[5])
            carb = float(parts[6])
        except ValueError:
            continue
        if not name or kcal <= 0:
            continue
        items.append({"name": name, "kcal": kcal, "protein_g": round(protein, 1),
                      "fat_g": round(fat, 1), "carb_g": round(carb, 1)})
    return items


def fetch_nutrition(*, client: httpx.Client | None = None, force: bool = False,
                    token: str | None = None) -> dict:
    """拉官方营养库（带缓存）。失败返回 error 信封，不抛异常。"""
    _url, resolved_token, tool, _timeout = _config(token)
    cache_key = hashlib.sha256(resolved_token.encode("utf-8")).hexdigest()[:16]
    if (not force and _CACHE["items"] and _CACHE.get("key") == cache_key
            and time.monotonic() - _CACHE["at"] < CACHE_TTL_S):
        return {"status": "ok",
                "data": {"items": _CACHE["items"], "fetched_at": _CACHE["fetched_at"]},
                "citations": []}
    tool = tool or NUTRITION_TOOL
    result = call_tool(tool, {}, client=client, token=token)
    if result["status"] != "ok":
        return result
    items = _parse_nutrition(result["data"]["text"])
    if not items:
        return {"status": "error", "error": "mcd_empty_menu", "retryable": True}
    fetched_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    _CACHE.update({"at": time.monotonic(), "items": items, "fetched_at": fetched_at,
                   "key": cache_key})
    return {"status": "ok", "data": {"items": items, "fetched_at": fetched_at}, "citations": []}


def _density(item: dict) -> float:
    """蛋白质密度：每 100 kcal 有多少克蛋白，用来排"减脂期该点哪个"。"""
    return item["protein_g"] / max(item["kcal"], 1) * 100


def _why(item: dict, budget_kcal: int) -> str:
    if budget_kcal <= 0:
        return f"蛋白质密度 {round(_density(item), 1)} g/100kcal"
    if item["kcal"] <= budget_kcal * 0.5:
        return f"热量低（{item['kcal']} kcal），还能再配一份"
    if item["kcal"] <= budget_kcal * 1.05:
        return f"正好塞进剩余 {budget_kcal} kcal"
    return f"比剩余额度高 {item['kcal'] - budget_kcal} kcal，需替换一餐"


def build_block(items: list[dict], *, target_kcal: int = 0, plan_kcal: int = 0,
                target_protein: float = 0.0, plan_protein: float = 0.0,
                fetched_at: str = "", limit: int = 3) -> dict:
    """把官方营养表变成"今天还能怎么点"：一个搭配 + 几个单点备选。

    只做加减法，不预测口味/供应；数字全部来自官方表。
    """
    remaining_kcal = max(0, int(target_kcal - plan_kcal))
    remaining_protein = round(max(0.0, target_protein - plan_protein), 1)
    mains = [item for item in items if item["kcal"] >= 180]
    # 配菜：50-180 kcal 且带点蛋白质，滤掉纯饮料/甜点（加一杯美式不解决"还想吃点东西"）
    sides = [item for item in items if 50 <= item["kcal"] < 180 and item["protein_g"] >= 3]
    mains.sort(key=_density, reverse=True)
    sides.sort(key=_density, reverse=True)

    combo: dict | None = None
    if mains and remaining_kcal >= mains[-1]["kcal"]:
        # 主食用"蛋白质密度最高且装得下"的那一个，再拿低热量配菜把额度填满
        main = next((item for item in mains if item["kcal"] <= remaining_kcal), None)
        if main is None:
            main = min(mains, key=lambda item: item["kcal"])
        picked = [main]
        total = main["kcal"]
        for side in sides:
            if len(picked) >= 3:
                break
            # 严格不超预算：宁可少吃一份，也不要"还剩 -13 kcal"
            if total + side["kcal"] <= remaining_kcal:
                picked.append(side)
                total += side["kcal"]
        combo = {
            "items": picked,
            "kcal": total,
            "protein_g": round(sum(item["protein_g"] for item in picked), 1),
            "left_kcal": remaining_kcal - total,
        }

    # 单点备选：优先蛋白密度高、且不超预算的
    ranked = sorted(mains or sides, key=_density, reverse=True)
    options = [
        {"name": item["name"], "kcal": item["kcal"], "protein_g": item["protein_g"],
         "why": _why(item, remaining_kcal)}
        for item in ranked[:limit]
    ]
    return {
        "status": "ok",
        "source": "麦当劳官方营养库（mcp.mcd.cn）",
        "fetched_at": fetched_at,
        "budget": {
            "target_kcal": target_kcal,
            "plan_kcal": plan_kcal,
            "remaining_kcal": remaining_kcal,
            "remaining_protein_g": remaining_protein,
        },
        "combo": combo,
        "options": options,
        # 可跳转的官方订餐入口：用户自己登录、选门店、加购物车、自己付款
        "links": {"order": ORDER_URL},
        "order_hint": ORDER_HINT,
        "note": NOTE,
    }
