"""用户对草案说「把红薯换成土豆，再新增一个练背动作」这类**具体编辑**。

上一版只处理模糊反馈（"动作要有区别"），具体指令反而没被识别，于是
模型在摘要里写"已经换了"，实际计划没动——这是最坏的一种"看起来做了"。

这里把具体编辑拆成可验证的操作：
- 食材替换：`把X换成Y` / `X改成Y` / `不要X`
- 新增动作：`新增/加一个 + 部位`（背/胸/腿/臀/核心/有氧）
- 删除动作：`去掉/不要 + 动作名`
- 动作替换：`把A换成B`（B 是动作名或 slug）

所有操作都在 payload 上做确定性变换，改完重算餐食宏量，并把改动写进 `revisions`。
"""

from __future__ import annotations

import re

from app.agent.planning_agent import FOODS
from app.domain.exercise_catalog import EXERCISE_CATALOG
from app.domain.revisions import (
    EASIER_HINTS,
    HARDER_HINTS,
    KNEE_HINTS,
    PROTEIN_HINTS,
    VARIETY_HINTS,
)

FOOD_INDEX = {food["name"]: food for food in FOODS}
FOOD_ALIASES = {
    "土豆": "土豆", "马铃薯": "土豆", "洋芋": "土豆",
    "红薯": "红薯", "地瓜": "红薯", "番薯": "红薯",
    "米饭": "米饭", "白米饭": "米饭", "大米饭": "米饭",
    "鸡胸": "鸡胸肉", "鸡胸肉": "鸡胸肉",
    "鸡蛋": "鸡蛋", "蛋": "鸡蛋",
    "牛肉": "瘦牛肉", "瘦牛肉": "瘦牛肉",
    "豆腐": "北豆腐", "北豆腐": "北豆腐",
    "酸奶": "希腊酸奶", "希腊酸奶": "希腊酸奶",
    "三文鱼": "三文鱼", "鱼": "三文鱼",
    "虾": "虾", "西兰花": "西兰花", "菠菜": "菠菜", "燕麦": "燕麦", "苹果": "苹果",
    "玉米": "玉米",
}

# 部位 → 动作模式（新增动作时用）
PART_TO_MODE = {
    "背": "horizontal_pull", "背部": "horizontal_pull", "划船": "horizontal_pull",
    "引体": "vertical_pull", "下拉": "vertical_pull",
    "胸": "horizontal_push", "推": "horizontal_push",
    "肩": "vertical_push", "推举": "vertical_push",
    "腿": "knee_dominant", "深蹲": "knee_dominant", "蹲": "knee_dominant",
    "臀": "hip_dominant", "髋": "hip_dominant", "硬拉": "hip_dominant",
    "核心": "core", "腹": "core", "平板": "core",
}

MODE_TO_SLUG = {
    "horizontal_pull": "one-arm-dumbbell-row",
    "vertical_pull": "lat-pulldown",
    "horizontal_push": "dumbbell-bench-press",
    "vertical_push": "overhead-press",
    "knee_dominant": "goblet-squat",
    "hip_dominant": "glute-bridge",
    "core": "plank",
}

SWAP_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9]{1,12}?)\s*(?:换成|改成|替换成|改为)\s*([\u4e00-\u9fa5A-Za-z0-9]{1,12})"
)
REPLACE_NOUN_RE = re.compile(
    r"(?:不要|去掉|别吃|不吃)\s*([\u4e00-\u9fa5A-Za-z0-9]{1,8})[，,、\s]*(?:换成|改成|改吃|换成别的)"
)
REMOVE_RE = re.compile(r"(?:不要|去掉|删掉|删除|换掉)\s*([\u4e00-\u9fa5A-Za-z0-9]{1,8})")
ADD_HINTS = ("新增", "加一个", "加个", "再加", "增加一个", "多练")
REMOVE_HINTS = ("不要", "去掉", "删掉", "删除")


