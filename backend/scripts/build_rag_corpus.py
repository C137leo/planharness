"""把两份公开数据源加工成中文 RAG 语料（data/rag/chunks.jsonl）。

数据源与许可：
- free-exercise-db：800+ 动作，The Unlicense（公共领域）
- USDA FoodData Central（Foundation Foods 2026-10）：公共领域 / CC0

做的事：
1. 挑 40 个常见动作（深蹲/卧推/硬拉/划船/推举/核心…），用模型把英文要点概括成 2 句中文；
2. 把 30 种常见食物的 USSF 每 100g 营养拼成中文条目；
3. 输出统一字段（含 license / source_url），供 app/knowledge/rag.py 检索。

翻译结果缓存在 data/rag/exercise_zh.json；--no-translate 可跳过模型调用。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data" / "rag"
EXERCISES = DATA / "free_exercise_db.json"
FOODS = DATA / "foods_foundation.json"
ZH_CACHE = DATA / "exercise_zh.json"
OUT = DATA / "chunks.jsonl"

EXERCISE_PICKS: list[tuple[str, str, str, list[str]]] = [
    ("Barbell Squat", "杠铃深蹲", "legs", ["深蹲", "下肢", "股四头肌"]),
    ("Goblet Squat", "高脚杯深蹲", "legs", ["深蹲", "下肢", "哑铃"]),
    ("Dumbbell Squat", "哑铃深蹲", "legs", ["深蹲", "下肢"]),
    ("Barbell Lunge", "杠铃弓步", "legs", ["弓步", "下肢"]),
    ("Dumbbell Lunges", "哑铃弓步", "legs", ["弓步", "下肢"]),
    ("Leg Press", "腿举", "legs", ["腿举", "器械"]),
    ("Leg Extensions", "腿屈伸", "legs", ["腿屈伸", "股四头肌"]),
    ("Lying Leg Curls", "俯卧腿弯举", "legs", ["腿弯举", "腘绳肌"]),
    ("Standing Calf Raises", "站姿提踵", "legs", ["提踵", "小腿"]),
    ("Barbell Hip Thrust", "杠铃臀推", "hip", ["臀推", "臀部"]),
    ("Barbell Glute Bridge", "杠铃臀桥", "hip", ["臀桥", "臀部"]),
    ("Single Leg Glute Bridge", "单腿臀桥", "hip", ["臀桥", "臀部"]),
    ("Barbell Deadlift", "杠铃硬拉", "back", ["硬拉", "后链", "下背"]),
    ("Romanian Deadlift", "罗马尼亚硬拉", "back", ["硬拉", "腘绳肌"]),
    ("Bent Over Barbell Row", "杠铃俯身划船", "back", ["划船", "背部"]),
    ("Bent Over Two-Dumbbell Row", "哑铃俯身划船", "back", ["划船", "背部"]),
    ("Seated Cable Rows", "坐姿绳索划船", "back", ["划船", "背部"]),
    ("Wide-Grip Lat Pulldown", "宽握高位下拉", "back", ["下拉", "背阔肌"]),
    ("Pullups", "引体向上", "back", ["引体向上", "背部"]),
    ("Chin-Up", "反手引体向上", "back", ["引体向上", "肱二头肌"]),
    ("Barbell Bench Press - Medium Grip", "杠铃卧推", "chest", ["卧推", "胸"]),
    ("Barbell Incline Bench Press - Medium Grip", "上斜杠铃卧推", "chest", ["卧推", "上胸"]),
    ("Dumbbell Bench Press", "哑铃卧推", "chest", ["卧推", "胸"]),
    ("Incline Dumbbell Press", "上斜哑铃卧推", "chest", ["卧推", "上胸"]),
    ("Dumbbell Flyes", "哑铃飞鸟", "chest", ["飞鸟", "胸"]),
    ("Cable Crossover", "绳索夹胸", "chest", ["夹胸", "胸"]),
    ("Pushups", "俯卧撑", "chest", ["俯卧撑", "自重"]),
    ("Barbell Shoulder Press", "杠铃肩推", "shoulder", ["肩推", "推举"]),
    ("Dumbbell Shoulder Press", "哑铃肩推", "shoulder", ["肩推", "推举"]),
    ("Side Lateral Raise", "哑铃侧平举", "shoulder", ["侧平举", "肩"]),
    ("Lateral Raise - With Bands", "弹力带侧平举", "shoulder", ["侧平举", "弹力带"]),
    ("Face Pull", "面拉", "shoulder", ["面拉", "肩袖"]),
    ("Barbell Shrug", "杠铃耸肩", "back", ["耸肩", "斜方肌"]),
    ("Barbell Curl", "杠铃弯举", "arm", ["弯举", "肱二头肌"]),
    ("Alternate Hammer Curl", "锤式弯举", "arm", ["弯举", "肱二头肌"]),
    ("Dumbbell Bicep Curl", "哑铃弯举", "arm", ["弯举", "肱二头肌"]),
    ("Triceps Pushdown", "三头下压", "arm", ["臂屈伸", "肱三头肌"]),
    ("Bench Dips", "凳上臂屈伸", "arm", ["臂屈伸", "肱三头肌"]),
    ("Plank", "平板支撑", "core", ["核心", "平板"]),
    ("Crunches", "卷腹", "core", ["核心", "腹肌"]),
    ("Side Bridge", "侧支撑", "core", ["核心", "侧腹"]),
    ("Hanging Leg Raise", "悬垂举腿", "core", ["核心", "腹肌"]),
    ("Child's Pose", "婴儿式拉伸", "core", ["拉伸", "恢复"]),
]

MUSCLE_ZH = {
    "chest": "胸大肌", "shoulders": "三角肌", "biceps": "肱二头肌", "triceps": "肱三头肌",
    "lats": "背阔肌", "middle back": "中背", "lower back": "下背", "traps": "斜方肌",
    "abdominals": "腹部", "quadriceps": "股四头肌", "hamstrings": "腘绳肌",
    "glutes": "臀大肌", "calves": "小腿", "forearms": "前臂", "adductors": "内收肌",
    "abductors": "外展肌", "neck": "颈部",
}
EQUIP_ZH = {
    "body only": "自重", "barbell": "杠铃", "dumbbell": "哑铃", "machine": "器械",
    "cable": "绳索", "bands": "弹力带", "kettlebells": "壶铃", "other": "其它",
}
LEVEL_ZH = {"beginner": "入门", "intermediate": "中级", "expert": "高级"}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _parse_cues(content: str) -> list[str]:
    """模型可能回数组、也可能回 {"items": [...]}，还可能带 ```json 围栏。"""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        left = min((idx for idx in (text.find("["), text.find("{")) if idx >= 0), default=-1)
        if left < 0:
            return []
        right = max(text.rfind("]"), text.rfind("}"))
        parsed = json.loads(text[left:right + 1])
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    if isinstance(parsed, dict):
        for value in parsed.values():
            if isinstance(value, list):
                return [str(item) for item in value]
    return []


def translate_cues(exercises: list[dict], *, use_model: bool) -> dict[str, str]:
    if ZH_CACHE.exists():
        return json.loads(ZH_CACHE.read_text(encoding="utf-8"))
    if not use_model:
        return {}
    import os

    import httpx

    from app.config import get_settings

    settings = get_settings()
    key = settings.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return {}
    mapping: dict[str, str] = {}
    # 分批翻译：一次 40 多个动作容易超 max_tokens 导致 JSON 截断
    for start in range(0, len(exercises), 6):
        batch = exercises[start:start + 6]
        payload = [
            {"name": item["name"], "steps": (item.get("instructions") or [])[:3]}
            for item in batch
        ]
        prompt = (
            "把每个动作的英文要点概括成 2 句中文动作要领，面向健身初学者："
            "第一句写起始姿势，第二句写发力与常见错误。不要输出英文，不要加编号。"
            "只输出 JSON 数组，长度与输入一致，元素是字符串。\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        try:
            response = httpx.post(
                f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": settings.deepseek_model,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.2, "max_tokens": 4000,
                      # deepseek-flash 默认先思考：不给足预算会出现
                      # finish_reason=length + content 为空
                      "reasoning_effort": "low",
                      "response_format": {"type": "json_object"}},
                timeout=120,
            )
            response.raise_for_status()
            cues = _parse_cues(response.json()["choices"][0]["message"]["content"])
            for index, item in enumerate(batch):
                if index < len(cues) and cues[index].strip():
                    mapping[item["name"]] = cues[index].strip()
        except Exception as exc:  # noqa: BLE001 - 单批失败不影响其它批次
            print(f"  第 {start // 6 + 1} 批翻译失败（{type(exc).__name__}）")
            continue
    missing = len(exercises) - len(mapping)
    if mapping:
        ZH_CACHE.write_text(json.dumps(mapping, ensure_ascii=False, indent=1), encoding="utf-8")
    if missing:
        print(f"  有 {missing} 个动作没翻译，用英文要点兜底")
    return mapping


def _muscle_text(muscles: list[str]) -> str:
    return "、".join(MUSCLE_ZH.get(m, m) for m in muscles) or "—"


def build_exercise_chunks(cues: dict[str, str]) -> list[dict]:
    data = json.loads(EXERCISES.read_text(encoding="utf-8"))
    by_name = {item["name"]: item for item in data}
    chunks: list[dict] = []
    for name, zh, body_part, tags in EXERCISE_PICKS:
        item = by_name.get(name)
        if item is None:
            continue
        muscles = item.get("primaryMuscles") or []
        equipment = EQUIP_ZH.get(item.get("equipment") or "", item.get("equipment") or "")
        level = LEVEL_ZH.get(item.get("level") or "", item.get("level") or "")
        cue = cues.get(name) or " ".join((item.get("instructions") or [])[:2])
        mechanic = {"compound": "复合动作", "isolation": "孤立动作"}.get(
            str(item.get("mechanic") or ""), item.get("mechanic") or "—")
        content = (
            f"主要肌群：{_muscle_text(muscles)}；器械：{equipment}；难度：{level}；"
            f"动作类型：{mechanic}。动作要领：{cue}"
        )
        chunks.append({
            "id": f"ex:{_slug(name)}",
            "title": f"{zh}（{name}）",
            "content": content,
            "tags": tags + [zh, equipment, level],
            "body_part": body_part,
            "topic": "technique",
            "kind": "exercise",
            "lang": "zh",
            "source_title": "free-exercise-db（The Unlicense）",
            "source_url": "https://github.com/yuhonas/free-exercise-db",
            "license": "Unlicense",
            "meta": {"equipment": item.get("equipment"), "level": item.get("level"),
                     "muscles": muscles, "translated": name in cues},
        })
    return chunks


BAD_WORDS = ("oil", "puffed", "leaves", "yogurt", "babyfood", "infant", "formula",
             "snacks", "candies", "beverages", "restaurant", "fast food")


def _pick_food(foods: list[dict], query: str) -> dict | None:
    tokens = [token for token in query.lower().split() if len(token) > 2]
    best, best_score = None, -10**9
    for item in foods:
        desc = str(item.get("description") or "").lower()
        score = sum(10 for token in tokens if token in desc)
        score += 6 if desc.startswith(tokens[0]) else 0
        score -= sum(20 for word in BAD_WORDS if word in desc and word not in query.lower())
        score -= 0.03 * len(desc)
        if score > best_score:
            best, best_score = item, score
    return best


def _amount(food: dict, name: str) -> tuple[float | None, str]:
    for item in food.get("foodNutrients") or []:
        nutrient = item.get("nutrient") or {}
        if str(nutrient.get("name") or "").lower() != name.lower():
            continue
        unit = str(nutrient.get("unitName") or "")
        if name == "Energy" and unit.lower() != "kcal":
            continue
        amount = item.get("amount")
        if amount is None:
            amount = item.get("median")
        try:
            return float(amount), unit
        except (TypeError, ValueError):
            continue
    return None, ""


def build_food_chunks() -> list[dict]:
    foods = json.loads(FOODS.read_text(encoding="utf-8"))
    chunks: list[dict] = []
    for item in foods:
        zh = item["zh"]
        kcal = item.get("kcal")
        protein = item.get("protein_g")
        fat = item.get("fat_g")
        carb = item.get("carb_g")
        if kcal is None or protein is None:
            continue
        content = (
            f"每 100 g：能量 {kcal:.0f} kcal，蛋白质 {protein:.1f} g，"
            f"脂肪 {fat if fat is not None else 0:.1f} g，碳水 {carb if carb is not None else 0:.1f} g。"
            f"英文原名：{item.get('description')}"
        )
        chunks.append({
            "id": f"food:{item.get('fdc_id')}",
            "title": f"{zh}（{item.get('description')}）",
            "content": content,
            "tags": [zh, "食物", "营养", "每100克"],
            "body_part": "general",
            "topic": "nutrition",
            "kind": "food",
            "lang": "zh",
            "source_title": "USDA FoodData Central（Foundation Foods，公共领域）",
            "source_url": f"https://fdc.nal.usda.gov/food-details/{item.get('fdc_id')}/nutrients",
            "license": "CC0-1.0",
            "meta": {"per": "100g", "kcal": kcal, "protein_g": protein,
                     "fat_g": fat, "carb_g": carb},
        })
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-translate", action="store_true", help="跳过模型翻译，用英文要点")
    args = parser.parse_args()

    data = json.loads(EXERCISES.read_text(encoding="utf-8"))
    by_name = {item["name"]: item for item in data}
    picked = [by_name[name] for name, *_ in EXERCISE_PICKS if name in by_name]
    cues = translate_cues(picked, use_model=not args.no_translate)
    chunks = build_exercise_chunks(cues) + build_food_chunks()
    OUT.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in chunks) + "\n",
                   encoding="utf-8")
    print(f"动作 {len(build_exercise_chunks(cues))} 条 / 食物 {len(build_food_chunks())} 条 "
          f"-> {OUT}")
    for item in chunks[:2]:
        print("-", item["title"], "|", item["content"][:120])


if __name__ == "__main__":
    main()
