"""今日规划工具：只读，不代下单。

外卖只给**平台搜索深链**：淘宝/京东/美团都要求登录，网页端拿不到稳定价格，
所以不做比价、不做抓取；用户点链接自己搜。
手机 ADB 比价（`app/food/phone_compare.py`）保留代码但不再暴露给模型。

设计要点（和 workflow 的区别）：
- 工具不按固定顺序在代码里串起来，模型自己决定调哪个、调几次、什么顺序；
- 工具失败不是 500，而是返回 {status:"error", error, retryable} 给模型看；
- 所有工具统一返回 {status, data, citations}，citation 必须能溯源。
"""

from __future__ import annotations

import datetime as dt
from urllib.parse import urlparse

from app.food.provider import build_options
from app.knowledge.retriever import search as kb_search
from app.knowledge.service import RED_FLAGS
from app.models import MealPlan, Plan, PlanAction, PlanVersion, Profile, User

ALLOWED_LINK_HOSTS = {"search.jd.com", "s.taobao.com", "www.meituan.com"}
MEAL_SLOTS = ("breakfast", "lunch", "dinner", "snack")


def _citation(chunk) -> dict:
    return {
        "id": chunk.id,
        "title": chunk.title,
        "source_url": chunk.source_url,
        "source_title": chunk.source_title,
    }


# ---------------------------------------------------------------- 工具 schema

TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "safety_screen",
        "description": "红旗症状硬拦截。用户提到胸痛、晕厥、严重肿胀、怀孕、进食障碍等先调用。blocked=true 时停止一切规划，只给就医建议。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "用户原话（必须原样传，不要改写）"},
        }, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "get_today_plan",
        "description": "读取指定日期（默认今天）的训练动作与餐食，以及计划版本号。",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD，可省略"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_nutrition_targets",
        "description": "读取当前计划的热量与宏量目标（来源是 skill 计算的营养目标，不要自己估算）。",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "search_knowledge",
        "description": "检索健康知识库（动作/安全/恢复/营养）。给出训练或健康建议前必须先调用并引用来源。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "body_part": {"type": "string", "description": "knee/ankle/wrist/elbow/shoulder/back/core 等，可省略"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "search_food_options",
        "description": "为某一餐生成 3 个替换选项并返回京东/淘宝/美团搜索链接。不要自己编价格或链接。",
        "parameters": {"type": "object", "properties": {
            "meal_slot": {"type": "string", "enum": list(MEAL_SLOTS)},
            "date": {"type": "string", "description": "YYYY-MM-DD，可省略"},
        }, "required": ["meal_slot"]},
    }},
    {"type": "function", "function": {
        "name": "mcd_nutrition",
        "description": "查询麦当劳官方营养库（mcp.mcd.cn MCP），返回可用剩余热量搭配的餐品及其热量/蛋白质。用户提到麦当劳、或你想给一个可直接点餐的备选时调用，数字必须原样引用，不要自己编价格。",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "只看名字里含这个词的餐品，可省略"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_recovery_tips",
        "description": "根据训练时间和用餐时间给出恢复建议（如饭后多久再训练），带知识库引用。",
        "parameters": {"type": "object", "properties": {
            "training_time": {"type": "string"},
            "meal_time": {"type": "string"},
        }},
    }},
]

# 麦当劳 MCP 未配置时不要把工具暴露给模型（否则它会调用一个必然失败的工具）
try:
    from app.food.mcd_mcp import available as _mcd_available
except Exception:  # noqa: BLE001
    _mcd_available = lambda: False  # noqa: E731
if not _mcd_available():
    TOOLS = [tool for tool in TOOLS
             if tool["function"]["name"] != "mcd_nutrition"]

TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}


def _error(code: str, retryable: bool = False) -> dict:
    return {"status": "error", "error": code, "retryable": retryable}


# ---------------------------------------------------------------- 上下文

