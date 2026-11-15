"""计划落库前的确定性约束守卫。

评审里最严重的一类问题不是"文案不好"，而是"计划数据违反用户约束"：
家里只有哑铃却排了腿举、档案写 20 分钟却生成 39 分钟、餐食合计与热量目标
差 300 kcal。模型负责提议，这里负责把提议压回用户真实条件内：

- 器械：动作的 equipment 必须在 `allowed_equipment(profile, {})` 里，
  不允许就换同肌群/同模式的替代动作，找不到就删；
- 时长：按 session_minutes 重新分配每个动作的分钟数，必要时减少动作数；
- 伤病：长期不适部位对应的风险动作直接换掉；
- 餐食：按目标热量补一份加餐，避免"目标 2220、三餐 1903"。

所有修改都会写进 payload["guard"]，前端/Console 可追溯。这个模块是纯函数，
不改数据库、不依赖请求上下文，便于单测。
"""

from __future__ import annotations

from app.domain.exercise_catalog import EXERCISE_CATALOG
from app.domain.planner import EXCLUDE_ACTION_TERMS
from app.domain.day_plan import allowed_equipment, pick_alternative
from app.domain.safety_rules import detect_regions, is_risky_for

INJURY_WORDS = ("膝", "腰", "肩", "腕", "踝", "肘", "疼", "痛", "不舒服", "不适", "受伤", "拉伤", "扭到")

# 同一动作在 catalog / 旧模板里有不同 ref（squat / goblet_squat / goblet-squat），
# 去重前先归一，否则会把"高脚杯深蹲"当成两个动作。
_REF_ALIASES = {
    "squat": "goblet-squat",
    "goblet-squat": "goblet-squat",
    "glute-bridge": "glute-bridge",
    "hip-thrust": "hip-thrust",
    "barbell-hip-thrust": "hip-thrust",
}


def _ref_key(value: str) -> str:
    key = (value or "").strip().lower().replace("_", "-")
    return _REF_ALIASES.get(key, key)


def _norm_equipment(value: str) -> str:
    raw = (value or "").strip().lower().replace("_", " ").replace("-", " ")
    if raw in ("body weight", "bodyweight", "none", ""):
        return "bodyweight"
    return raw.replace(" ", "_")


def _allowed(profile) -> set[str]:
    # 和"今天"页用同一套器械规则，避免两处口径不同。
    return allowed_equipment(profile, {})


def _disliked_refs(memory: dict | None) -> set[str]:
    """把"不喜欢深蹲"翻译成 ref 黑名单，避免替换时又换回用户讨厌的动作。"""
    tokens: set[str] = set()
    for label in (memory or {}).get("dislikes") or []:
        config = EXCLUDE_ACTION_TERMS.get(str(label))
        if config:
            tokens.update(token for token in config[0] if token)
    if not tokens:
        return set()
    blocked: set[str] = set()
    for ref, meta in EXERCISE_CATALOG.items():
        text = f"{ref} {meta.get('name_zh') or ''}".lower()
        if any(token in text for token in tokens):
            blocked.add(_ref_key(ref))
    for alias in ("squat", "goblet-squat", "bodyweight-squat"):
        if any("squat" == token or "squat" in token for token in tokens):
            blocked.add(_ref_key(alias))
    return blocked


def _injured_regions(memory: dict | None, instruction: str = "") -> list[str]:
    """长期不适始终生效；本次原话提到的不适只对这一份计划生效。"""
    text = "；".join(str(item) for item in (memory or {}).get("injuries_long_term") or [])
    if instruction and any(word in instruction for word in INJURY_WORDS):
        text += "；" + instruction
    return detect_regions(text) if text else []


def _display_name(action: dict) -> str:
    meta = action.get("meta") or {}
    return str(meta.get("name_zh") or meta.get("name_en") or action.get("action_ref") or "动作")


def _replace_for_equipment(action: dict, allowed: set[str], injured: list[str],
                           used: set[str]) -> dict | None:
    """换一个同肌群/同模式、器械可用、且不在当天已用清单里的动作。"""
    meta = action.get("meta") or {}
    blocked = {_ref_key(item) for item in used}
    candidate = None
    for _ in range(8):
        candidate = pick_alternative(
            {
                "action_ref": action.get("action_ref"),
                "name": _display_name(action),
                "meta": meta,
            },
            allowed=allowed,
            injured=injured,
            used=blocked,
        )
        if candidate is None:
            return None
        if _ref_key(candidate["ref"]) not in blocked:
            break
        blocked.add(_ref_key(candidate["ref"]))
        candidate = None
    if candidate is None:
        return None
    updated = dict(action)
    updated["action_ref"] = candidate["ref"]
    updated["meta"] = {
        **meta,
        "name_zh": candidate["name"],
        "name_en": candidate["name"],
        "equipment": candidate["equipment"],
        "movement_mode": candidate.get("movement_mode") or meta.get("movement_mode"),
        "primary_muscle": candidate.get("primary_muscle") or meta.get("primary_muscle"),
        "guarded_from": action.get("action_ref"),
    }
    return updated


