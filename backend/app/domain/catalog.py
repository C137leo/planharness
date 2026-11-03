"""动作/餐食的命名与可选集：一处收口，补丁、生成、前端文案都查这里。

两套 ref 并存是现实：
- 模板计划用短 ref（`squat` / `pushup` / `chicken_rice_bowl`）；
- skill 生成的计划用它的 slug（`goblet-squat` / `dumbbell-bench-press`）。
安全门和替换建议必须同时认得两者，否则"换了计划来源就静默漏检"。
"""

from __future__ import annotations

from app.domain.exercise_catalog import EXERCISE_CATALOG, KNEE_RISKY_SLUGS
from app.domain.planning import EXERCISES, MEALS

MEAL_CATALOG = {meal["ref"]: meal for meal in MEALS}


def all_exercise_refs() -> list[str]:
    """可选动作 = skill 目录 + 模板动作，去重后排序，方便模型挑。"""
    return sorted(set(EXERCISE_CATALOG) | set(EXERCISES))


def all_meal_refs() -> list[str]:
    return sorted(MEAL_CATALOG)


def name_of(ref: str) -> str:
    """中文名优先；没有就用英文名；再退回 ref。"""
    # 模板 ref 优先：模板的 `squat` 指高脚杯深蹲，而 skill 目录的 `squat` 是杠铃深蹲
    template = EXERCISES.get(ref) or {}
    if template.get("name"):
        return str(template["name"])
    item = EXERCISE_CATALOG.get(ref) or {}
    if item.get("name_zh"):
        return str(item["name_zh"])
    if item.get("name_en"):
        return str(item["name_en"])
    meal = MEAL_CATALOG.get(ref) or {}
    return str(meal.get("name") or ref)


def is_knee_risky_ref(ref: str) -> bool:
    normalized = str(ref or "").replace("-", "_").lower()
    if str(ref) in KNEE_RISKY_SLUGS:
        return True
    keywords = ("squat", "lunge", "run", "jump", "step_up", "split", "deadlift", "leg_press")
    return any(keyword in normalized for keyword in keywords)


def refs_for_actions(actions: list[dict]) -> list[str]:
    """替换建议只在"同一类"里选：训练动作不会换成一餐饭。"""
    kinds = {action.get("action_type") for action in actions}
    refs: list[str] = []
    if "exercise" in kinds:
        refs += all_exercise_refs()
    if "meal" in kinds:
        refs += all_meal_refs()
    return refs