def load_context(db, user: User, day: dt.date) -> dict:
    """一次性把计划事实读出来（工具共享），避免每次工具调用重复查库。"""
    plan = db.query(Plan).filter(Plan.user_id == user.id, Plan.status == "active").first()
    if plan is None:
        return {"plan": None, "day": day}
    version = db.query(PlanVersion).filter(
        PlanVersion.plan_id == plan.id, PlanVersion.version == plan.current_version
    ).first()
    if version is None:
        return {"plan": None, "day": day}
    weekday = day.isoweekday()
    actions = db.query(PlanAction).filter(
        PlanAction.plan_version_id == version.id, PlanAction.day_index == weekday
    ).all()
    meals = db.query(MealPlan).filter(
        MealPlan.plan_version_id == version.id, MealPlan.day_index == weekday
    ).all()
    profile = db.query(Profile).filter(Profile.user_id == user.id).first()
    return {
        "plan": plan,
        "version": version,
        "day": day,
        "weekday": weekday,
        "actions": actions,
        "meals": {meal.slot: meal for meal in meals},
        "profile": profile,
        "allergies": list(profile.allergies or []) if profile else [],
        "diet_preferences": list(profile.diet_preferences or []) if profile else [],
        "nutrition": plan.nutrition or {},
    }


# ---------------------------------------------------------------- 工具实现

def safety_screen(args: dict, ctx: dict, db) -> dict:
    text = str(args.get("text") or "")
    blocked = [word for word in RED_FLAGS if word in text]
    if not blocked:
        return {"status": "ok", "data": {"blocked": False, "reason": "未发现红旗症状"}, "citations": []}
    hits = kb_search(db, "红旗症状 就医", limit=1, min_score=1.0)
    return {
        "status": "ok",
        "data": {
            "blocked": True,
            "reason": f"提到红旗症状：{'、'.join(blocked)}",
            "action": "停止训练规划，建议尽快就医评估",
        },
        "citations": [_citation(hit.chunk) for hit in hits],
    }


def get_today_plan(args: dict, ctx: dict, db) -> dict:
    if ctx.get("plan") is None:
        return _error("no_active_plan")
    return {
        "status": "ok",
        "data": {
            "date": str(ctx["day"]),
            "version": ctx["version"].version,
            "actions": [
                {
                    "slot": action.slot,
                    "ref": action.action_ref,
                    "name": (action.meta or {}).get("name_zh") or action.action_ref,
                    "sets": action.sets,
                    "duration_min": action.duration_min,
                }
                for action in ctx["actions"]
            ],
            "meals": {
                slot: {
                    "title": meal.title,
                    "kcal": meal.kcal,
                    "protein_g": meal.protein_g,
                    "items": meal.items,
                }
                for slot, meal in ctx["meals"].items()
            },
        },
        "citations": [],
    }


def get_nutrition_targets(args: dict, ctx: dict, db) -> dict:
    if ctx.get("plan") is None:
        return _error("no_active_plan")
    nutrition = ctx.get("nutrition") or {}
    macros = nutrition.get("macros") or {}
    return {
        "status": "ok",
        "data": {
            "target_kcal": ctx["version"].calorie_target,
            "protein_g": ctx["version"].protein_target_g,
            "macros": macros,
            "note": "来自计划版本的营养目标，不要自行估算",
        },
        "citations": [],
    }


def search_knowledge(args: dict, ctx: dict, db) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        return _error("empty_query")
    hits = kb_search(db, query, body_part=args.get("body_part"), limit=3)
    return {
        "status": "ok",
        "data": [
            {
                "id": hit.chunk.id,
                "title": hit.chunk.title,
                "topic": hit.chunk.topic,
                "content": hit.chunk.content,
            }
            for hit in hits
        ],
        "citations": [_citation(hit.chunk) for hit in hits],
    }


def search_food_options(args: dict, ctx: dict, db) -> dict:
    slot = str(args.get("meal_slot") or "")
    meal = (ctx.get("meals") or {}).get(slot)
    if meal is None:
        return _error("meal_slot_not_found")
    options = build_options(
        {"kcal": meal.kcal, "protein_g": meal.protein_g, "slot": slot},
        allergies=ctx.get("allergies") or [],
        diet_preferences=ctx.get("diet_preferences") or [],
    )
    if not options:
        return _error("food_catalog_empty")
    # 保证 3 个选项：不足时把"按原计划这一餐"作为兜底选项补上
    if len(options) < 3 and (meal.items or []):
        from app.food.links import platform_links
        from app.food.provider import FoodOption, title_of

        # 标题用食材拼出来（"按计划：鸡胸肉米饭 + 西兰花"），而不是只写"午餐"，
        # 用户才知道这一项到底吃的是什么
        original = FoodOption(
            title=f"按计划：{title_of(list(meal.items or [])) or str(meal.title or '')}",
            items=list(meal.items or []),
            kcal=int(meal.kcal or 0),
            protein_g=float(meal.protein_g or 0),
            fat_g=float(meal.fat_g or 0),
            carb_g=float(meal.carb_g or 0),
            reason="按当前计划这一餐，不做替换",
        )
        if all(option.title != original.title for option in options):
            options.append(original)
    slim = []
    for option in options:
        payload = option.as_dict()
        # 模型最终只需复制标题/热量/蛋白/理由/链接，items 会让输出变长、更容易被截断
        payload.pop("items", None)
        payload.pop("fat_g", None)
        payload.pop("carb_g", None)
        slim.append(payload)
    return {
        "status": "ok",
        "data": {"meal_slot": slot, "target_kcal": meal.kcal, "options": slim},
        "citations": [],
    }


