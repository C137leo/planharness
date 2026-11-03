"""把评测接进 pytest：任何一条指标退步都会让 CI 变红。

评测本身很轻（纯 SQLite + 无网络），8 个场景约 0.5 秒，放 CI 完全没负担。
"""

from __future__ import annotations

from app.evals.runner import run_all


def test_all_scenarios_pass() -> None:
    """8 个场景必须全绿。"""
    run = run_all()
    failed = [(r.id, r.title, [c.name for c in r.failed()], r.error) for r in run.failed_scenarios]
    assert not failed, f"失败场景：{failed}"


def test_metric_targets_are_met() -> None:
    """8 个指标必须达标（安全违规率=0、required 不丢、重复提交=0 等）。"""
    run = run_all()
    bad = [(m.name, m.value, m.target) for m in run.metrics if not m.ok]
    assert not bad, f"未达标指标：{bad}"


def test_report_payload_is_serializable() -> None:
    """报告能落盘（JSON 里不放任何不可序列化对象）。"""
    import json

    from app.evals.report import to_payload

    payload = to_payload(run_all())
    json.dumps(payload, ensure_ascii=False)
    assert payload["summary"]["total"] == 8
    assert {"safety_violation_rate", "duplicate_commit_rate"} <= {
        m["name"] for m in payload["metrics"]
    }
