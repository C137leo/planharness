"""变异测试：故意弄坏护栏，看评测能不能抓住。

思路（对应 docs/04 §6 变异测试）：

1. 先确认基线（不改任何东西）8 个场景全绿；
2. 逐个注入「变异体」——把某个护栏替换成失效版本；
3. 跑受影响的场景，**必须至少有一个场景变红**；
4. 如果某个变异体活下来了（场景还是全绿），说明评测本身有盲区。

这就是「你为什么相信这些指标」的答案：不是我说它们有用，而是**弄坏护栏它们会红**。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

from app.evals.runner import EvalRun, run_all


@dataclass(frozen=True)
class Mutant:
    id: str
    name: str
    description: str
    # (模块路径, 属性名, 替换值)
    target_module: str
    target_attr: str
    replacement: Callable
    # 预期会被杀掉的场景
    expected_killers: tuple[str, ...]


@dataclass
class MutantResult:
    mutant: Mutant
    killed: bool
    failed_scenarios: list[str]
    checks_broken: list[str]
    run: EvalRun | None = None


# ------------------------------------------------------------------ 变异体


def _full_rewrite_diff(base, proposed, required_changes):
    """M1：churn 计算退化成「全量重写」—— A 模式（裸奔）的特征。"""
    total = len(base)
    return {
        "total_slots": total,
        "changed_slots": total,
        "churn": 1.0,
        "required_changes": required_changes,
        "unnecessary_churn": 1.0,
        "unchanged_ratio": 0.0,
    }


def _reject_as_approve(db, user, patch_id, comment=""):
    """M2：驳回逻辑被写成通过 —— 用户说「不同意」，计划却变了。"""
    from app.domain.patches import approve_patch

    _, version = approve_patch(db, user, patch_id, comment)
    return version


def _always_pass_verify(db, *, plan_id, patch_id, expected_version):
    """M3：验证器恒通过 —— 提交后不再核对事实。"""
    from app.agent.verify import Check, VerificationResult

    return VerificationResult(passed=True, checks=[Check("always_pass", True, "mutant")])


def _ignore_budget(db, user, plan, version, payload, reason_text, available_days, token_budget):
    """M4：预算配置被忽略 —— 上下文装配永远用超大预算。"""
    import app.domain.patches as patches_module
    from app.llm.context import ContextBuilder

    builder = ContextBuilder(budget=10**6)
    builder.add_section("system_policy", "你是教练。", priority=100, order=10, required=True)
    builder.add_section("constraints", reason_text, priority=90, order=20, required=True)
    return builder.build()


def _empty_fallback(operations, diff):
    """M5：降级文案为空 —— 模型挂了用户就什么都看不到。"""
    return ""


def _no_safety_check(proposed, available_days):
    """M6：安全门被关掉 —— 禁忌动作、超天数全部放行。"""
    return []


MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        "M1",
        "退化成全量重生成",
        "把 _diff 改成「几乎全改」，等价于 A 模式（裸奔）的 churn",
        "app.domain.patches",
        "_diff",
        _full_rewrite_diff,
        ("E02",),
    ),
    Mutant(
        "M2",
        "驳回落成通过",
        "reject_patch 实际调用 approve_patch，用户不同意也提交",
        "app.agent.patch_graph",
        "reject_patch",
        _reject_as_approve,
        ("E03",),
    ),
    Mutant(
        "M3",
        "验证器恒通过",
        "verify_commit 永远返回 passed=True，提交后不再核对事实",
        "app.agent.patch_graph",
        "verify_commit",
        _always_pass_verify,
        ("E07",),
    ),
    Mutant(
        "M4",
        "预算配置被忽略",
        "上下文装配永远用超大预算，required 超限也不会告警",
        "app.domain.patches",
        "_build_context",
        _ignore_budget,
        ("E05",),
    ),
    Mutant(
        "M5",
        "降级文案为空",
        "fallback_explanation 返回空串，模型不可用时用户看不到任何解释",
        "app.domain.patches",
        "fallback_explanation",
        _empty_fallback,
        ("E05", "E06"),
    ),
    Mutant(
        "M6",
        "安全门被关闭",
        "_safety_check 恒返回空，禁忌动作与超天数全部放行",
        "app.domain.patches",
        "_safety_check",
        _no_safety_check,
        ("E02",),
    ),
)


# ------------------------------------------------------------------ 执行


@contextmanager
def _apply(mutant: Mutant):
    import importlib

    module = importlib.import_module(mutant.target_module)
    old = getattr(module, mutant.target_attr)
    setattr(module, mutant.target_attr, mutant.replacement)
    try:
        yield
    finally:
        setattr(module, mutant.target_attr, old)


def run_mutants() -> list[MutantResult]:
    """跑所有变异体。调用方应先确认基线全绿。"""
    results: list[MutantResult] = []
    for mutant in MUTANTS:
        with _apply(mutant):
            run = run_all(only=list(mutant.expected_killers))

        failed = [r.id for r in run.failed_scenarios]
        broken = [
            f"{r.id}:{c.name}"
            for r in run.failed_scenarios
            for c in r.failed()
        ]
        killed = bool(failed)
        results.append(
            MutantResult(
                mutant=mutant,
                killed=killed,
                failed_scenarios=failed,
                checks_broken=broken,
                run=run,
            )
        )
    return results


def mutation_score(results: list[MutantResult]) -> float:
    if not results:
        return 0.0
    return sum(1 for r in results if r.killed) / len(results)


def survivors(results: list[MutantResult]) -> list[MutantResult]:
    return [r for r in results if not r.killed]