def _remaining(ctx: dict) -> dict:
    """今天还剩多少热量/蛋白额度：目标来自计划版本，已排进去的是当天 MealPlan 之和。"""
    if ctx.get("plan") is None:
        return {"target_kcal": 0, "plan_kcal": 0, "target_protein": 0.0, "plan_protein": 0.0}
    meals = list((ctx.get("meals") or {}).values())
    return {
        "target_kcal": int(ctx["version"].calorie_target or 0),
        "plan_kcal": int(sum(int(meal.kcal or 0) for meal in meals)),
        "target_protein": float(ctx["version"].protein_target_g or 0),
        "plan_protein": float(sum(float(meal.protein_g or 0) for meal in meals)),
    }


def mcd_nutrition(args: dict, ctx: dict, db) -> dict:
    """麦当劳官方营养库：拿官方热量表算"今天还剩的额度能点哪几样"。

    先拉表（带 6h 缓存），再做加减法；失败只返回错误码，不影响主流程。
    """
    from app.food.mcd_mcp import available, build_block, fetch_nutrition

    token = ctx.get("mcd_token")
    if not available(token):
        return {"status": "error", "error": "mcd_mcp_unavailable", "retryable": False}
    fetched = fetch_nutrition(token=token)
    if fetched["status"] != "ok":
        return fetched
    data = fetched["data"]
    items = data["items"]
    keyword = str(args.get("keyword") or "").strip()
    if keyword:
        narrowed = [item for item in items if keyword in item["name"]]
        items = narrowed or items  # 没匹配到就用全表，别让模型自己补
    block = build_block(
        items,
        target_kcal=_remaining(ctx)["target_kcal"],
        plan_kcal=_remaining(ctx)["plan_kcal"],
        target_protein=_remaining(ctx)["target_protein"],
        plan_protein=_remaining(ctx)["plan_protein"],
        fetched_at=data.get("fetched_at") or "",
    )
    return {"status": "ok", "data": block, "citations": []}


def get_recovery_tips(args: dict, ctx: dict, db) -> dict:
    training = str(args.get("training_time") or "18:30")
    meal_time = str(args.get("meal_time") or "19:00")
    hits = kb_search(db, "饭后 训练 时间 恢复", limit=2, min_score=1.5)
    citations = [_citation(hit.chunk) for hit in hits]
    # 每条建议对应它自己的来源，而不是统一指向第一条
    tips = [
        {"text": f"训练安排在 {training}，餐后建议休息 60-90 分钟再开始力量训练。",
         "source_ids": [citations[0]["id"]] if citations else []},
        {"text": f"如果 {meal_time} 才吃饭，优先把训练放到餐前或餐后 1.5 小时。",
         "source_ids": [citations[1]["id"]] if len(citations) > 1 else (
             [citations[0]["id"]] if citations else [])},
        {"text": "训练后 1-2 小时内补充蛋白质和主食，恢复更稳。",
         "source_ids": [citations[0]["id"]] if citations else []},
    ]
    return {"status": "ok", "data": {"tips": tips}, "citations": citations}


DISPATCH = {
    "safety_screen": safety_screen,
    "get_today_plan": get_today_plan,
    "get_nutrition_targets": get_nutrition_targets,
    "search_knowledge": search_knowledge,
    "search_food_options": search_food_options,
    "get_recovery_tips": get_recovery_tips,
    "mcd_nutrition": mcd_nutrition,
}


def dispatch(name: str, args: dict, ctx: dict, db) -> dict:
    """白名单执行：未知工具 / 异常都变成 observation，而不是 500。"""
    handler = DISPATCH.get(name)
    if handler is None:
        return _error(f"unknown_tool:{name}")
    try:
        return handler(args or {}, ctx, db)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:80]}",
                "retryable": True}


def links_allowed(links: dict) -> bool:
    for url in (links or {}).values():
        host = urlparse(str(url)).hostname or ""
        if host not in ALLOWED_LINK_HOSTS:
            return False
    return True
