"""把一次评测跑成两份产物：latest.json（机器读）+ latest.md（人读）。"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from app.evals.runner import EvalRun


def to_payload(run: EvalRun, previous: dict | None = None) -> dict:
    payload = {
        "generated_at": run.started_at,
        "duration_ms": run.duration_ms,
        "python": platform.python_version(),
        "summary": {
            "total": len(run.results),
            "passed": sum(1 for r in run.results if r.passed),
            "failed": len(run.failed_scenarios),
            "all_targets_met": run.passed,
        },
        "metrics": [
            {
                "name": m.name,
                "value": m.value,
                "target": m.target,
                "ok": m.ok,
                "detail": m.detail,
            }
            for m in run.metrics
        ],
        "scenarios": [
            {
                "id": r.id,
                "title": r.title,
                "passed": r.passed,
                "latency_ms": r.latency_ms,
                "error": r.error,
                "checks": [
                    {"name": c.name, "ok": c.ok, "detail": c.detail} for c in r.checks
                ],
                "metrics": {
                    "churn": r.sample.churn,
                    "safety_violations": r.sample.safety_violations,
                    "actions_checked": r.sample.actions_checked,
                    "verifications": len(r.sample.verifications),
                    "patches_created": r.sample.patches_created,
                    "fallbacks": f"{r.sample.fallbacks_used}/{r.sample.fallbacks_total}",
                    "duplicates": f"{r.sample.duplicate_commits}/{r.sample.duplicate_attempts}",
                    "required_dropped": r.sample.required_dropped,
                },
            }
            for r in run.results
        ],
    }

    if previous:
        from app.evals.history import diff_metrics, scenario_diff

        payload["trend"] = {
            "previous_ts": previous.get("ts"),
            "metrics": diff_metrics(
                {m.name: m.value for m in run.metrics}, previous
            ),
            "scenarios": scenario_diff(
                {r.id: r.passed for r in run.results}, previous
            ),
        }
    return payload


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def to_markdown(run: EvalRun, previous: dict | None = None) -> str:
    payload = to_payload(run, previous)
    lines: list[str] = []
    ok_all = payload["summary"]["all_targets_met"]
    badge = "✅ 全部达标" if ok_all else "❌ 未达标"

    lines.append("# PlanHarness Evals 报告")
    lines.append("")
    lines.append(f"> 生成时间：{payload['generated_at']} ｜ 耗时 {payload['duration_ms']} ms ｜ "
                 f"Python {payload['python']}")
    lines.append("")
    lines.append(f"**结论：{badge}** — {payload['summary']['passed']}/{payload['summary']['total']} 个场景通过")
    lines.append("")

    lines.append("## 指标")
    lines.append("")
    lines.append("| 指标 | 实测 | 目标 | 结果 | 说明 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for m in payload["metrics"]:
        mark = "✅" if m["ok"] else "❌"
        lines.append(
            f"| `{m['name']}` | {_fmt(m['value'])} | {m['target']} | {mark} | {m['detail']} |"
        )
    lines.append("")

    trend = payload.get("trend")
    if trend:
        arrow = {"up": "↑", "down": "↓", "same": "=", "new": "new"}
        lines.append(f"## 与上次对比（{trend['previous_ts']}）")
        lines.append("")
        lines.append("| 指标 | 上次 | 本次 | 变化 |")
        lines.append("| --- | --- | --- | --- |")
        for row in trend["metrics"]:
            delta = row["delta"]
            delta_text = "—" if delta is None else f"{delta:+g}"
            lines.append(
                f"| `{row['name']}` | {_fmt(row['previous'])} | {_fmt(row['current'])} "
                f"| {arrow.get(row['status'], row['status'])} {delta_text} |"
            )
        lines.append("")
        regressed = [r for r in trend["scenarios"] if r["status"] == "regressed"]
        fixed = [r for r in trend["scenarios"] if r["status"] == "fixed"]
        if regressed or fixed:
            lines.append(
                f"场景变化：修复 {len(fixed)} 个（{', '.join(r['id'] for r in fixed) or '无'}）"
                f" ｜ 退化 {len(regressed)} 个（{', '.join(r['id'] for r in regressed) or '无'}）"
            )
            lines.append("")

    lines.append("## 场景")
    lines.append("")
    lines.append("| ID | 场景 | 结果 | 耗时(ms) | 断言 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for s in payload["scenarios"]:
        mark = "✅" if s["passed"] else "❌"
        n_ok = sum(1 for c in s["checks"] if c["ok"])
        ratio = f"{n_ok}/{len(s['checks'])}"
        lines.append(f"| {s['id']} | {s['title']} | {mark} | {s['latency_ms']} | {ratio} |")
    lines.append("")

    failures = [s for s in payload["scenarios"] if not s["passed"]]
    if failures:
        lines.append("## 失败归因")
        lines.append("")
        for s in failures:
            lines.append(f"### {s['id']} {s['title']}")
            if s.get("error"):
                lines.append("")
                lines.append(f"- 场景异常：`{s['error']}`")
            for c in s["checks"]:
                if not c["ok"]:
                    lines.append(f"- ❌ `{c['name']}`：{c['detail']}")
            lines.append("")

    lines.append("## 怎么复现")
    lines.append("")
    lines.append("```bash")
    lines.append("cd backend && python -m app.evals")
    lines.append("# 只跑某几个场景")
    lines.append("cd backend && python -m app.evals --only E04,E07")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def write_reports(
    run: EvalRun, out_dir: Path, stem: str = "latest", previous: dict | None = None
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"

    json_path.write_text(
        json.dumps(to_payload(run, previous), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(to_markdown(run, previous) + "\n", encoding="utf-8")
    return json_path, md_path


def default_out_dir() -> Path:
    """仓库根的 evals/reports。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "backend").is_dir() and (parent / "docs").is_dir():
            return parent / "evals" / "reports"
    return Path.cwd() / "evals" / "reports"


