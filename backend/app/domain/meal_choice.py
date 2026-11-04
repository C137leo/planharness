"""把 Agent 推荐的某一餐写回计划：新建版本（append-only），只换选中的餐次。

为什么不是直接改 meal_plans：计划是版本化 + 可回滚的，
"我选了这份外卖" 也应该是一条可追溯的版本记录。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.patches import (
    PatchValidationError,
    _current_version,
    _load_plan,
    _new_version_from_actions,
)
from app.models import MealPlan, PlanAction, User


def apply_meal_choices(db: Session, user: User, plan_id: str,
                       selections: list[dict], day: dt.date) -> object:
    """selections: [{slot, title, items, kcal, protein_g, fat_g, carb_g}]"""
    plan = _load_plan(db, user, plan_id)
    current = _current_version(db, plan)
    if not selections:
        raise PatchValidationError(["没有选择任何餐食"])

    weekday = day.isoweekday()
    actions = [
        {
            "day_index": row.day_index, "slot": row.slot, "seq": row.seq,
            "slot_key": row.slot_key, "action_type": row.action_type,
            "action_ref": row.action_ref, "sets": row.sets, "reps": row.reps,
            "duration_min": row.duration_min, "intensity": row.intensity,
            "portion_g": row.portion_g, "meta": row.meta or {},
        }
        for row in db.scalars(
            select(PlanAction).where(PlanAction.plan_version_id == current.id)
        ).all()
    ]

    day_meals = db.scalars(
        select(MealPlan).where(
            MealPlan.plan_version_id == current.id, MealPlan.day_index == weekday
        )
    ).all()
    if not day_meals:
        raise PatchValidationError(["今天没有餐食记录，先生成计划"])

    by_slot = {row.slot: row for row in day_meals}
    chosen = {}
    for item in selections:
        slot = str(item.get("slot") or "")
        if slot not in by_slot:
            raise PatchValidationError([f"没有这一餐：{slot}"])
        chosen[slot] = item

    version, _ = _new_version_from_actions(
        db, plan, current, actions,
        summary_prefix="按推荐替换今日餐食",
        summary_text=(
            f"已按 Agent 推荐替换 {len(chosen)} 餐，记为 v{current.version + 1}"
            f"（训练动作不变）"
        ),
    )

    # _new_version_from_actions 已经把旧版本的餐食复制过来了，
    # 这里只就地覆盖选中的那几餐，不能再插一遍（否则会出现重复餐次）
    copied = db.scalars(
        select(MealPlan).where(
            MealPlan.plan_version_id == version.id, MealPlan.day_index == weekday
        )
    ).all()
    # 历史坏数据里同一天同一餐可能有重复行：先按餐次去重，只保留第一条
    seen_slots: set[str] = set()
    for row in copied:
        if row.slot in seen_slots:
            db.delete(row)
            continue
        seen_slots.add(row.slot)
    db.flush()

    for row in copied:
        if row.slot not in seen_slots:
            continue
        override = chosen.get(row.slot)
        if override is None:
            continue
        row.title = str(override.get("title") or row.title)
        row.items = list(override.get("items") or row.items or [])
        row.kcal = int(override.get("kcal") or row.kcal)
        row.protein_g = float(override.get("protein_g") or row.protein_g)
        row.fat_g = float(override.get("fat_g") or row.fat_g)
        row.carb_g = float(override.get("carb_g") or row.carb_g)

    db.commit()
    db.refresh(version)
    return version
