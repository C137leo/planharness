"""热量估算：只算「消耗」，不算「摄入」。

口径（写进接口返回，前端直接展示）：

    kcal = MET × 3.5 × 体重(kg) / 200 × 时长(min)

为什么只算消耗：摄入要精确到克，误差比消耗还大；4 周减脂阶段用户最需要的是
「今天动了多少」这一个数，所以先只做消耗。以后要加摄入，是新增字段而不是改口径。

MET 表是常量查表，不做 LLM 估算——热量必须可复算。
"""

from __future__ import annotations

from app.domain.planning import EXERCISES

# MET 值取自运动能量消耗概要表的常见取值；缺失动作走 DEFAULT_MET 并在返回里标注
MET_TABLE: dict[str, float] = {
    "squat": 5.0,
    "goblet_squat": 5.0,
    "bodyweight_squat": 5.0,
    "jump_squat": 7.0,
    "lunge": 5.0,
    "pushup": 3.8,
    "row": 3.5,
    "backpack_row": 3.5,
    "glute_bridge": 3.5,
    "plank": 3.0,
    "kettlebell_swing": 9.8,
    "burpee": 8.0,
    "run": 8.3,
    "sprint": 11.0,
    "brisk_walk": 4.3,
    "easy_walk": 3.0,
    "stretch": 2.3,
}
DEFAULT_MET = 4.0
FORMULA = "kcal = MET × 3.5 × 体重(kg) / 200 × 时长(min)"


def met_for(action_ref: str) -> tuple[float, bool]:
    """返回 (MET, 是否用了兜底值)。兜底值要在接口里标出来，不能假装精确。"""
    met = MET_TABLE.get(action_ref)
    if met is None:
        return DEFAULT_MET, True
    return met, False


def estimate_kcal(met: float, weight_kg: float, duration_min: float) -> float:
    """标准估算式；结果保留一位小数。"""
    return round(met * 3.5 * float(weight_kg) / 200.0 * float(duration_min), 1)


def default_duration(action_ref: str, action_type: str) -> int:
    """动作库里自带时长就直接用；力量动作按 3 组 × 1 分钟估 12 分钟。"""
    spec = EXERCISES.get(action_ref, {})
    if spec.get("duration_min"):
        return int(spec["duration_min"])
    if action_type == "strength":
        return 12
    return 20


def burn_for_log(log, weight_kg: float) -> dict:
    """把一条 workout_log 算成热量行。"""
    met, fallback = met_for(log.action_ref)
    kcal = estimate_kcal(met, weight_kg, log.duration_min)
    return {
        "log_id": log.id,
        "date": log.date.isoformat(),
        "action_ref": log.action_ref,
        "kind": log.kind,
        "duration_min": log.duration_min,
        "met": met,
        "met_fallback": fallback,
        "kcal": kcal,
    }


def summarize(rows: list[dict], weight_kg: float) -> dict:
    """按天汇总 + 总计。days 里没有记录的天不补零，由前端决定怎么画。"""
    by_date: dict[str, dict] = {}
    for row in rows:
        bucket = by_date.setdefault(
            row["date"], {"date": row["date"], "kcal": 0.0, "duration_min": 0, "sessions": 0}
        )
        bucket["kcal"] = round(bucket["kcal"] + row["kcal"], 1)
        bucket["duration_min"] += row["duration_min"]
        bucket["sessions"] += 1

    by_kind: dict[str, float] = {}
    for row in rows:
        by_kind[row["kind"]] = round(by_kind.get(row["kind"], 0.0) + row["kcal"], 1)

    return {
        "weight_kg": weight_kg,
        "total_kcal": round(sum(row["kcal"] for row in rows), 1),
        "total_duration_min": sum(row["duration_min"] for row in rows),
        "days": [by_date[key] for key in sorted(by_date)],
        "by_kind": by_kind,
        "sessions": len(rows),
        "formula": FORMULA,
        "scope": "burn_only",
        "note": "只统计训练消耗，不含基础代谢与饮食摄入；MET 为通用取值，个体差异可达 ±20%。",
    }
