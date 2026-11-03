"""从 skill 的动作目录生成 Python 动作表。

为什么要生成而不是运行时读：补丁校验、动作替换、安全门都在 Python 侧同步执行，
不能每次都起一个 Node 进程查名字。生成出来的文件提交进仓库，
skill 更新时重跑这个脚本即可（`python scripts/sync_exercise_catalog.py`）。

用法：
    SKILL_DIR=/path/to/healthy-fitness-coach-skill python scripts/sync_exercise_catalog.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SKILL_DIR = Path(os.environ.get("SKILL_DIR", "/Users/leo/PycharmProjects/healthy-fitness-coach-skill"))
CATALOG = SKILL_DIR / "healthy-fitness-coach" / "assets" / "workout-guide-catalog.json"
GUIDE = SKILL_DIR / "healthy-fitness-coach" / "references" / "workout-guide.js"
TARGET = Path(__file__).resolve().parents[1] / "app" / "domain" / "exercise_catalog.py"

# 膝痛时需要避开的动作：按名称判断（比按肌群判断更接近真实禁忌）
KNEE_RISK_PATTERNS = (
    "squat", "lunge", "leg-press", "hack", "split", "step-up", "pistol",
    "run", "jump", "sprint", "lunge", "deadlift", "good-morning",
)


def chinese_aliases() -> dict[str, str]:
    """从 workout-guide.js 的 aliases 表里抽出「中文名 → slug」。"""
    text = GUIDE.read_text(encoding="utf-8")
    start = text.index("const aliases = {")
    end = text.index("};", start)
    block = text[start:end]
    pairs: dict[str, str] = {}
    for line in block.splitlines()[1:]:
        for chunk in line.split("',"):
            if ":" not in chunk:
                continue
            left, _, right = chunk.partition(":")
            left = left.strip().strip("'").strip('"').strip()
            right = right.strip().strip("'").strip('"').strip()
            if left and right.startswith(("'", "")) and right:
                right = right.strip("'")
                if left and right and " " not in right[:1]:
                    pairs[left] = right
    return pairs


def main() -> None:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    aliases = chinese_aliases()
    by_slug: dict[str, dict] = {}
    for exercise in data["exercises"]:
        slug = exercise["slug"]
        name_en = exercise["name"]
        risky = any(pattern in slug for pattern in KNEE_RISK_PATTERNS)
        by_slug[slug] = {
            "name_en": name_en,
            "name_zh": next((zh for zh, target in aliases.items() if target == slug), ""),
            "equipment": exercise.get("equipment") or "",
            "primary_muscle": (exercise.get("muscles") or {}).get("primary") or "",
            "frames": [frame.get("displayPath") for frame in exercise.get("frames") or []][:3],
            "knee_risky": risky,
        }

    lines = [
        '"""动作目录（由 scripts/sync_exercise_catalog.py 从 skill 生成，不要手改）。',
        "",
        f"来源：{SKILL_DIR.name} / healthy-fitness-coach/assets/workout-guide-catalog.json",
        "用途：补丁校验（动作是否存在）、替换建议、膝痛安全门的初筛。",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "EXERCISE_CATALOG: dict[str, dict] = {",
    ]
    for slug in sorted(by_slug):
        item = by_slug[slug]
        lines.append(f"    {slug!r}: {item!r},")
    lines += [
        "}",
        "",
        "KNEE_RISKY_SLUGS = {slug for slug, item in EXERCISE_CATALOG.items() if item['knee_risky']}",
        "",
        "",
        "def name_of(ref: str) -> str:",
        '    """动作中文名；没有中文名就退回英文名，再退回 slug。"""',
        "    item = EXERCISE_CATALOG.get(ref) or {}",
        "    return item.get('name_zh') or item.get('name_en') or ref",
        "",
    ]
    TARGET.write_text("\n".join(lines), encoding="utf-8")
    print(f"写入 {TARGET}：{len(by_slug)} 个动作，其中膝痛风险 {sum(1 for i in by_slug.values() if i['knee_risky'])} 个")


if __name__ == "__main__":
    main()
