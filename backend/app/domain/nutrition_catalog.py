"""Canonical ingredient identity, separate from a user's dish/display name."""
from __future__ import annotations

FOOD_MACROS_PER_100G = {
    "鸡胸肉": (165, 31, 3.6, 0), "牛肉": (250, 26, 15, 0),
    "虾": (99, 24, 0.3, 0.2), "豆腐": (76, 8, 4.8, 1.9),
    "鸡蛋": (143, 13, 9.5, 1.1), "三文鱼": (208, 20, 13, 0),
    "米饭": (116, 2.6, 0.3, 25.9), "红薯": (86, 1.6, 0.1, 20.1),
    "西兰花": (34, 2.8, 0.4, 7), "菠菜": (23, 2.9, 0.4, 3.6),
    "希腊酸奶": (59, 10, 0.4, 3.6), "香蕉": (89, 1.1, 0.3, 22.8),
}


# Shared existing planning registry; keep its units and recipe eligibility tags.
PLANNING_FOODS = [
    {"name": "鸡胸肉", "group": "protein", "kcal": 165, "protein": 31, "fat": 3.6, "carb": 0,
     "tags": {"meat"}},
    {"name": "鸡蛋", "group": "protein", "kcal": 143, "protein": 13, "fat": 9.5, "carb": 1.1,
     "tags": {"egg"}},
    {"name": "北豆腐", "group": "protein", "kcal": 76, "protein": 8, "fat": 4.8, "carb": 1.9,
     "tags": {"soy"}},
    {"name": "三文鱼", "group": "protein", "kcal": 208, "protein": 20, "fat": 13, "carb": 0,
     "tags": {"fish", "seafood"}},
    {"name": "虾", "group": "protein", "kcal": 99, "protein": 24, "fat": 0.3, "carb": 0,
     "tags": {"seafood"}},
    {"name": "希腊酸奶", "group": "protein", "kcal": 59, "protein": 10, "fat": 0.4, "carb": 3.6,
     "tags": {"dairy"}},
    {"name": "瘦牛肉", "group": "protein", "kcal": 250, "protein": 26, "fat": 15, "carb": 0,
     "tags": {"meat", "beef"}},
    {"name": "米饭", "group": "carb", "kcal": 130, "protein": 2.7, "fat": 0.3, "carb": 28,
     "tags": set()},
    {"name": "红薯", "group": "carb", "kcal": 86, "protein": 1.6, "fat": 0.1, "carb": 20,
     "tags": set()},
    {"name": "土豆", "group": "carb", "kcal": 77, "protein": 2.0, "fat": 0.1, "carb": 17,
     "tags": set()},
    {"name": "玉米", "group": "carb", "kcal": 96, "protein": 3.4, "fat": 1.5, "carb": 21,
     "tags": set()},
    {"name": "燕麦", "group": "carb", "kcal": 389, "protein": 16.9, "fat": 6.9, "carb": 66,
     "tags": {"gluten"}},
    {"name": "西兰花", "group": "veg", "kcal": 34, "protein": 2.8, "fat": 0.4, "carb": 7,
     "tags": set()},
    {"name": "菠菜", "group": "veg", "kcal": 23, "protein": 2.9, "fat": 0.4, "carb": 3.6,
     "tags": set()},
    {"name": "苹果", "group": "fruit", "kcal": 52, "protein": 0.3, "fat": 0.2, "carb": 14,
     "tags": set()},
]

# Structured catalog aliases, not parsing of user instructions.
PLANNING_INGREDIENT_KEYS = {"北豆腐": "豆腐", "瘦牛肉": "牛肉"}
FOOD_TAGS = {}
for _food in PLANNING_FOODS:
    _key = PLANNING_INGREDIENT_KEYS.get(_food["name"], _food["name"])
    FOOD_MACROS_PER_100G.setdefault(_key, tuple(_food[field] for field in ("kcal", "protein", "fat", "carb")))
    FOOD_TAGS.setdefault(_key, set()).update(_food['tags'])


def ingredient_key(name: str) -> str | None:
    name = str(name or "").strip()
    if name in FOOD_MACROS_PER_100G:
        return name
    # Catalog-backed entity grounding, not natural-language routing. A mixed
    # dish containing multiple known ingredients is not one ingredient.
    matches = [key for key in FOOD_MACROS_PER_100G if key in name]
    return matches[0] if len(matches) == 1 else None
