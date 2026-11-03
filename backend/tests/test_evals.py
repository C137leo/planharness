"""把评测接进 pytest：任何一条指标退步都会让 CI 变红。

评测本身很轻（纯 SQLite + 无网络），11 个场景约 3 秒，放 CI 完全没负担。

E09–E11 需要本机装有健康健身 skill；没有时只跑核心 8 条，所以这里按“至少 8 条”断言。
"""

from __future__ import annotations

from app.evals.runner import run_all


def test_all_scenarios_pass() -> None:
    """核心 8 条必须全绿；有 skill 时再加上教练链路的 3 条。"""
    run = run_all()
    failed = [(r.id, r.title, [c.name for c in r.failed()], r.error) for r in run.failed_scenarios]
    assert not failed, f"失败场景：{failed}"


def test_metric_targets_are_met() -> None:
    """10 个指标必须达标（安全违规率=0、required 不丢、重复提交=0 等）。"""
    run = run_all()
    bad = [(m.name, m.value, m.target) for m in run.metrics if not m.ok]
    assert not bad, f"未达标指标：{bad}"


def test_report_payload_is_serializable() -> None:
    """报告能落盘（JSON 里不放任何不可序列化对象）。"""
    import json

    from app.evals.report import to_payload

    run = run_all()
    payload = to_payload(run)
    json.dumps(payload, ensure_ascii=False)
    assert payload["summary"]["total"] >= 8
    assert payload["summary"]["total"] == len(run.results)
    assert {"safety_violation_rate", "duplicate_commit_rate"} <= {
        m["name"] for m in payload["metrics"]
    }
