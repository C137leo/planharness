"""抓取两份公开数据源，缓存到 backend/data/rag/。

- free-exercise-db（The Unlicense，公共领域）：876 个动作，JSON 全量
- USDA FoodData Central（CC0/public domain，DEMO_KEY 可试用）：常见食物营养

只做"取数 + 缓存"，不做清洗；清洗在 build_rag_corpus.py 里。
DEMO_KEY 有严格限流（每小时几十次），所以命中缓存就不再请求。
"""

from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "rag"
EXERCISES = DATA_DIR / "free_exercise_db.json"
FOUNDATION_JSON_RAW = DATA_DIR / "foundation_food_raw.json"

# 从 Foundation Foods 里显式挑 31 条常见食物（避免自动匹配选到"鱼油/膨化米"这类条目）
FOUNDATION_URL = ("https://fdc.nal.usda.gov/fdc-datasets/"
                  "FoodData_Central_foundation_food_json_2026-10-29.zip")
FOUNDATION_ZIP = DATA_DIR / "foundation_food.zip"
FOUNDATION_JSON = DATA_DIR / "foods_foundation.json"

FOOD_PICKS: list[tuple[int, str]] = [
    (30, "鸡胸肉（熟）"), (29, "鸡腿肉（熟）"), (251, "鸡腿肉（生）"),
    (42, "牛里脊（熟）"), (219, "牛肉末 90% 瘦"), (248, "猪里脊（生）"),
    (264, "三文鱼（生）"), (266, "虾（生）"), (38, "金枪鱼罐头（水浸）"),
    (97, "鸡蛋（全蛋）"), (66, "全脂牛奶"), (26, "希腊酸奶（脱脂）"),
    (177, "低脂奶酪"), (20, "切达奶酪"), (189, "燕麦片（生）"),
    (217, "糙米（生）"), (218, "白米（生）"), (197, "红薯（生）"),
    (194, "土豆（生）"), (89, "西兰花（生）"), (135, "菠菜（生）"),
    (5, "羽衣甘蓝（生）"), (199, "黄瓜（带皮）"), (294, "牛油果"),
    (118, "香蕉"), (120, "苹果"), (204, "蓝莓"), (186, "杏仁（生）"),
    (173, "花生酱"), (96, "橄榄油"), (235, "鹰嘴豆（干）"), (236, "扁豆（干）"),
]

def fetch_exercises() -> list[dict]:
    if EXERCISES.exists():
        return json.loads(EXERCISES.read_text(encoding="utf-8"))
    url = "https://api.github.com/repos/yuhonas/free-exercise-db/contents/dist/exercises.json"
    with httpx.Client(timeout=120, trust_env=False) as client:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    raw = base64.b64decode(payload["content"])
    data = json.loads(raw)
    EXERCISES.parent.mkdir(parents=True, exist_ok=True)
    EXERCISES.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


# 明显对不上的品类词：搜 "salmon" 返回 "Fish oil, salmon" 这种要扣分
BAD_WORDS = ("oil", "puffed", "leaves", "yogurt", "babyfood", "infant", "formula",
             "snacks", "candies", "beverages", "restaurant")


def _pick_best(foods: list[dict], query: str) -> dict | None:
    """按"描述和查询词的重合度"挑，而不是拿到第一条就用。

    实测坑：按 dataType 取第一条，`salmon` 会选到 "Fish oil, salmon"，
    `brown rice` 会选到 "Brown rice, puffed"。
    """
    tokens = [token for token in query.lower().split() if len(token) > 2]
    best, best_score = None, -10**9
    for item in foods:
        desc = str(item.get("description") or "").lower()
        score = 0.0
        score += sum(12 for token in tokens if token in desc)
        score += 8 if desc.startswith(tokens[0]) else 0
        score += 6 if item.get("dataType") == "SR Legacy" else 0
        score += 4 if item.get("dataType") == "Foundation" else 0
        score -= sum(25 for word in BAD_WORDS if word in desc and word not in query.lower())
        score -= 0.05 * len(desc)
        if score > best_score:
            best, best_score = item, score
    return best


