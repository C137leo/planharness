"""Scoped mutation capabilities; addition, replacement and regeneration are distinct."""
from __future__ import annotations
import copy
from app.domain.nutrition_catalog import ingredient_key


def add_meal_item(c, db, user, day, task):
    from app.agent.task_executor import TaskFailure
    slots = task.get("slots") or {}
    meal_slot = slots.get("meal_slot")
    food = slots.get("food") or slots.get("to_food")
    grams = slots.get("grams", 100)
    if meal_slot not in {"breakfast", "lunch", "dinner", "snack"} or not isinstance(food, str) or not food:
        raise TaskFailure("parameters", "请明确要加到哪一餐，以及添加什么食物。")
    if type(grams) not in {int, float} or not 0 < grams <= 500:
        raise TaskFailure("parameters", "请提供大于 0 且不超过 500 克的添加份量。")
    current = c.effective_day(db, user, day, fallback_next=False)
    profile = db.get(c.Profile, user.id)
    forbidden = list(profile.allergies or []) if profile else []
    forbidden += list((task.get("slots") or {}).get("avoid_foods") or [])
    forbidden += list((current.get("constraints") or {}).get("avoid_foods") or [])
    forbidden += list((current.get("constraints", {}).get("meal_constraints", {}).get(meal_slot) or {}).get("avoid_foods") or [])
    if any(term and term in food for term in forbidden):
        raise TaskFailure("parameters", "添加的食物与过敏或当前忌口约束冲突。")
    if c._food_macros(food, grams) is None:
        raise TaskFailure("parameters", "食物库缺少这个食材，请换一个可估算营养的食物。")
    meals = copy.deepcopy(current["meals"])
    selected = [m for m in meals if m["slot"] == meal_slot]
    if len(selected) != 1:
        raise TaskFailure("context_reference", "没有找到唯一的目标餐次，未新增食物。")
    meal = selected[0]
    matches = [i for i in meal["items"] if (i.get("ingredient_key") or ingredient_key(i.get("name"))) == ingredient_key(food)]
    if len(matches) > 1:
        raise TaskFailure("context_reference", "餐单中有多个同名食物，请先确认份量。")
    amount = grams + (float(matches[0].get("grams") or 0) if matches else 0)
    kcal, protein, fat, carb = c._food_macros(food, amount)
    new_item = {"name": food, "ingredient_key": ingredient_key(food), "grams": amount, "kcal": round(kcal, 1), "protein": round(protein, 1),
                "fat": round(fat, 1), "carb": round(carb, 1)}
    if matches:
        matches[0].update(new_item)
    else:
        meal["items"].append(new_item)
    c._recalc_meal(meal)
    c.save_override(db, user, day, actions=current["actions"], meals=meals,
                    constraints=current.get("constraints") or {}, summary=f"{meal_slot}新增{food}{grams}克",
                    reason=task.get("user_goal") or "添加餐食", base_version=current.get("plan_version"), expected_adjustment_id=current.get("adjustment_id"),
                    borrowed_from=current.get("borrowed_from"))
    db.flush()
    db.expire_all()
    reloaded = c.effective_day(db, user, day, fallback_next=False)
    if reloaded["meals"] != meals or reloaded["actions"] != current["actions"]:
        raise TaskFailure("verification", "新增食物未通过数据库重读验证。")
    return {"reply": f"已在{meal_slot}增加{food}{grams}克，其他食物和餐次保持不变。",
            "changes": [f"{meal_slot}增加{food}{grams}克"], "task_status": "success", "day": reloaded}


def regenerate_training(c, db, user, day, task):
    from app.agent.task_executor import TaskFailure
    current = c.effective_day(db, user, day, fallback_next=False)
    if not current["actions"]:
        raise TaskFailure("context_reference", "今天没有可重新安排的训练，请先建立训练计划。")
    profile = db.get(c.Profile, user.id)
    constraints = {**(current.get("constraints") or {}), **(task.get("slots") or {})}
    allowed = c.allowed_equipment(profile, constraints)
    used = {a["action_ref"] for a in current["actions"]}
    actions = copy.deepcopy(current["actions"])
    changes = []
    for action in actions:
        if action.get("slot") == "warmup":
            continue
        replacement = c.pick_alternative(action, allowed=allowed,
                                         injured=constraints.get("injured_regions") or [], used=used)
        if replacement:
            old = action["action_ref"]
            action.update(action_ref=replacement["ref"], name=replacement["name"],
                          meta=replacement.get("meta") or {}, equipment=replacement.get("equipment"),
                          primary_muscle=replacement.get("primary_muscle"),
                          movement_mode=replacement.get("movement_mode"), replaced_from=old)
            used.add(replacement["ref"])
            changes.append(f"{old} → {replacement['name']}")
    if not changes:
        raise TaskFailure("capability", "当前限制下没有可用的新训练组合，原训练保持不变。")
    c.save_override(db, user, day, actions=actions, meals=current["meals"],
                    constraints=current.get("constraints") or {}, summary="重新安排今日训练",
                    reason=task.get("user_goal") or "重新安排训练", base_version=current.get("plan_version"), expected_adjustment_id=current.get("adjustment_id"),
                    borrowed_from=current.get("borrowed_from"))
    db.flush()
    db.expire_all()
    after = c.effective_day(db, user, day, fallback_next=False)
    if after["actions"] != actions or after["meals"] != current["meals"]:
        raise TaskFailure("verification", "训练重排未满足范围或持久化后置条件。")
    return {"reply": "今天的训练已重新安排，饮食保持不变。", "changes": changes,
            "task_status": "success", "day": after}