def mutation_payload(results) -> dict:
    killed = sum(1 for r in results if r.killed)
    return {
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(timespec="seconds"),
        "score": round(killed / len(results), 4) if results else 0.0,
        "killed": killed,
        "total": len(results),
        "mutants": [
            {
                "id": r.mutant.id,
                "name": r.mutant.name,
                "description": r.mutant.description,
                "expected_killers": list(r.mutant.expected_killers),
                "killed": r.killed,
                "failed_scenarios": r.failed_scenarios,
                "checks_broken": r.checks_broken,
            }
            for r in results
        ],
    }


def mutation_markdown(results) -> str:
    payload = mutation_payload(results)
    lines = ["# PlanHarness 变异测试报告", ""]
    lines.append(
        f"> 生成时间：{payload['generated_at']} ｜ 变异得分 "
        f"**{payload['score']:.0%}**（{payload['killed']}/{payload['total']}）"
    )
    lines.append("")
    lines.append("| ID | 变异体 | 弄坏了什么 | 预期失败场景 | 实测失败 | 结果 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for m in payload["mutants"]:
        mark = "✅ 被杀" if m["killed"] else "❌ 存活"
        lines.append(
            f"| {m['id']} | {m['name']} | {m['description']} "
            f"| {', '.join(m['expected_killers'])} "
            f"| {', '.join(m['failed_scenarios']) or '—'} | {mark} |"
        )
    lines.append("")
    survived = [m for m in payload["mutants"] if not m["killed"]]
    if survived:
        lines.append("## 存活变异体（评测盲区）")
        lines.append("")
        for m in survived:
            lines.append(f"- **{m['id']} {m['name']}**：{m['description']}")
        lines.append("")
    lines.append("## 怎么复现")
    lines.append("")
    lines.append("```bash")
    lines.append("cd backend && python -m app.evals --mutants")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def write_mutation_report(results, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "mutations.json"
    md_path = out_dir / "mutations.md"
    json_path.write_text(
        json.dumps(mutation_payload(results), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(mutation_markdown(results) + "\n", encoding="utf-8")
    return json_path, md_path
