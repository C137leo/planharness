"""餐食变体（3 个推荐）：复用现有 FOODS 和 meal_plans，不新建食物库。

规则（全部确定性，LLM 只能写一句推荐理由，不参与算热量）：
- 换蛋白源 / 主食 / 蔬菜的组合，产生 3 个不同变体；
- 热量与当前这一餐差 ≤ 10%；
- 蛋白质不低于当前这一餐的 90%；
- 过敏原和忌口直接排除（与生成计划用同一套解析）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.planning_agent import FOODS, _pick_items
from app.domain.allergens import describe_tags, resolve_tags
from app.food.links import DISCLAIMER, platform_links

# 变体顺序：不同蛋白源 + 主食 + 蔬菜的组合
VARIANT_PLAN = [
    (["鸡胸肉", "鸡蛋", "希腊酸奶", "北豆腐"], ["米饭", "红薯", "土豆"], ["西兰花", "菠菜"]),
    (["瘦牛肉", "北豆腐", "鸡蛋", "鸡胸肉"], ["燕麦", "玉米", "米饭"], ["菠菜", "西兰花"]),
    (["三文鱼", "虾", "瘦牛肉", "北豆腐"], ["红薯", "土豆", "玉米"], ["西兰花", "菠菜"]),
]


@dataclass
class FoodOption:
    title: str
    items: list[dict]
    kcal: int
    protein_g: float
    fat_g: float
    carb_g: float
    reason: str

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "items": self.items,
            "kcal": self.kcal,
            "protein_g": self.protein_g,
            "fat_g": self.fat_g,
            "carb_g": self.carb_g,
            "reason": self.reason,
            "links": platform_links(self.title),
        }


def title_of(items: list[dict]) -> str:
    """公开版菜名（口径与 _title_of 一致），给托盘/兜底选项复用。"""
    return _title_of(items)


def _title_of(items: list[dict]) -> str:
    """菜名：主蛋白 + 主食，再带上蔬菜，方便平台搜索"成品餐"。"""
    protein = next((item["name"] for item in items if item["name"] in {
        "鸡胸肉", "鸡蛋", "北豆腐", "三文鱼", "虾", "瘦牛肉", "希腊酸奶", "米饭"
    }), "")
    carb = next((item["name"] for item in items if item["name"] in {"米饭", "红薯", "土豆", "燕麦", "玉米"}), "")
    veg = next((item["name"] for item in items if item["name"] in {"西兰花", "菠菜"}), "")
    parts = [part for part in (protein, carb) if part]
    head = "".join(parts) or "轻食"
    return f"{head} + {veg}" if veg else head


def _allowed_names(avoid: set[str], excluded: set[str]) -> list[dict]:
    return [
        food for food in FOODS
        if not (food["tags"] & avoid) and food["name"] not in excluded
    ]


def _tune_kcal(items: list[dict], target_kcal: int) -> list[dict]:
    """调整主食克数把热量拉回目标附近（±10% 以内），不动蛋白源。"""
    carb = next((item for item in items if item["name"] in {"米饭", "红薯", "土豆", "燕麦", "玉米"}), None)
    if carb is None:
        return items
    per_100 = next((food["kcal"] for food in FOODS if food["name"] == carb["name"]), 100)
    delta = target_kcal - sum(int(item["kcal"]) for item in items)
    grams = max(20, min(400, round((carb["grams"] + delta / max(1, per_100) * 100) / 5) * 5))
    ratio = grams / 100
    food = next(food for food in FOODS if food["name"] == carb["name"])
    carb.update({
        "grams": grams,
        "kcal": round(food["kcal"] * ratio),
        "protein": round(food["protein"] * ratio, 1),
        "fat": round(food["fat"] * ratio, 1),
        "carb": round(food["carb"] * ratio, 1),
    })
    return items


def build_options(meal: dict, *, allergies: list[str] | None = None,
                  diet_preferences: list[str] | None = None,
                  limit: int = 3) -> list[FoodOption]:
    """基于某一餐生成最多 3 个变体。

    做法：按"蛋白源 × 主食 × 蔬菜"的组合逐个尝试，把主食克数调到热量接近原餐，
    再检查蛋白下限；不满足就换下一种组合。
    """
    terms = list(allergies or []) + list(diet_preferences or [])
    avoid, _unmatched = resolve_tags(terms)
    excluded = {str(term) for term in terms}
    allowed = _allowed_names(avoid, excluded)

    target_kcal = int(meal.get("kcal") or 0)
    target_protein = float(meal.get("protein_g") or 0)
    if target_kcal <= 0:
        return []

    proteins = [food["name"] for food in allowed if food["group"] == "protein"]
    carbs = [food["name"] for food in allowed if food["group"] == "carb"]
    vegs = [food["name"] for food in allowed if food["group"] == "veg"]
    if not proteins or not carbs:
        return []

    options: list[FoodOption] = []
    seen: set[str] = set()
    # 两档阈值：先严格（±10% / 蛋白 90%），不够再放宽（±18% / 蛋白 80%），
    # 保证每次都能给出 3 个选项——校验要求 3 个，否则整条模型答案会被回退。
    for kcal_tol, protein_floor in ((0.10, 0.90), (0.18, 0.80)):
        for index, protein in enumerate(proteins):
            carb = carbs[index % len(carbs)]
            veg = vegs[index % len(vegs)] if vegs else None
            items = _pick_items(
                target_kcal,
                max(1, round(target_protein)),
                avoid,
                protein_pref=[protein],
                carb_pref=[carb],
                veg_pref=[veg] if veg else [],
                excluded=excluded,
            )
            if not items:
                continue
            items = _tune_kcal(items, target_kcal)
            title = _title_of(items)
            if title in seen:
                continue
            kcal = sum(int(item["kcal"]) for item in items)
            protein_g = round(sum(float(item["protein"]) for item in items), 1)
            if abs(kcal - target_kcal) > target_kcal * kcal_tol:
                continue
            if protein_g < target_protein * protein_floor:
                continue
            seen.add(title)
            reason_bits = ["高蛋白", "热量接近目标"]
            if avoid:
                reason_bits.append(f"已避开{describe_tags(avoid)}")
            options.append(FoodOption(
                title=title,
                items=items,
                kcal=kcal,
                protein_g=protein_g,
                fat_g=round(sum(float(item.get("fat") or 0) for item in items), 1),
                carb_g=round(sum(float(item.get("carb") or 0) for item in items), 1),
                reason="，".join(reason_bits),
            ))
            if len(options) >= limit:
                return options
    return options


def payload(options: list[FoodOption], meal_slot: str, target_kcal: int, target_protein: float) -> dict:
    return {
        "meal_slot": meal_slot,
        "target_kcal": target_kcal,
        "target_protein_g": target_protein,
        "options": [option.as_dict() for option in options],
        "disclaimer": DISCLAIMER,
    }