def _nutrient(food: dict, name: str) -> tuple[float | None, str]:
    """返回 (数值, 单位)。优先 kcal 的 Energy，缺失再用 Atwater。"""
    wanted = [name, "Energy (Atwater General Factors)", "Energy (Atwater Specific Factors)"]
    for target in wanted:
        for item in food.get("foodNutrients") or []:
            if str(item.get("nutrientName") or "").lower() != target.lower():
                continue
            unit = str(item.get("unitName") or "")
            if target == "Energy" and unit.lower() != "kcal":
                continue
            try:
                return float(item.get("value") or 0), unit
            except (TypeError, ValueError):
                continue
    return None, ""


def _nutrient(food: dict, name: str, unit: str | None = None) -> float | None:
    for item in food.get("foodNutrients") or []:
        nutrient = item.get("nutrient") or {}
        if str(nutrient.get("name") or "").lower() != name.lower():
            continue
        if unit and str(nutrient.get("unitName") or "").lower() != unit.lower():
            continue
        value = item.get("amount", item.get("median"))
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _kcal(food: dict) -> float | None:
    """优先 kcal；只有 kJ 就换算；两者都没有就按宏量回算。"""
    kcal = _nutrient(food, "Energy", "kcal")
    if kcal is not None:
        return kcal
    kj = _nutrient(food, "Energy", "kJ")
    if kj is not None:
        return round(kj / 4.184, 1)
    protein = _nutrient(food, "Protein") or 0.0
    fat = _nutrient(food, "Total lipid (fat)") or 0.0
    carb = _nutrient(food, "Carbohydrate, by difference") or 0.0
    return round(4 * protein + 9 * fat + 4 * carb, 1) if (protein or fat or carb) else None


def fetch_foods() -> list[dict]:
    """下载 Foundation Foods 全量 JSON，按固定索引挑出 31 条常见食物。

    为什么不用搜索 API：DEMO_KEY 很快 429；全量 zip 只有 ~700KB，本地筛选更稳。
    """
    if not FOUNDATION_ZIP.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=120, trust_env=False) as client:
            response = client.get(FOUNDATION_URL)
            response.raise_for_status()
            FOUNDATION_ZIP.write_bytes(response.content)
    if not FOUNDATION_JSON_RAW.exists():
        with zipfile.ZipFile(FOUNDATION_ZIP) as archive:
            name = next(item for item in archive.namelist() if item.endswith(".json"))
            FOUNDATION_JSON_RAW.write_bytes(archive.read(name))
    data = json.loads(FOUNDATION_JSON_RAW.read_text(encoding="utf-8"))
    foods = data.get("FoundationFoods") or []
    out: list[dict] = []
    for index, zh in FOOD_PICKS:
        if index >= len(foods):
            continue
        food = foods[index]
        out.append({
            "query": str(food.get("description") or ""),
            "zh": zh,
            "fdc_id": food.get("fdcId"),
            "description": food.get("description"),
            "kcal": _kcal(food),
            "protein_g": _nutrient(food, "Protein"),
            "fat_g": _nutrient(food, "Total lipid (fat)"),
            "carb_g": _nutrient(food, "Carbohydrate, by difference"),
        })
    FOUNDATION_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    # 全量可用食物（406 条）：给"扩到 600 chunks"用，中文名在 build 阶段翻译
    all_foods: list[dict] = []
    for food in foods:
        kcal = _kcal(food)
        protein = _nutrient(food, "Protein")
        if kcal is None or protein is None:
            continue
        all_foods.append({
            "fdc_id": food.get("fdcId"), "description": food.get("description"),
            "kcal": kcal, "protein_g": protein,
            "fat_g": _nutrient(food, "Total lipid (fat)"),
            "carb_g": _nutrient(food, "Carbohydrate, by difference"),
        })
    (DATA_DIR / "foods_all.json").write_text(
        json.dumps(all_foods, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    exercises = fetch_exercises()
    print(f"动作：{len(exercises)} 条 -> {EXERCISES}")
    foods = fetch_foods()
    print(f"食物：{len(foods)} 条 -> {FOUNDATION_JSON}")
