"""热量估算 + 训练记录接口测试。

口径是 `MET × 3.5 × 体重 / 200 × 时长`，并且只算消耗。
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from app.domain.calories import DEFAULT_MET, estimate_kcal, met_for, summarize

API = "/api/v1"


def _log(client: TestClient, headers: dict[str, str], **payload) -> dict:
    body = {"action_ref": "brisk_walk", "kind": "cardio", "duration_min": 30, **payload}
    resp = client.post(f"{API}/workout-logs", headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_formula_matches_reference() -> None:
    """78kg 快走 30 分钟：4.3 × 3.5 × 78 / 200 × 30 = 176.1 kcal。"""
    assert estimate_kcal(4.3, 78, 30) == 176.1
    assert estimate_kcal(4.0, 70, 0) == 0.0


def test_unknown_move_is_flagged() -> None:
    """没收录的动作要给兜底 MET，并标记出来，不能假装精确。"""
    met, fallback = met_for("dragon_flag_press")
    assert met == DEFAULT_MET
    assert fallback is True
    assert met_for("brisk_walk")[1] is False


def test_workout_log_then_calorie_stats(client, auth_headers) -> None:
    """打卡一条训练后，/stats/calories 要能按天汇总并对上公式。"""
    logged = _log(client, auth_headers, action_ref="brisk_walk", duration_min=30)
    assert logged["kcal"] > 0
    assert logged["weight_source"] in {"checkin", "profile", "default"}

    stats = client.get(f"{API}/stats/calories?days=7", headers=auth_headers).json()["data"]
    assert stats["scope"] == "burn_only", "口径必须显式声明：只算消耗"
    assert stats["sessions"] == 1
    assert stats["total_kcal"] == logged["kcal"]
    assert stats["days"][0]["date"] == date.today().isoformat()
    assert stats["days"][0]["kcal"] == logged["kcal"]
    assert "MET" in stats["formula"]


def test_delete_workout_log(client, auth_headers) -> None:
    """记错了能删；删完统计归零。"""
    logged = _log(client, auth_headers, action_ref="pushup", kind="strength", duration_min=12)
    assert client.delete(f"{API}/workout-logs/{logged['log_id']}", headers=auth_headers).status_code == 200
    stats = client.get(f"{API}/stats/calories?days=7", headers=auth_headers).json()["data"]
    assert stats["sessions"] == 0
    assert stats["total_kcal"] == 0


def test_summarize_groups_by_day_and_kind() -> None:
    """纯函数：跨天、跨类型的分组要稳定（不依赖数据库）。"""
    rows = [
        {"date": "2026-01-01", "kind": "cardio", "duration_min": 30, "kcal": 100.0},
        {"date": "2026-01-01", "kind": "strength", "duration_min": 12, "kcal": 50.0},
        {"date": "2026-01-02", "kind": "cardio", "duration_min": 20, "kcal": 70.0},
    ]
    data = summarize(rows, 78.0)
    assert data["total_kcal"] == 220.0
    assert [d["date"] for d in data["days"]] == ["2026-01-01", "2026-01-02"]
    assert data["by_kind"] == {"cardio": 170.0, "strength": 50.0}
    assert data["sessions"] == 3
