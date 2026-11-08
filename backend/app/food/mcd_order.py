"""麦当劳点餐链路：找门店 → 看菜单 → 算价 → 建单（停在付款前）。

为什么能做到"停在付款前"：
官方 MCP 的 `create-order` 会真的生成一张**待支付**订单，返回
`payH5Url`（`https://m.mcd.cn/mcp/scanToPay?orderId=...`）。
用户自己决定要不要付；不付就作废，我们也能 `cancel-order` 撤掉。

边界（刻意不做的）：
- 不代付、不碰支付；只把官方支付链接交回给用户；
- 不做"加购物车"：麦当劳网页的购物车是浏览器会话，MCP 是另一个通道，
  两者不互通，所以只能在 MCP 侧直接建单（这也是为什么你在网页看到的购物车是空的）；
- 只用**到店自取**（orderType=1 / beType=1）：外送要会员地址簿里的 addressId，
  我们没有用户麦当劳账号的地址，不乱猜。

所有函数都返回 `{status, data}` 信封，失败也只给错误码，不抛异常。
"""

from __future__ import annotations

from app.food.mcd_mcp import call_tool_json

# 到店自取：orderType=1，beType=1。到店场景传 beCode 会报错，所以一律不传。
ORDER_TYPE = 1
BE_TYPE = 1

# 取餐方式偏好：外带优先，其次堂食，最后用官方返回的第一个
TAKE_WAY_PREFERENCE = ("locker-out", "eat-in")


