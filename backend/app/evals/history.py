"""历史指标记录：每次全量评测追加一行，用于和上一次对比趋势。

为什么用 JSONL 而不是 SQLite：
- 追加只写一行，冲突少、可 diff、可提交进仓库；
- 报告里只需要「上一次」和「最近 N 次」，线性扫描足够。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.evals.runner import EvalRun


def history_path() -> Path:
    from app.evals.report import default_out_dir

    return default_out_dir().parent / "history.jsonl"


def load_records(path: Path | None = None) -> list[dict]:
    p = path or history_path()
    if not p.exists():
        return []
    records: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 坏行跳过，不影响其余历史
    return records


def make_record(run: EvalRun) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": sum(1 for r in run.results if r.passed),
        "total": len(run.results),
        "all_targets_met": run.passed,
        "duration_ms": run.duration_ms,
        "metrics": {m.name: m.value for m in run.metrics},
        "scenarios": {r.id: r.passed for r in run.results},
    }


def append_record(record: dict, path: Path | None = None) -> Path:
    p = path or history_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return p


def diff_metrics(current: dict[str, float | None], previous: dict | None) -> list[dict]:
    """逐指标对比。方向按「越小越好 / 越大越好」由调用方在报告里展示。"""
    if not previous:
        return []
    prev_metrics = previous.get("metrics") or {}
    rows: list[dict] = []
    for name, value in current.items():
        prev = prev_metrics.get(name)
        if value is None or prev is None:
            delta = None
        else:
            delta = round(float(value) - float(prev), 6)
        rows.append(
            {
                "name": name,
                "current": value,
                "previous": prev,
                "delta": delta,
                "status": (
                    "new"
                    if prev is None
                    else "same"
                    if delta == 0
                    else ("up" if (delta or 0) > 0 else "down")
                ),
            }
        )
    return rows


def scenario_diff(current: dict[str, bool], previous: dict | None) -> list[dict]:
    if not previous:
        return []
    prev = previous.get("scenarios") or {}
    rows: list[dict] = []
    for sid, ok in current.items():
        before = prev.get(sid)
        if before is None:
            status = "new"
        elif before == ok:
            status = "same"
        elif ok:
            status = "fixed"
        else:
            status = "regressed"
        rows.append({"id": sid, "previous": before, "current": ok, "status": status})
    return rows