def _fit_day(day: dict, minutes: int, changes: list[str], formal_action_count: int | None = None) -> None:
    """把一天的训练压进时长预算：先减动作数，再重分分钟数。"""
    actions = [item for item in (day.get("actions") or [])
               if item.get("action_type") == "exercise"]
    if not actions:
        return
    work_budget = max(minutes - 5, 5)  # 5 分钟热身是固定成本
    # 40 分钟可以放下 4 个动作（每个 8–9 分钟）；30 分钟 3 个；20 分钟 2 个。
    # 之前固定上限 3 会把用户刚要求新增的"练背动作"直接删掉（显式编辑被守卫回滚）。
    if work_budget >= 32:
        max_actions = 4
    elif work_budget >= 20:
        max_actions = 3
    elif work_budget >= 10:
        max_actions = 2
    else:
        max_actions = 1
    if formal_action_count is not None:
        max_actions = formal_action_count
    if len(actions) > max_actions:
        # 先保不同肌群，再按原顺序补位：20 分钟时宁可留"腿 + 上肢"，也不要两个深蹲。
        chosen: list[dict] = []
        seen_muscles: set[str] = set()
        for item in actions:
            muscle = str((item.get("meta") or {}).get("primary_muscle") or "").lower()
            if muscle and muscle in seen_muscles:
                continue
            chosen.append(item)
            seen_muscles.add(muscle)
            if len(chosen) >= max_actions:
                break
        for item in actions:
            if len(chosen) >= max_actions:
                break
            if item not in chosen:
                chosen.append(item)
        keep = {id(item) for item in chosen}
        removed = [item for item in actions if id(item) not in keep]
        day["actions"] = [item for item in (day.get("actions") or [])
                          if item.get("action_type") != "exercise" or id(item) in keep]
        changes.append(
            f"第 {day.get('day_index')} 天按 {minutes} 分钟预算删掉 "
            + "、".join(_display_name(item) for item in removed)
        )
        actions = chosen[:max_actions]
    if not actions:
        return
    per_action = max(3, round(work_budget / len(actions)))
    for item in actions:
        if item.get("duration_min") != per_action:
            item["duration_min"] = per_action
    # 20 分钟以内不再给每个动作排 3 组，先保证做得完。
    if minutes <= 25:
        for item in actions:
            sets = int(item.get("sets") or 3)
            if sets > 2:
                item["sets"] = 2
                changes.append(f"{_display_name(item)} 组数 3 → 2（{minutes} 分钟预算）")


def reconcile_meals(payload: dict) -> list[str]:
    """核对每日餐食与热量目标，并把差额写进 payload 供 UI 明示。

    不擅自加餐：减脂模板只有三餐（增肌才加 snack），改餐次会改变产品语义。
    差额用"剩余 X kcal 可加餐"展示，避免"目标 2220、三餐 1903"却没有任何解释。
    """
    meals = payload.get("meals") or []
    nutrition = payload.get("nutrition") or {}
    target_kcal = float(nutrition.get("target_kcal") or 0)
    if not meals or target_kcal <= 0:
        return []
    target_protein = float(
        ((nutrition.get("macros") or {}).get("protein_g") or {}).get("low_g") or 0
    )
    totals: dict[int, dict] = {}
    for meal in meals:
        day = int(meal.get("day_index") or 1)
        bucket = totals.setdefault(day, {"kcal": 0.0, "protein_g": 0.0})
        bucket["kcal"] += float(meal.get("kcal") or 0)
        bucket["protein_g"] += float(meal.get("protein_g") or 0)
    payload["meal_totals"] = {
        "target_kcal": target_kcal,
        "target_protein_g": target_protein,
        "days": {
            str(day): {
                **value,
                "remaining_kcal": round(target_kcal - value["kcal"], 1),
                "remaining_protein_g": round(target_protein - value["protein_g"], 1),
            }
            for day, value in totals.items()
        },
    }
    return []


