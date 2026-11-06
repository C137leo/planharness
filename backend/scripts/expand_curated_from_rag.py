"""从 600 条外部语料生成"清单型"知识条目（可选，需人工复核后入库）。

能生成什么、不能生成什么，先划清楚：

能（事实型，可从数据直接推出）
- 某个部位有哪些常见动作（名字/器械/难度）
- 某个品类有哪些食物（每 100g 热量/蛋白）
出处是 free-exercise-db（Unlicense）/ USDA（CC0），属于**数据整理**，不是医学结论。

不能（结论型，必须人写 + 机构出处）
- "膝痛该换什么动作""什么情况该就医""孕期能不能练"
这些不能从动作库/食物库推导，硬生成就是编。

产物：data/knowledge/expanded.json（默认不自动入库，seed_knowledge 会读取它；
删除该文件即可回滚到 30 条）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHUNKS = ROOT / "data" / "rag" / "chunks.jsonl"
OUT = ROOT / "data" / "knowledge" / "expanded.json"

BODY_PARTS = [
    ("chest", "胸"), ("back", "背"), ("shoulder", "肩"), ("arm", "手臂"),
    ("legs", "腿"), ("hip", "臀"), ("core", "核心"),
]
FOOD_CATEGORIES = [
    ("蛋白质来源", "protein"), ("主食", "carb"), ("蔬菜", "veg"),
    ("水果", "fruit"), ("坚果", "nut"), ("奶制品", "dairy"), ("油脂", "fat"),
]
EX_SOURCE = ("free-exercise-db（The Unlicense）", "https://github.com/yuhonas/free-exercise-db")


def _zh_title(chunk: dict) -> str:
    """从 "杠铃卧推（Barbell Bench Press...）" 里取中文部分。"""
    title = str(chunk.get("title") or "")
    return title.split("（")[0].strip() or title


def _pick(chunks: list[dict], *, kind: str, body_part: str | None = None,
          category: str | None = None, limit: int = 8) -> list[dict]:
    pool = [item for item in chunks if item.get("kind") == kind]
    if kind == "exercise":
        # 清单只列训练动作，拉伸/瑜伽类不进"练 X 的动作"列表
        pool = [item for item in pool
                if not any(word in _zh_title(item) for word in ("拉伸", "瑜伽", "泡沫轴"))]
    if body_part:
        pool = [item for item in pool if item.get("body_part") == body_part]
    if category:
        pool = [item for item in pool if category in (item.get("tags") or [])]
    # 人工确认过的优先，其次名字短的（通常更常用）
    pool.sort(key=lambda item: (
        not (item.get("meta") or {}).get("priority"),
        len(_zh_title(item)),
        _zh_title(item),
    ))
    return pool[:limit]


def build() -> list[dict]:
    chunks = [json.loads(line) for line in CHUNKS.read_text(encoding="utf-8").splitlines() if line]
    entries: list[dict] = []

    for part, zh in BODY_PARTS:
        picked = _pick(chunks, kind="exercise", body_part=part, limit=8)
        if len(picked) < 3:
            continue
        lines = []
        for item in picked:
            meta = item.get("meta") or {}
            equipment = {
                "body only": "自重", "barbell": "杠铃", "dumbbell": "哑铃",
                "machine": "器械", "cable": "绳索", "bands": "弹力带",
                "kettlebells": "壶铃",
            }.get(str(meta.get("equipment")), str(meta.get("equipment") or ""))
            level = {"beginner": "入门", "intermediate": "中级", "expert": "高级"}.get(
                str(meta.get("level")), "")
            lines.append(f"{_zh_title(item)}（{equipment}/{level}）")
        entries.append({
            "id": f"kb-ex-{part}",
            "title": f"练{zh}的常见动作清单",
            "body_part": part,
            "topic": "technique",
            "content": f"练{zh}的常见动作（按器械/难度）：" + "、".join(lines) +
                       "。具体要领见对应条目；有疼痛或伤病时先咨询医生。",
            "tags": [zh, "动作清单", "自动生成"],
            "source_title": EX_SOURCE[0],
            "source_url": EX_SOURCE[1],
            "license": "Unlicense",
            "generated": True,
        })

    for category, _key in FOOD_CATEGORIES:
        picked = _pick(chunks, kind="food", category=category, limit=8)
        if len(picked) < 3:
            continue
        lines = []
        for item in picked:
            meta = item.get("meta") or {}
            kcal = meta.get("kcal")
            protein = meta.get("protein_g")
            if kcal is None:
                continue
            lines.append(f"{_zh_title(item)}（{kcal:.0f} kcal / 蛋白 {protein or 0:.1f} g）")
        if len(lines) < 3:
            continue
        entries.append({
            "id": f"kb-food-{_key}",
            "title": f"{category}清单（每 100g）",
            "body_part": "general",
            "topic": "nutrition",
            "content": f"{category}常见选项（每 100g，USDA 数据）：" + "、".join(lines) +
                       "。份量按你的热量目标调整。",
            "tags": [category, "食物清单", "自动生成"],
            "source_title": "USDA FoodData Central（Foundation Foods，CC0）",
            "source_url": "https://fdc.nal.usda.gov/",
            "license": "CC0-1.0",
            "generated": True,
        })
    return entries


def main() -> None:
    entries = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"生成 {len(entries)} 条清单型知识 -> {OUT}")
    for item in entries[:3]:
        print("-", item["title"], "|", item["content"][:90])


if __name__ == "__main__":
    main()
