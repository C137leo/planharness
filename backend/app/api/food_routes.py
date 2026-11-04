"""订餐推荐接口：3 个变体 + 平台搜索深链。

刻意不做的事：不登录平台、不代下单、不碰支付。
链接只是搜索页，价格/库存/配送都以平台为准。
"""

from __future__ import annotations

from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.routes import get_current_user, get_db, ok
from app.food.provider import build_options, payload
from app.models import FoodOption, MealPlan, Plan, PlanVersion, Profile, User, new_id

router = APIRouter(prefix="/api/v1")


def _owned_plan(db: Session, user: User, plan_id: str) -> Plan:
    plan = db.get(Plan, plan_id)
    if plan is None or plan.user_id != user.id:
        raise HTTPException(404, "plan not found")
    return plan


@router.get("/food/options")
def food_options(
    request: Request,
    plan_id: str,
    date: str,
    meal_slot: str,
    refresh: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """某一天某一餐的 3 个替换推荐（带京东/淘宝/美团搜索深链）。"""
    plan = _owned_plan(db, user, plan_id)
    try:
        plan_date = date_type.fromisoformat(date)
    except ValueError as exc:
        raise HTTPException(422, "date 需要 YYYY-MM-DD") from exc

    version = db.scalar(
        select(PlanVersion).where(
            PlanVersion.plan_id == plan.id, PlanVersion.version == plan.current_version
        )
    )
    if version is None:
        raise HTTPException(404, "plan version not found")

    meal = db.scalar(
        select(MealPlan).where(
            MealPlan.plan_version_id == version.id,
            MealPlan.day_index == plan_date.isoweekday(),
            MealPlan.slot == meal_slot,
        )
    )
    if meal is None:
        raise HTTPException(404, "这一餐没有餐食记录，先生成计划")

    if refresh:
        db.execute(
            delete(FoodOption).where(
                FoodOption.user_id == user.id,
                FoodOption.plan_version_id == version.id,
                FoodOption.plan_date == plan_date,
                FoodOption.meal_slot == meal_slot,
            )
        )
        db.commit()

    cached = db.scalars(
        select(FoodOption).where(
            FoodOption.user_id == user.id,
            FoodOption.plan_version_id == version.id,
            FoodOption.plan_date == plan_date,
            FoodOption.meal_slot == meal_slot,
        )
    ).all()
    if cached:
        return ok(request, {
            "meal_slot": meal_slot,
            "target_kcal": int(meal.kcal),
            "target_protein_g": float(meal.protein_g),
            "options": [
                {
                    "id": row.id,
                    "title": row.title,
                    "items": row.items,
                    "kcal": row.kcal,
                    "protein_g": row.protein_g,
                    "fat_g": row.fat_g,
                    "carb_g": row.carb_g,
                    "reason": row.reason,
                    "links": row.platform_links,
                }
                for row in cached
            ],
            "disclaimer": "价格为平台实时结果，库存与配送范围以平台为准；本页只做搜索，不会代下单。",
        })

    profile = db.get(Profile, user.id)
    options = build_options(
        {"kcal": meal.kcal, "protein_g": meal.protein_g, "slot": meal.slot},
        allergies=list(profile.allergies or []) if profile else [],
        diet_preferences=list(profile.diet_preferences or []) if profile else [],
    )
    if not options:
        raise HTTPException(422, "这一餐没有可用的替换方案（可能忌口太严）")

    saved: list[dict] = []
    for option in options:
        row = FoodOption(
            id=new_id(),
            user_id=user.id,
            plan_id=plan.id,
            plan_version_id=version.id,
            plan_date=plan_date,
            meal_slot=meal_slot,
            title=option.title,
            items=option.items,
            kcal=option.kcal,
            protein_g=option.protein_g,
            fat_g=option.fat_g,
            carb_g=option.carb_g,
            reason=option.reason,
            platform_links=option.as_dict()["links"],
            provider="catalog",
        )
        db.add(row)
        saved.append({
            "id": row.id,
            "title": row.title,
            "items": row.items,
            "kcal": row.kcal,
            "protein_g": row.protein_g,
            "fat_g": row.fat_g,
            "carb_g": row.carb_g,
            "reason": row.reason,
            "links": row.platform_links,
        })
    db.commit()

    data = payload(options, meal_slot, int(meal.kcal), float(meal.protein_g))
    data["options"] = saved
    return ok(request, data)