def _factual_summary(payload: dict, profile) -> str:
    """用最终数据生成摘要，避免"文案说 1 组、卡片写 2 组"（评审 P-004）。"""
    days = payload.get("days") or []
    nutrition = payload.get("nutrition") or {}
    minutes = (payload.get("program") or {}).get("session_minutes") or getattr(
        profile, "session_minutes", 0) or 30
    goal = {"fat_loss": "减脂", "muscle_gain": "增肌",
            "cardio": "心肺", "health": "综合健康"}.get(
                str(getattr(profile, "goal", "")), "综合健康")
    first = days[0].get("actions") if days else []
    names = "、".join(_display_name(item) for item in first[:3])
    sets = sorted({int(item.get("sets") or 0) for day in days for item in day.get("actions") or []
                   if item.get("action_type") == "exercise" and item.get("sets")})
    set_text = f"{sets[0]} 组" if len(sets) == 1 else "按动作 2–3 组"
    kcal = int(nutrition.get("target_kcal") or 0)
    low = ((nutrition.get("macros") or {}).get("protein_g") or {}).get("low_g")
    high = ((nutrition.get("macros") or {}).get("protein_g") or {}).get("high_g")
    protein = f"{int(low)}–{int(high)} g" if low and high else "按体重"
    text = f"每周 {len(days)} 天、每次约 {int(minutes)} 分钟，目标{goal}。"
    if names:
        text += f"以 {names} 为主，每个动作 {set_text}。"
    if kcal:
        text += f"饮食目标 {kcal} kcal、蛋白 {protein}。"
    return text


def enforce_plan_constraints(payload: dict, profile, memory: dict | None = None,
                             instruction: str = "", formal_action_count: int | None = None) -> list[str]:
    """对一份 ready 的计划草案做器械/时长/伤病/餐食校正（原地修改）。"""
    if payload.get("status") != "ready":
        return []
    allowed = _allowed(profile)
    injured = _injured_regions(memory, instruction)
    changes: list[str] = []
    days = list(payload.get("days") or [])
    disliked = _disliked_refs(memory)
    weekly_days = int(getattr(profile, "weekly_days", 0) or 0)
    if weekly_days and len(days) > weekly_days:
        dropped = days[weekly_days:]
        payload["days"] = days[:weekly_days]
        changes.append(f"按每周 {weekly_days} 天删掉 {len(dropped)} 个训练日")
        days = days[:weekly_days]

    minutes = int(
        (payload.get("program") or {}).get("session_minutes")
        or getattr(profile, "session_minutes", 0)
        or 30
    )

    for day in days:
        actions = day.get("actions") or []
        # 只在"同一天"内去重：A/B 两天都练深蹲是合理的，不能因为第 2 天用过
        # 就把第 3 天的深蹲也判成重复。
        used: set[str] = {
            _ref_key(str(action.get("action_ref")))
            for action in actions
            if action.get("action_type") == "exercise" and action.get("action_ref")
        } | disliked
        kept: list[dict] = []
        for action in actions:
            # 只有"3 组 · 12 分钟"没有次数，新手不知道一组做几次；
            # 计划数据补一个保守的起始次数（后续按完成度调整）。
            if (action.get("action_type") == "exercise"
                    and action.get("slot") == "strength"
                    and not action.get("reps")):
                action["reps"] = 10
                action.setdefault("meta", {})["reps_source"] = "guard_default"
            if action.get("action_type") != "exercise":
                kept.append(action)
                continue
            meta = action.get("meta") or {}
            equipment = _norm_equipment(str(meta.get("equipment") or ""))
            ref = str(action.get("action_ref") or "")
            risky = bool(injured) and (
                is_risky_for(ref, injured[0], meta) or
                any(is_risky_for(ref, region, meta) for region in injured)
            )
            if equipment in allowed and not risky:
                kept.append(action)
                used.add(_ref_key(ref))
                continue
            replacement = _replace_for_equipment(action, allowed, injured, used | {ref})
            if replacement is None:
                changes.append(
                    f"第 {day.get('day_index')} 天删除 {_display_name(action)}"
                    f"（当前器械 {equipment or '未知'} 不可用）"
                )
                continue
            kept.append(replacement)
            used.add(_ref_key(str(replacement.get("action_ref"))))
            reason = "伤病规避" if risky else f"器械 {equipment} 不在可用清单"
            changes.append(
                f"{_display_name(action)} → {_display_name(replacement)}（{reason}）"
            )
        day["actions"] = kept
        _fit_day(day, minutes, changes, formal_action_count)

    meal_changes = reconcile_meals(payload)
    changes.extend(meal_changes)

    if changes and payload.get("days"):
        previous_summary = str(payload.get("summary") or "")
        summary = _factual_summary(payload, profile)
        # 守卫只负责把数据压回约束；不能把"这轮没改内容"的诚实标注冲掉。
        if "没有改动计划内容" in previous_summary:
            summary += "（本轮没有改动计划内容）"
        payload["summary"] = summary

    payload.setdefault("meta", {})["guard"] = {
        "allowed_equipment": sorted(allowed),
        "injured_regions": injured,
        "session_minutes": minutes,
        "changes": changes,
    }
    payload["guard_changes"] = changes
    return changes
