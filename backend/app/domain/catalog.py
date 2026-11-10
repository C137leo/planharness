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

# skill 动作目录里有一部分动作没有中文名（只有英文），
# 但它们会被"加个背部动作/换动作"用到，所以这里补齐常用项。
EXTRA_ZH: dict[str, str] = {
    # 模板 / 旧版本里用下划线的 ref，目录里找不到对应中文名
    "goblet_squat": "高脚杯深蹲",
    "one-arm-dumbbell-row": "单臂哑铃划船",
    "inverted-row": "反向划船",
    "bodyweight-squat": "自重深蹲",
    "walking-lunge": "行走弓步",
    "seated-row": "坐姿划船",
    "chest-supported-row": "胸支撑划船",
    "doorway-row": "门框划船",
    "banded-row": "弹力带划船",
    "dead-bug": "死虫式",
    "dynamic_warmup": "动态热身",
    "full_body_stretch": "全身拉伸",
    "jump_squat": "跳跃深蹲",
    "backpack_row": "背包划船",
    "banded-row": "弹力带划船",
    "banded-face-pull": "弹力带面拉",
    "banded-lat-pulldown": "弹力带下拉",
    "inverted-row": "反向划船",
    "doorway-row": "门框划船",
    "dumbbell-bent-over-row": "哑铃俯身划船",
    "chest-supported-row": "胸支撑划船",
    "straight-arm-pulldown": "直臂下拉",
    "seated-leg-curl": "坐姿腿弯举",
    "back-extension": "山羊挺身",
    "arnold-press": "阿诺德推举",
    "smith-machine-bench-press": "史密斯卧推",
}


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
    if ref in EXTRA_ZH:
        return EXTRA_ZH[ref]
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

def enrich_operations(operations: list[dict]) -> list[dict]:
    """给补丁操作补中文名（before_name / after_name）。

    补丁详情、草案、Console 三处都要显示"深蹲 → 臀桥"，
    名字统一在这里解析，前端不用再维护一份映射。
    """
    from app.domain.safety_rules import name_of as catalog_name_of

    def resolve(ref: str) -> str:
        return catalog_name_of(ref) or name_of(ref)

    enriched: list[dict] = []
    for op in operations or []:
        item = dict(op)
        before = item.get("before_action_ref")
        if before:
            item["before_name"] = resolve(str(before))
        after = (item.get("after_action") or {}).get("action_ref")
        if after:
            item["after_name"] = resolve(str(after))
        enriched.append(item)
    return enriched
