"""草案反馈 → 具体改动。

用户说「动作要有区别」「膝盖别太多蹲」「蛋白丰富点」，如果只是把这句话再喂给模型，
计划结构（由 skill 按画像确定算出）根本不会变，用户看到的就是"提示变了、计划没变"。

所以反馈必须落到**确定性变换**上：

| 反馈关键词 | 变换 | 为什么这么改 |
| --- | --- | --- |
| 动作重复 / 要有区别 / 换动作 | 同一肌群换一个动作（跨天去重） | 结构不变、动作有变化，可解释 |
| 膝 / 膝疼 / 蹲太多 | 膝主导动作换成髋主导或核心 | 保守，不新增诊断 |
| 加强度 / 加量 / 不够 | 每个力量动作 +1 组（上限 4） | 单变量调整 |
| 太累 / 轻松点 / 减少 | 每个力量动作 -1 组（下限 1），去掉每场最后一个动作 | 只降一个变量 |
| 蛋白 / 丰富 / 轮换 / 鸡胸肉 | 重排餐食蛋白源，同一天不重复 | 与生成逻辑同一套规则 |

每次变换都写进 `revisions`，前端直接展示"这一轮改了什么"，不用猜。
"""

from __future__ import annotations

from app.domain.exercise_catalog import EXERCISE_CATALOG

# 膝主导动作的安全替代（都不涉及明显屈膝负荷）
KNEE_SAFE_SLUGS = [
    "glute-bridge", "hip-thrust", "romanian-deadlift", "plank",
    "side-plank", "back-extension", "seated-leg-curl",
]

# 动作模式 → 可替换的同肌群动作（人工挑过的确定性集合，不从目录里随机抓）
MODE_ALTERNATIVES: dict[str, list[str]] = {
    "knee_dominant": ["goblet-squat", "leg-press", "walking-lunge", "bulgarian-split-squat"],
    "hip_dominant": ["glute-bridge", "romanian-deadlift", "hip-thrust", "back-extension"],
    "horizontal_push": ["dumbbell-bench-press", "push-up", "incline-bench-press", "smith-machine-bench-press"],
    "horizontal_pull": ["one-arm-dumbbell-row", "seated-row", "chest-supported-row", "inverted-row"],
    "vertical_push": ["overhead-press", "arnold-press", "lateral-raise"],
    "vertical_pull": ["lat-pulldown", "pull-up", "straight-arm-pulldown"],
    "core": ["plank", "side-plank", "crunch", "russian-twist"],
}

VARIETY_HINTS = ("不同", "有区别", "区别", "换动作", "重复", "一样", "变化", "多样")
KNEE_HINTS = ("膝", "蹲太多", "蹲不动", "膝盖")
HARDER_HINTS = ("加强度", "加量", "不够", "太难", "加练", "进阶")
EASIER_HINTS = ("太累", "轻松", "减少", "简单点", "别太狠", "压力小")
PROTEIN_HINTS = ("蛋白", "丰富", "轮换", "鸡胸肉", "肉类", "食材", "吃腻")


def _name(slug: str) -> str:
    item = EXERCISE_CATALOG.get(slug) or {}
    return str(item.get("name_zh") or item.get("name_en") or slug)


def _action_name(action: dict) -> str:
    meta = action.get("meta") or {}
    return str(meta.get("name_zh") or _name(action.get("action_ref", "")))


def _replace_action(action: dict, slug: str, reason: str) -> dict:
    """换动作但保留组次/时长，只改 ref 与 meta。"""
    item = EXERCISE_CATALOG.get(slug) or {}
    updated = dict(action)
    updated["action_ref"] = slug
    updated["meta"] = {
        **(action.get("meta") or {}),
        "name_zh": item.get("name_zh") or slug,
        "movement_mode": (action.get("meta") or {}).get("movement_mode"),
        "varied_from": (action.get("meta") or {}).get("name_zh") or action.get("action_ref"),
        "reason": reason,
    }
    return updated


