"""变异测试接进 pytest：护栏被弄坏时，评测必须变红。

如果哪天某个变异体「存活」了，说明对应的场景断言失效了 —— 这比代码 bug 更危险，
因为它意味着你以为有保护、其实没有。
"""

from __future__ import annotations

from app.evals.mutations import mutation_score, run_mutants, survivors
from app.evals.runner import run_all


def test_baseline_is_green_before_mutating() -> None:
    """变异测试的前提：不改任何东西时全绿，否则「被杀」没有意义。"""
    run = run_all()
    assert all(r.passed for r in run.results), [
        r.id for r in run.failed_scenarios
    ]


def test_all_mutants_are_killed() -> None:
    """6 个变异体必须全部被杀 —— 证明这些指标不是装饰。"""
    results = run_mutants()
    alive = [(r.mutant.id, r.mutant.name) for r in survivors(results)]
    assert not alive, f"存活的变异体（评测盲区）：{alive}"
    assert mutation_score(results) == 1.0


def test_mutant_hits_the_expected_scenario() -> None:
    """每个变异体应该只（或至少）打中它声明的那个场景，而不是靠别的场景连带失败。"""
    for result in run_mutants():
        expected = set(result.mutant.expected_killers)
        assert expected & set(result.failed_scenarios), (
            f"{result.mutant.id} 的预期场景 {expected} 没红，实际红的是 {result.failed_scenarios}"
        )
