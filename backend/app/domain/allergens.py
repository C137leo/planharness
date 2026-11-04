"""过敏原 / 忌口识别。

第一版是「精确字符串 → 标签」的字典，结果用户写「海鲜过敏」「对海鲜过敏」「甲壳类」
就全部匹配不上，食谱照样推荐虾——这是安全问题，不能靠用户写法恰好标准。

现在改成关键词匹配，并且**识别不了就明说**：
- 命中 → 排除对应标签的食材；
- 没命中 → 不假装安全，把原词放进 `unmatched`，由上层提示用户人工确认。
"""

from __future__ import annotations

import re

# 标签 → 关键词（中英混写都认）。顺序无关，命中即排除。
ALLERGEN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "dairy": ("乳", "奶", "芝士", "奶酪", "黄油", "dairy", "milk", "lactose", "cheese", "yogurt"),
    "egg": ("蛋", "egg"),
    "peanut": ("花生", "peanut"),
    "nuts": ("坚果", "核桃", "腰果", "杏仁", "榛子", "nut", "almond", "cashew"),
    "seafood": ("海鲜", "虾", "蟹", "贝", "甲壳", "海产", "shellfish", "seafood", "shrimp", "prawn"),
    "fish": ("鱼", "三文鱼", "金枪鱼", "鳕鱼", "fish", "salmon", "tuna"),
    "soy": ("豆", "大豆", "豆腐", "豆浆", "soy", "tofu"),
    "gluten": ("麸质", "小麦", "面", "面包", "面条", "gluten", "wheat"),
    "beef": ("牛肉", "牛羊肉", "beef"),
    "lamb": ("羊肉", "lamb", "mutton"),
    "pork": ("猪肉", "pork"),
    "meat": ("肉", "meat"),
}

# 这些词太宽，单独出现时不该当成过敏（例如"我对肉没兴趣"不是过敏）
WEAK_TERMS = {"肉", "面", "豆", "蛋", "奶", "鱼"}

_NORMALIZE = re.compile(r"[\s,，、;；/]+")


def split_terms(raw: list[str] | str | None) -> list[str]:
    """把 ["海鲜过敏"] 或 "海鲜、花生" 统一拆成词条。"""
    if not raw:
        return []
    items = [raw] if isinstance(raw, str) else list(raw)
    terms: list[str] = []
    for item in items:
        for part in _NORMALIZE.split(str(item)):
            part = part.strip()
            if part:
                terms.append(part)
    return terms


def resolve_tags(terms: list[str] | str | None) -> tuple[set[str], list[str]]:
    """返回 (命中的标签集合, 没识别出来的原词)。

    匹配是"包含"关系：`海鲜过敏` 含 `海鲜` → seafood；`不吃牛羊肉` 含 `牛肉` → beef。
    """
    tags: set[str] = set()
    unmatched: list[str] = []

    for term in split_terms(terms):
        lowered = term.lower()
        hit = False
        for tag, keywords in ALLERGEN_KEYWORDS.items():
            for keyword in keywords:
                if keyword in lowered:
                    # 弱词要求整词相同，避免"面"命中"面包"以外的奇怪情况
                    if keyword in WEAK_TERMS and lowered != keyword:
                        continue
                    tags.add(tag)
                    hit = True
                    break
        if not hit:
            unmatched.append(term)
    return tags, unmatched


def describe_tags(tags: set[str]) -> str:
    """把标签变成人话，用于提示语。"""
    labels = {
        "dairy": "乳制品", "egg": "蛋类", "peanut": "花生", "nuts": "坚果",
        "seafood": "甲壳/海鲜", "fish": "鱼类", "soy": "大豆", "gluten": "小麦/麸质",
        "beef": "牛肉", "lamb": "羊肉", "pork": "猪肉", "meat": "肉类",
    }
    return "、".join(labels.get(tag, tag) for tag in sorted(tags))