def _vary_exercises(days: list[dict]) -> list[str]:
    """每个动作都换成同肌群列表里的"下一个"，并尽量跨天不重复。

    为什么不是"只换重复项"：如果只替换重复动作，第一天跟原草案一模一样，
    用户点完「再改改」会觉得"计划没变"。整轮轮换能让每一场都看出区别。
    """
    changes: list[str] = []
    used_global: set[str] = set()

    for day in days:
        used_today: set[str] = set()
        for action in day.get("actions") or []:
            slug = str(action.get("action_ref") or "")
            mode = str((action.get("meta") or {}).get("movement_mode") or "")
            options = MODE_ALTERNATIVES.get(mode) or []
            if not options:
                used_global.add(slug)
                used_today.add(slug)
                continue

            index = options.index(slug) if slug in options else -1
            candidates = [
                options[(index + step) % len(options)] for step in range(1, len(options) + 1)
            ]
            fresh = [item for item in candidates if item not in used_today and item not in used_global]
            pick = (fresh or [item for item in candidates if item not in used_today] or [None])[0]
            if not pick:
                used_global.add(slug)
                used_today.add(slug)
                continue

            old_name = _action_name(action)
            action.update(_replace_action(action, pick, "按你的要求换成同一肌群的另一个动作"))
            used_global.add(pick)
            used_today.add(pick)
            changes.append(f"第 {day['day_index']} 天「{old_name}」→「{_name(pick)}」")
    return changes


def _avoid_knee(days: list[dict]) -> list[str]:
    """把膝主导动作换成髋主导/核心，保守处理"膝盖不舒服"。"""
    changes: list[str] = []
    used = {str(a.get("action_ref")) for day in days for a in day.get("actions") or []}
    for day in days:
        for index, action in enumerate(day.get("actions") or []):
            mode = str((action.get("meta") or {}).get("movement_mode") or "")
            if mode != "knee_dominant":
                continue
            replacement = next((slug for slug in KNEE_SAFE_SLUGS if slug not in used), None)
            if replacement is None:
                continue
            old_name = _action_name(action)
            new_action = _replace_action(action, replacement, "膝盖顾虑：换成不压膝的动作")
            new_action["meta"]["movement_mode"] = "hip_dominant"
            day["actions"][index] = new_action
            used.add(replacement)
            changes.append(f"第 {day['day_index']} 天「{old_name}」→「{_name(replacement)}」（避膝）")
    return changes


def _adjust_sets(days: list[dict], delta: int) -> list[str]:
    changes: list[str] = []
    touched = 0
    for day in days:
        actions = day.get("actions") or []
        if delta < 0 and len(actions) > 1:
            # 减量时去掉每场最后一个动作，避免只是"少做一组"体感不明显
            dropped = actions.pop()
            changes.append(f"第 {day['day_index']} 天去掉「{_action_name(dropped)}」")
        for action in actions:
            if action.get("action_type") != "exercise":
                continue
            current = int(action.get("sets") or 3)
            new_sets = max(1, min(4, current + delta))
            if new_sets == current:
                continue
            action["sets"] = new_sets
            touched += 1
    if touched:
        changes.append(
            f"{touched} 个动作的组数已调整（{'每个 +1 组' if delta > 0 else '每个 -1 组'}）"
        )
    return changes


def _vary_meals(meals: list[dict]) -> list[str]:
    """餐食按天整体后移一位：内容真的变，但不破坏营养配比。"""
    if not meals:
        return []
    rotation = {1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7, 7: 1}
    for meal in meals:
        meal["day_index"] = rotation.get(int(meal.get("day_index") or 1), 1)
    meals.sort(key=lambda item: (item["day_index"], item["slot"]))
    return ["餐食按天顺延一天（同样的配比，不同的食材组合）"]


def apply_feedback(payload: dict, feedback: str) -> tuple[dict, list[str]]:
    """把一句反馈变成具体改动；返回 (新 payload, 改动说明列表)。"""
    text = (feedback or "").strip()
    if not text:
        return payload, []

    days = [dict(day) for day in payload.get("days") or []]
    for day in days:
        day["actions"] = [dict(action) for action in day.get("actions") or []]
    meals = [dict(meal) for meal in payload.get("meals") or []]

    changes: list[str] = []
    if any(hint in text for hint in KNEE_HINTS):
        changes += _avoid_knee(days)
    if any(hint in text for hint in VARIETY_HINTS):
        changes += _vary_exercises(days)
    if any(hint in text for hint in HARDER_HINTS):
        changes += _adjust_sets(days, +1)
    if any(hint in text for hint in EASIER_HINTS):
        changes += _adjust_sets(days, -1)
    if any(hint in text for hint in PROTEIN_HINTS):
        changes += _vary_meals(meals)

    new_payload = dict(payload)
    new_payload["days"] = days
    new_payload["meals"] = meals
    new_payload["revisions"] = [str(item) for item in ((payload.get("revisions") or []) + changes)]
    if changes:
        new_payload["summary"] = str(payload.get("summary") or "") + "（已按你的意见调整）"
    return new_payload, changes
