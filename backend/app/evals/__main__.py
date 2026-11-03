"""命令行入口：python -m app.evals [--only E01,E04] [--out DIR] [--quiet]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.evals.history import append_record, load_records, make_record
from app.evals.report import default_out_dir, write_mutation_report, write_reports
from app.evals.runner import run_all
from app.evals.scenarios import SCENARIOS


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="跑 PlanHarness 任务级评测")
    parser.add_argument(
        "--only",
        default="",
        help="只跑指定场景，逗号分隔，例如 E04,E07",
    )
    parser.add_argument(
        "--out",
        default="",
        help="报告输出目录（默认 <repo>/evals/reports）",
    )
    parser.add_argument("--quiet", action="store_true", help="只输出结论，不逐个打印场景")
    parser.add_argument(
        "--mutants", action="store_true", help="跑变异测试（故意弄坏护栏，看评测能否抓住）"
    )
    parser.add_argument(
        "--no-history", action="store_true", help="不写入历史、不做趋势对比"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    only = [s.strip().upper() for s in args.only.split(",") if s.strip()]
    known = {s.id for s in SCENARIOS}
    unknown = [s for s in only if s not in known]
    if unknown:
        print(f"未知场景：{unknown}；可选：{sorted(known)}", file=sys.stderr)
        return 2

    if args.mutants:
        return _run_mutants(Path(args.out) if args.out else default_out_dir())

    run = run_all(only or None)

    if not args.quiet:
        for result in run.results:
            mark = "PASS" if result.passed else "FAIL"
            print(f"  [{mark}] {result.id} {result.title}  ({result.latency_ms} ms)")
            for check in result.failed():
                print(f"         ✗ {check.name}: {check.detail}")
            if result.error:
                print(f"         ✗ {result.error}")

    out_dir = Path(args.out) if args.out else default_out_dir()
    # 全量跑才更新 latest；子集跑单独存一份，避免把全量报告覆盖成残缺版
    stem = "latest" if not only else "partial-" + "-".join(only)

    previous = None
    if not only and not args.no_history:
        records = load_records()
        previous = records[-1] if records else None

    json_path, md_path = write_reports(run, out_dir, stem=stem, previous=previous)

    if not only and not args.no_history:
        append_record(make_record(run))

    print()
    print("指标：")
    for metric in run.metrics:
        mark = "ok  " if metric.ok else "FAIL"
        value = "—" if metric.value is None else metric.value
        if not metric.sampled:
            mark = "skip"
        print(f"  [{mark}] {metric.name:26} {value!s:>10}  (目标 {metric.target})")

    passed = sum(1 for r in run.results if r.passed)
    print()
    print(f"场景：{passed}/{len(run.results)} 通过 ｜ 结论：{'全部达标' if run.passed else '未达标'}")
    print(f"报告：{json_path}")
    print(f"      {md_path}")

    return 0 if run.passed else 1


def _run_mutants(out_dir: Path) -> int:
    from app.evals.mutations import mutation_score, run_mutants, survivors

    baseline = run_all()
    if not all(r.passed for r in baseline.results):
        print("基线不绿，变异测试没有意义；先修场景。", file=sys.stderr)
        return 2

    results = run_mutants()
    for r in results:
        mark = "KILLED  " if r.killed else "SURVIVED"
        print(f"  [{mark}] {r.mutant.id} {r.mutant.name}")
        print(f"           预期被杀 {list(r.mutant.expected_killers)} → 实测失败 {r.failed_scenarios}")
        for broken in r.checks_broken[:2]:
            print(f"           ✗ {broken}")

    score = mutation_score(results)
    json_path, md_path = write_mutation_report(results, out_dir)
    print()
    print(f"变异得分：{score:.0%}（{len(results) - len(survivors(results))}/{len(results)}）")
    print(f"报告：{json_path}")
    print(f"      {md_path}")
    return 0 if not survivors(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
