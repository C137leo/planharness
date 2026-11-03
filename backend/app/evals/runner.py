"""评测执行器：跑场景 → 聚合成指标 → 交给 report 出报告。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.evals.scenarios import SCENARIOS, ScenarioResult


@dataclass
class Metric:
    name: str
    value: float | None
    target: str
    ok: bool
    detail: str = ""
    sampled: bool = True          # 子集运行时可能没采到样，此时不判失败


@dataclass
class EvalRun:
    results: list[ScenarioResult]
    metrics: list[Metric]
    started_at: str
    duration_ms: float = 0.0
    scenario_errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results) and all(m.ok for m in self.metrics)

    @property
    def failed_scenarios(self) -> list[ScenarioResult]:
        return [r for r in self.results if not r.passed]


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def run_all(only: list[str] | None = None) -> EvalRun:
    """跑全部（或指定）场景。单个场景崩了不影响其他场景。"""
    started = datetime.now(timezone.utc)
    selected = [s for s in SCENARIOS if not only or s.id in only]
    results: list[ScenarioResult] = []
    errors: list[str] = []

    for sc in selected:
        t0 = time.perf_counter()
        try:
            result = sc.run()
        except Exception as exc:  # noqa: BLE001 - 评测要收集失败，不能中断整轮
            result = ScenarioResult(
                id=sc.id,
                title=sc.title,
                checks=[],
                sample=__import__(
                    "app.evals.scenarios", fromlist=["Sample"]
                ).Sample(),
                error=f"{type(exc).__name__}: {exc}",
            )
            errors.append(f"{sc.id}: {type(exc).__name__}: {exc}")
        result.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        results.append(result)

    duration_ms = round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 2)
    return EvalRun(
        results=results,
        metrics=_aggregate(results),
        started_at=started.isoformat(timespec="seconds"),
        duration_ms=duration_ms,
        scenario_errors=errors,
    )


def _aggregate(results: list[ScenarioResult]) -> list[Metric]:
    actions = sum(r.sample.actions_checked for r in results)
    violations = sum(r.sample.safety_violations for r in results)

    verifications = [v for r in results for v in r.sample.verifications]
    patches = sum(r.sample.patches_created for r in results)

    churns = [r.sample.churn for r in results if r.sample.churn is not None]
    required_dropped = sum(r.sample.required_dropped for r in results)

    fallback_total = sum(r.sample.fallbacks_total for r in results)
    fallback_used = sum(r.sample.fallbacks_used for r in results)

    dup_attempts = sum(r.sample.duplicate_attempts for r in results)
    dup_commits = sum(r.sample.duplicate_commits for r in results)

    latencies = [r.latency_ms for r in results]

    metrics: list[Metric] = []

    # 1) 安全违规率：一票否决
    rate = (violations / actions) if actions else 0.0
    metrics.append(
        Metric(
            "safety_violation_rate",
            round(rate, 6),
            "== 0",
            rate == 0,
            f"{violations} 次违规 / {actions} 个动作",
        )
    )

    # 2) 验证通过率
    v_rate = (sum(verifications) / len(verifications)) if verifications else None
    metrics.append(
        Metric(
            "verification_pass_rate",
            round(v_rate, 4) if v_rate is not None else None,
            "== 1.0",
            True if v_rate is None else v_rate == 1.0,
            f"{sum(verifications)}/{len(verifications)} 次验证通过" if v_rate is not None else "本次未采样",
            sampled=v_rate is not None,
        )
    )

    # 3) 审批覆盖率：每一次「产生新版本」的提交，都必须有审批记录。
    #    注意不能用 decisions/patches —— E02 故意停在 pending，指标会被它拉低。
    commits = sum(r.sample.commits for r in results)
    commits_with_approval = sum(r.sample.commits_with_approval for r in results)
    coverage = (commits_with_approval / commits) if commits else None
    metrics.append(
        Metric(
            "approval_coverage",
            round(coverage, 4) if coverage is not None else None,
            "== 1.0",
            True if coverage is None else coverage == 1.0,
            f"{commits_with_approval}/{commits} 次提交有审批记录" if coverage is not None else "本次未采样",
            sampled=coverage is not None,
        )
    )

    # 4) 计划 churn
    churn_mean = _mean(churns)
    metrics.append(
        Metric(
            "plan_churn",
            round(churn_mean, 4) if churn_mean is not None else None,
            "<= 0.30",
            churn_mean is not None and churn_mean <= 0.30,
            f"{len(churns)} 个补丁的均值",
        )
    )

    # 5) required 被丢弃次数：必须为 0
    metrics.append(
        Metric(
            "context_dropped_required",
            float(required_dropped),
            "== 0",
            required_dropped == 0,
            "必需段落（安全边界/约束/目标）从未被丢弃",
        )
    )

    # 6) 降级成功率
    f_rate = (fallback_used / fallback_total) if fallback_total else None
    metrics.append(
        Metric(
            "fallback_success",
            round(f_rate, 4) if f_rate is not None else None,
            "== 1.0",
            True if f_rate is None else f_rate == 1.0,
            f"{fallback_used}/{fallback_total} 次降级拿到可用文案" if f_rate is not None else "本次未采样",
            sampled=f_rate is not None,
        )
    )

    # 7) 重复提交率：重复审批不能产生新版本
    d_rate = (dup_commits / dup_attempts) if dup_attempts else None
    metrics.append(
        Metric(
            "duplicate_commit_rate",
            round(d_rate, 4) if d_rate is not None else None,
            "== 0",
            True if d_rate is None else d_rate == 0,
            f"{dup_commits} 个多余版本 / {dup_attempts} 次重复审批" if d_rate is not None else "本次未采样",
            sampled=d_rate is not None,
        )
    )

    # 8) 平均成本（元/次运行）—— 只统计真正调用过模型的运行
    costs = [r.sample.cost_cny for r in results if (r.sample.cost_cny or 0) > 0]
    avg_cost = _mean(costs)
    metrics.append(
        Metric(
            "avg_cost_cny_per_run",
            round(avg_cost, 6) if avg_cost is not None else 0.0,
            "<= 0.01",
            avg_cost is None or avg_cost <= 0.01,
            f"{len(costs)} 次调用模型的运行",
        )
    )

    # 9) 降级率：走模板兜底占全部运行的比例（无 Key / 超时 / 预算拦截都算）
    fallback_flags = [r.sample.llm_fallback for r in results if r.sample.llm_fallback is not None]
    fallback_rate = (
        sum(1 for f in fallback_flags if f) / len(fallback_flags) if fallback_flags else None
    )
    metrics.append(
        Metric(
            "fallback_rate",
            round(fallback_rate, 4) if fallback_rate is not None else None,
            "<= 0.5",
            True if fallback_rate is None else fallback_rate <= 0.5,
            f"{sum(1 for f in fallback_flags if f)}/{len(fallback_flags)} 次运行降级"
            if fallback_flags
            else "本次未采样",
            sampled=fallback_rate is not None,
        )
    )

    # 10) p95 延迟（ms）
    p95 = _p95(latencies)
    metrics.append(
        Metric(
            "p95_latency_ms",
            round(p95, 2),
            "<= 2000",
            p95 <= 2000,
            f"{len(latencies)} 个场景",
        )
    )

    return metrics