def _resolve(raw: str, known: set[str]) -> str | None:
    """在已知名字里找出 raw 中包含的那个。

    用户不会按格式说话：「主食把红薯换成土豆即可」里，来源是「主食把红薯」、
    目标是「土豆即可」。直接查表一定失败，所以要从长到短找子串。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if text in known:
        return text
    for length in range(len(text) - 1, 1, -1):
        for start in range(0, len(text) - length + 1):
            candidate = text[start:start + length]
            if candidate in known:
                return candidate
    return None


def _food_name(raw: str) -> str | None:
    known = set(FOOD_ALIASES) | set(FOOD_INDEX)
    found = _resolve(raw, known)
    if found is None:
        return None
    return FOOD_ALIASES.get(found, found)


def _exercise_slug(raw: str) -> str | None:
    text = (raw or "").strip()
    if text in EXERCISE_CATALOG:
        return text
    chinese = {
        str(item.get("name_zh")): slug
        for slug, item in EXERCISE_CATALOG.items()
        if item.get("name_zh")
    }
    found = _resolve(text, set(chinese))
    if found is not None:
        return chinese[found]
    return _resolve(text, set(MODE_TO_SLUG.values()))


def _recalc_meal(meal: dict) -> None:
    meal["kcal"] = sum(int(item.get("kcal") or 0) for item in meal.get("items") or [])
    meal["protein_g"] = round(sum(float(item.get("protein") or 0) for item in meal.get("items") or []), 1)
    meal["fat_g"] = round(sum(float(item.get("fat") or 0) for item in meal.get("items") or []), 1)
    meal["carb_g"] = round(sum(float(item.get("carb") or 0) for item in meal.get("items") or []), 1)


def _swap_food_in_meals(meals: list[dict], source: str, target: str) -> list[str]:
    """把食谱里的 X 换成 Y，克数按同等热量换算，避免热量跳动。"""
    changes: list[str] = []
    for meal in meals:
        for item in meal.get("items") or []:
            if item.get("name") != source:
                continue
            food = FOOD_INDEX[target]
            old_kcal = float(item.get("kcal") or 0)
            grams = max(20, min(400, round(old_kcal / max(1, food["kcal"]) * 100 / 5) * 5))
            ratio = grams / 100
            item.update({
                "name": target,
                "grams": grams,
                "kcal": round(food["kcal"] * ratio),
                "protein": round(food["protein"] * ratio, 1),
                "fat": round(food["fat"] * ratio, 1),
                "carb": round(food["carb"] * ratio, 1),
            })
            _recalc_meal(meal)
            changes.append(f"第 {meal.get('day_index')} 天{meal.get('title')}「{source}」→「{target}」")
    return changes


def _add_exercise(days: list[dict], mode: str) -> list[str]:
    """每个训练日加一个该模式的动作（同一动作不重复加）。"""
    slug = MODE_TO_SLUG.get(mode)
    item = EXERCISE_CATALOG.get(slug or "") or {}
    if not slug:
        return []
    name = str(item.get("name_zh") or slug)
    changes: list[str] = []
    for day in days:
        actions = day.get("actions") or []
        if any(action.get("action_ref") == slug for action in actions):
            continue
        actions.append({
            "slot": "strength",
            "seq": len(actions) + 1,
            "action_type": "exercise",
            "action_ref": slug,
            "sets": 3,
            "reps": 12,
            "duration_min": 12,
            "meta": {
                "name_zh": name,
                "movement_mode": mode,
                "primary_muscle": item.get("primary_muscle"),
                "added_by_user": True,
            },
        })
        day["actions"] = actions
        changes.append(f"第 {day.get('day_index')} 天新增「{name}」")
    return changes


def _remove_exercise(days: list[dict], target: str) -> list[str]:
    slug = _exercise_slug(target)
    changes: list[str] = []
    for day in days:
        kept = []
        for action in day.get("actions") or []:
            match = (action.get("action_ref") == slug) if slug else False
            name = str((action.get("meta") or {}).get("name_zh") or "")
            if match or (target and target in name):
                changes.append(f"第 {day.get('day_index')} 天去掉「{name or target}」")
                continue
            kept.append(action)
        day["actions"] = kept
    return changes


EXPLICIT_HINTS = ADD_HINTS + REMOVE_HINTS + ("换成", "改成", "替换成", "改为", "轮换", "加个")


def has_explicit_edits(text: str) -> bool:
    """这句话能不能被确定性执行（具体编辑或已知的模糊指令）。

    能执行就不该让模型反问你"要不要这么做"——直接做出来给你看。
    """
    text = (text or "").strip()
    if not text:
        return False
    if any(hint in text for hint in VARIETY_HINTS + KNEE_HINTS + HARDER_HINTS
           + EASIER_HINTS + PROTEIN_HINTS):
        return True
    if any(hint in text for hint in ("换成", "改成", "替换成", "改为")):
        return True
    if any(hint in text for hint in ADD_HINTS) and any(
        keyword in text for keyword in PART_TO_MODE
    ):
        return True
    if any(hint in text for hint in REMOVE_HINTS) and any(
        item.get("name_zh") and item["name_zh"] in text for item in EXERCISE_CATALOG.values()
    ):
        return True
    return False


def apply_explicit_edits(payload: dict, feedback: str) -> tuple[dict, list[str]]:
    """解析具体编辑并应用；没识别出任何操作时原样返回。"""
    text = (feedback or "").strip()
    if not text:
        return payload, []

    days = [dict(day) for day in payload.get("days") or []]
    for day in days:
        day["actions"] = [dict(action) for action in day.get("actions") or []]
    meals = [dict(meal) for meal in payload.get("meals") or []]
    for meal in meals:
        meal["items"] = [dict(item) for item in meal.get("items") or []]

    changes: list[str] = []

    # 0) 口语替换：不要红薯，换成土豆
    for match in REPLACE_NOUN_RE.finditer(text):
        source = _food_name(match.group(1))
        tail = text[match.end():]
        target_match = re.match(r"\s*([\u4e00-\u9fa5A-Za-z0-9]{1,8})", tail)
        target = _food_name(target_match.group(1)) if target_match else None
        if source and target and source != target:
            changes += _swap_food_in_meals(meals, source, target)

    # 1) 食材替换：把X换成Y
    for match in SWAP_RE.finditer(text):
        source, target = _food_name(match.group(1)), _food_name(match.group(2))
        if source and target and source != target:
            changes += _swap_food_in_meals(meals, source, target)
            continue
        # 2) 动作替换：把深蹲换成臀桥
        src_slug, dst_slug = _exercise_slug(match.group(1)), _exercise_slug(match.group(2))
        if src_slug and dst_slug:
            for day in days:
                for action in day.get("actions") or []:
                    if action.get("action_ref") != src_slug:
                        continue
                    item = EXERCISE_CATALOG.get(dst_slug) or {}
                    action["action_ref"] = dst_slug
                    action["meta"] = {
                        **(action.get("meta") or {}),
                        "name_zh": item.get("name_zh") or dst_slug,
                        "varied_from": src_slug,
                    }
                    changes.append(
                        f"第 {day.get('day_index')} 天「{match.group(1)}」→「{match.group(2)}」"
                    )

    # 3) 新增动作：新增一个练背动作 / 加个划船
    if any(hint in text for hint in ADD_HINTS):
        modes: list[str] = []
        for keyword, mode in PART_TO_MODE.items():
            if keyword in text and mode not in modes:
                modes.append(mode)
        for mode in modes:
            changes += _add_exercise(days, mode)

    # 4) 删除动作：去掉臀桥 / 不要划船
    if any(hint in text for hint in REMOVE_HINTS):
        for match in REMOVE_RE.finditer(text):
            target = match.group(1)
            if _exercise_slug(target) or any(
                target in str((action.get("meta") or {}).get("name_zh") or "")
                for day in days for action in day.get("actions") or []
            ):
                changes += _remove_exercise(days, target)

    if not changes:
        return payload, []

    new_payload = dict(payload)
    new_payload["days"] = days
    new_payload["meals"] = meals
    new_payload["revisions"] = [str(item) for item in (payload.get("revisions") or [])] + changes
    new_payload["summary"] = str(payload.get("summary") or "") + "（已按你的具体修改调整）"
    return new_payload, changes