def _money(value) -> float:
    """菜单里价格是"元"的字符串（"34.5"），算价接口返回的是"分"的整数。"""
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _cents_text(cents) -> str:
    try:
        return f"{int(cents) / 100:.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _items_payload(items: list[dict]) -> list[dict]:
    """把前端传来的 [{product_code, quantity}] 转成官方要求的 items 结构。"""
    payload = []
    for item in items or []:
        code = str(item.get("product_code") or "").strip()
        if not code:
            continue
        try:
            quantity = max(1, int(item.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
        payload.append({"productCode": code, "quantity": quantity})
    return payload


# ---------------------------------------------------------------- 门店 / 菜单

def find_stores(city: str, keyword: str, *, client=None,
                token: str | None = None) -> dict:
    """按城市 + 关键词搜门店（到店自取）。官方这两个参数都必填。"""
    city = (city or "").strip()
    keyword = (keyword or "").strip()
    if not city or not keyword:
        return {"status": "error", "error": "mcd_need_city_and_keyword", "retryable": False}
    result = call_tool_json("query-nearby-stores", {
        "beType": BE_TYPE, "searchType": 2, "city": city, "keyword": keyword,
    }, client=client, token=token)
    if result["status"] != "ok":
        return result
    rows = (result["data"].get("data") or [])
    stores = [
        {
            "store_code": str(row.get("storeCode") or ""),
            "name": row.get("storeName") or "",
            "address": row.get("address") or "",
            "distance_m": row.get("distance"),
            "open": bool(row.get("businessStatus")),
            "hours": f"{row.get('businessStartTime') or '—'}-{row.get('businessEndTime') or '—'}",
        }
        for row in rows if isinstance(row, dict) and row.get("storeCode")
    ]
    if not stores:
        return {"status": "error", "error": "mcd_no_store", "retryable": False}
    return {"status": "ok", "data": {"stores": stores}, "citations": []}


def store_menu(store_code: str, *, client=None, token: str | None = None) -> dict:
    """当前门店可售卖的餐品（含官方现价，单位元）。"""
    if not store_code:
        return {"status": "error", "error": "mcd_need_store", "retryable": False}
    result = call_tool_json("query-meals", {
        "storeCode": store_code, "orderType": ORDER_TYPE, "beType": BE_TYPE,
    }, client=client, token=token)
    if result["status"] != "ok":
        return result
    data = result["data"].get("data") or {}
    meals = data.get("meals") or {}
    items = [
        {
            "product_code": str(code),
            "name": meal.get("name") or "",
            "price": _money(meal.get("currentPrice")),
            "original_price": _money(meal.get("originalPrice")),
            "product_type": meal.get("productType"),
            "image": meal.get("image") or "",
        }
        for code, meal in meals.items() if isinstance(meal, dict)
    ]
    categories = [
        {"name": str(cat.get("name") or "").replace("\n", " "),
         "codes": [str(x.get("code")) for x in cat.get("meals") or [] if isinstance(x, dict)]}
        for cat in data.get("categories") or [] if isinstance(cat, dict)
    ]
    if not items:
        return {"status": "error", "error": "mcd_empty_menu", "retryable": True}
    return {"status": "ok",
            "data": {"items": items, "categories": categories, "store_code": store_code},
            "citations": []}


def match_items(menu_items: list[dict], names: list[str]) -> dict:
    """把推荐里的餐名对到这家店的真实商品上。

    先精确同名，再双向包含（"薄皮焦香V翅" vs "薄皮焦香V翅(2块)"）。
    对不上的不猜，单独列出来告诉用户"这家店没有"。
    """
    matched: list[dict] = []
    missing: list[str] = []
    used: set[str] = set()
    for name in names or []:
        name = (name or "").strip()
        if not name:
            continue
        hit = next((it for it in menu_items if it["name"] == name), None)
        if hit is None:
            hit = next((it for it in menu_items
                        if it["product_code"] not in used
                        and (name in it["name"] or it["name"] in name)), None)
        if hit is None or hit["product_code"] in used:
            missing.append(name)
            continue
        used.add(hit["product_code"])
        matched.append({"suggestion": name, **hit})
    return {"matched": matched, "missing": missing}


# ---------------------------------------------------------------- 算价 / 建单

def _pick_take_way(options: list[dict]) -> str:
    codes = [str(item.get("code") or "") for item in options if isinstance(item, dict)]
    for preferred in TAKE_WAY_PREFERENCE:
        if preferred in codes:
            return preferred
    return codes[0] if codes else ""


def quote(store_code: str, items: list[dict], *, client=None,
          token: str | None = None) -> dict:
    """算价：返回官方口径的应付金额（分→元）和可选取餐方式。"""
    official = _items_payload(items)
    if not official:
        return {"status": "error", "error": "mcd_need_items", "retryable": False}
    result = call_tool_json("calculate-price", {
        "storeCode": store_code, "orderType": ORDER_TYPE, "beType": BE_TYPE,
        "items": official,
    }, client=client, token=token)
    if result["status"] != "ok":
        return result
    data = result["data"].get("data") or {}
    return {"status": "ok", "data": {
        "price": _cents_text(data.get("price")),
        "original_price": _cents_text(data.get("originalPrice")),
        "discount": _cents_text(data.get("discount")),
        "take_way": [
            {"code": str(t.get("code") or ""), "title": t.get("title") or "",
             "subtitle": t.get("subtitle") or ""}
            for t in data.get("takeWayList") or [] if isinstance(t, dict)
        ],
        "lines": [
            {"name": line.get("productName") or "", "quantity": line.get("quantity"),
             "subtotal": _cents_text(line.get("subtotal"))}
            for line in data.get("productList") or [] if isinstance(line, dict)
        ],
    }, "citations": []}


def create_order(store_code: str, items: list[dict], take_way_code: str = "",
                 *, client=None, token: str | None = None) -> dict:
    """建一张**待支付**订单，返回官方支付链接。用户不付就作废（15 分钟）。"""
    official = _items_payload(items)
    if not official:
        return {"status": "error", "error": "mcd_need_items", "retryable": False}
    if not take_way_code:
        # 取餐方式不是我们该猜的：先算一次价，拿官方给的候选
        priced = quote(store_code, items, client=client, token=token)
        if priced["status"] != "ok":
            return priced
        take_way_code = _pick_take_way(priced["data"]["take_way"])
    result = call_tool_json("create-order", {
        "storeCode": store_code, "orderType": ORDER_TYPE, "beType": BE_TYPE,
        "takeWayCode": take_way_code, "items": official, "needTableware": False,
    }, client=client, token=token)
    if result["status"] != "ok":
        # 600039 = 官方账号级的"今日下单数已经上限"（不是我们的限流，也不是次数统计错误）。
        # 已取消的订单也算一天里的下单数，所以调试/反复试单很容易把它用光。
        if result.get("code") == 600039:
            return {"status": "error", "error": "mcd_daily_order_limit",
                    "detail": str(result.get("detail") or "")[:200], "retryable": False}
        return result
    data = result["data"].get("data") or {}
    detail = data.get("orderDetail") or {}
    if not data.get("orderId"):
        return {"status": "error", "error": "mcd_order_failed",
                "detail": str(result["data"].get("message") or "")[:200],
                "retryable": False}
    return {"status": "ok", "data": {
        "order_id": str(data["orderId"]),
        "pay_id": str(data.get("payId") or ""),
        "pay_url": data.get("payH5Url") or "",
        "status": detail.get("orderStatus") or "待支付",
        "store_name": detail.get("storeName") or "",
        "total": _money(detail.get("totalAmount")),
        "discount": _money(detail.get("totalDiscountAmount")),
        "take_way": detail.get("takeWay") or "",
        "expire_at": detail.get("expirePayTime") or "",
        "items": [
            {"name": row.get("productName") or "", "quantity": row.get("quantity"),
             "price": _money(row.get("price"))}
            for row in detail.get("orderProductList") or [] if isinstance(row, dict)
        ],
    }, "citations": []}


def get_order(order_id: str, *, client=None, token: str | None = None) -> dict:
    if not order_id:
        return {"status": "error", "error": "mcd_need_order", "retryable": False}
    result = call_tool_json("query-order", {"orderId": order_id}, client=client, token=token)
    if result["status"] != "ok":
        return result
    data = result["data"].get("data") or {}
    return {"status": "ok", "data": {
        "order_id": str(data.get("orderId") or order_id),
        "status": data.get("orderStatus") or "",
        "total": _money(data.get("totalAmount")),
        "store_name": data.get("storeName") or "",
        "expire_at": data.get("expirePayTime") or "",
        "take_way": data.get("takeWay") or "",
    }, "citations": []}


def cancel_order(order_id: str, reason_code: str = "3", *, client=None,
                 token: str | None = None) -> dict:
    """取消订单。reason_code：1 改主意、2 重复下单、3 点错了、-1 其它（官方枚举）。"""
    if not order_id:
        return {"status": "error", "error": "mcd_need_order", "retryable": False}
    result = call_tool_json("cancel-order", {
        "orderId": order_id, "cancelReasonCode": str(reason_code or "3"),
    }, client=client, token=token)
    if result["status"] != "ok":
        return result
    data = result["data"].get("data") or {}
    return {"status": "ok",
            "data": {"order_id": order_id, "cancelled": bool(data.get("cancelResult"))},
            "citations": []}
