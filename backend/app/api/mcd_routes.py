"""麦当劳点餐接口：找门店 → 菜单 → 算价 → 建单（停在付款前）。

和 `food_routes.py` 的分工：
- `food_routes` 是"给你链接，你自己去京东/淘宝搜"；
- 这里是"用官方 MCP 真的把订单建出来，但把付款留给你"。

安全边界（代码里也是这么写的）：
- 只做**到店自取**，不需要地址；外送要会员地址簿，我们不碰；
- 建单前用审计链做冷却检查：同一个用户 60 秒内只能建一单，防止连点/刷单；
- 每建一单都会往审计链写 `mcd_order_created`，Console 里能看到"Agent 帮你下过什么"；
- 不保存支付信息、不代付；`pay_url` 是官方 H5 收银台，用户自己决定付不付。
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes import get_current_user, get_db, ok
from app.audit import append_event
from app.config import get_settings
from app.food import mcd_order
from app.models import AuditEvent, Plan, User

router = APIRouter(prefix="/api/v1")

# 建单冷却：一次点单是"用户明确动作"，但连点两下会很吓人，也浪费一张待支付单
ORDER_COOLDOWN_S = 60
ERROR_STATUS = {
    "mcd_mcp_unavailable": 503,
    "mcd_unauthorized": 503,
    "mcd_need_city_and_keyword": 422,
    "mcd_need_store": 422,
    "mcd_need_items": 422,
    "mcd_need_order": 422,
    "mcd_no_store": 404,
    "mcd_rate_limited": 429,
}


class QuoteIn(BaseModel):
    store_code: str = Field(min_length=1, max_length=32)
    items: list[dict] = Field(default_factory=list)


class OrderIn(QuoteIn):
    take_way_code: str = Field(default="", max_length=32)


def _fail(result: dict) -> None:
    """把 MCP 的错误信封翻成 HTTP 错误，前端只看到一句人话 + 错误码。"""
    code = result.get("error") or "mcd_error"
    detail = result.get("detail") or {
        "mcd_mcp_unavailable": "还没配置 MCD_MCP_TOKEN",
        "mcd_unauthorized": "MCD Token 无效或已过期",
        "mcd_need_city_and_keyword": "官方要求城市和关键词都填（例如 上海 / 人民广场）",
        "mcd_no_store": "没搜到门店，换个关键词试试",
        "mcd_rate_limited": "调用太频繁，等一分钟再来",
    }.get(code, code)
    raise HTTPException(ERROR_STATUS.get(code, 502), f"{detail}")


def _guard():
    settings = get_settings()
    if not settings.mcd_mcp_allow_order:
        raise HTTPException(403, "写操作被配置关闭（MCD_MCP_ALLOW_ORDER=0）")


@router.get("/mcd/status")
def mcd_status(request: Request, user: User = Depends(get_current_user)):
    """前端用它决定要不要显示"帮你点好"的入口。"""
    settings = get_settings()
    from app.food.mcd_mcp import available

    return ok(request, {
        "available": available(),
        "allow_order": bool(settings.mcd_mcp_allow_order),
    })


@router.get("/mcd/locate")
def mcd_locate(request: Request, user: User = Depends(get_current_user)):
    """按 IP 猜一个默认城市：前端打开点餐抽屉时先填好，用户仍可自己改。"""
    from app.food import geo

    return ok(request, geo.locate_from_request(request))


@router.get("/mcd/stores")
def mcd_stores(
    request: Request,
    city: str,
    keyword: str,
    user: User = Depends(get_current_user),
):
    _guard()
    result = mcd_order.find_stores(city, keyword)
    if result["status"] != "ok":
        _fail(result)
    return ok(request, result["data"])


@router.get("/mcd/menu")
def mcd_menu(
    request: Request,
    store_code: str,
    names: str = "",
    user: User = Depends(get_current_user),
):
    """这家店的菜单；带 `names` 时顺便把推荐餐名对到真实商品上。"""
    _guard()
    result = mcd_order.store_menu(store_code)
    if result["status"] != "ok":
        _fail(result)
    data = result["data"]
    wanted = [name.strip() for name in names.split("|") if name.strip()]
    matched = mcd_order.match_items(data["items"], wanted) if wanted else {
        "matched": [], "missing": []}
    return ok(request, {**data, **matched})


@router.post("/mcd/quote")
def mcd_quote(
    request: Request,
    body: QuoteIn,
    user: User = Depends(get_current_user),
):
    _guard()
    items = [{"product_code": item.get("product_code"),
              "quantity": item.get("quantity", 1)} for item in body.items]
    result = mcd_order.quote(body.store_code, items)
    if result["status"] != "ok":
        _fail(result)
    return ok(request, result["data"])


@router.post("/mcd/orders")
def mcd_create_order(
    request: Request,
    body: OrderIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """建一张待支付订单，返回官方支付链接。付款由用户自己在麦当劳页面完成。"""
    _guard()
    _check_cooldown(db, user)
    items = [{"product_code": item.get("product_code"),
              "quantity": item.get("quantity", 1)} for item in body.items]
    result = mcd_order.create_order(body.store_code, items, body.take_way_code)
    if result["status"] != "ok":
        _fail(result)
    data = result["data"]

    # 挂在当前计划的审计流里：打开 Console 就能看到"这一版计划被点过几单"；
    # 还没有计划的新用户就挂在自己身上，别让这条记录凭空消失（冷却也要靠它）
    append_event(
        db, _audit_stream(db, user), "mcd_order_created",
        {"order_id": data["order_id"], "total": data["total"],
         "store_name": data["store_name"], "status": data["status"]},
        actor_type="user", actor_id=user.id,
    )
    db.commit()
    return ok(request, data)


@router.get("/mcd/orders/{order_id}")
def mcd_get_order(
    request: Request,
    order_id: str,
    user: User = Depends(get_current_user),
):
    _guard()
    result = mcd_order.get_order(order_id)
    if result["status"] != "ok":
        _fail(result)
    return ok(request, result["data"])


@router.post("/mcd/orders/{order_id}/cancel")
def mcd_cancel_order(
    request: Request,
    order_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _guard()
    result = mcd_order.cancel_order(order_id)
    if result["status"] != "ok":
        _fail(result)
    if result["data"]["cancelled"]:
        append_event(db, _audit_stream(db, user), "mcd_order_cancelled",
                     {"order_id": order_id}, actor_type="user", actor_id=user.id)
        db.commit()
    return ok(request, result["data"])


def _audit_stream(db: Session, user: User) -> str:
    """审计流：有生效计划就挂计划上，没有就挂用户自己（36 位 uuid，长度够）。"""
    plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    return plan.id if plan is not None else user.id


def _check_cooldown(db: Session, user: User) -> None:
    """同一用户 60 秒内只允许建一单：连点两下会生成两张待支付单，纯属浪费。"""
    last = db.scalar(
        select(AuditEvent)
        .where(AuditEvent.actor_id == user.id, AuditEvent.action == "mcd_order_created")
        .order_by(AuditEvent.created_at.desc())
        .limit(1)
    )
    if last is None or last.created_at is None:
        return
    created = last.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    # 取消过就不用等：冷却防的是"连点生成两张待支付单"，不是惩罚用户改主意
    cancelled = db.scalar(
        select(AuditEvent)
        .where(AuditEvent.actor_id == user.id,
               AuditEvent.action == "mcd_order_cancelled",
               AuditEvent.created_at >= created)
        .limit(1)
    )
    if cancelled is not None:
        return
    elapsed = (dt.datetime.now(dt.timezone.utc) - created).total_seconds()
    if elapsed < ORDER_COOLDOWN_S:
        raise HTTPException(
            429, f"刚刚已经建过一单了，{int(ORDER_COOLDOWN_S - elapsed)} 秒后再试")
