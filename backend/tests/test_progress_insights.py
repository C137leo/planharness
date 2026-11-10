"""进展页教练发现 + 临时记忆展示 + 页面上下文入参。"""

from __future__ import annotations

from fastapi.testclient import TestClient

API = "/api/v1"


def _headers(client: TestClient) -> dict[str, str]:
    token = client.post(f"{API}/auth/demo", json={}).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _finish_incomplete(client, headers, pct=25):
    started = client.post(f"{API}/workout-sessions", headers=headers, json={})
    session_id = started.json()["data"]["session"]["session_id"]
    client.post(
        f"{API}/workout-sessions/{session_id}/finish", headers=headers,
        json={"completed": False, "completion_pct": pct, "difficulty": 4, "fatigue": 4},
    )


def test_progress_insights_use_real_sessions(client: TestClient) -> None:
    headers = _headers(client)
    for _ in range(2):
        _finish_incomplete(client, headers)
    data = client.get(f"{API}/agent/progress-insights?days=14", headers=headers)
    assert data.status_code == 200, data.text
    body = data.json()["data"]
    assert body["session_count"] == 2
    assert body["weekly"]["consecutive_incomplete"] >= 2
    findings = body["findings"]
    assert any(item["category"] == "missed" for item in findings)
    assert any(item["suggested_prompt"] for item in findings)
    assert body["trend"]


def test_progress_insights_empty_state(client: TestClient) -> None:
    headers = _headers(client)
    body = client.get(f"{API}/agent/progress-insights", headers=headers).json()["data"]
    assert body["session_count"] == 0
    assert body["findings"][0]["category"] == "empty"


def test_memories_expose_today_temporary_context(client: TestClient) -> None:
    headers = _headers(client)
    client.post(f"{API}/agent/chat", headers=headers, json={
        "text": "今天晚上有空。", "page": "profile",
    })
    body = client.get(f"{API}/agent/memories", headers=headers).json()["data"]
    temporary = body["temporary"]
    assert any(item["key"] == "time_window" for item in temporary)
    item = next(item for item in temporary if item["key"] == "time_window")
    assert item["value"]["window"] == "19:00-20:00"
    assert item["status"] == "active"


def test_chat_accepts_page_context(client: TestClient) -> None:
    headers = _headers(client)
    resp = client.post(f"{API}/agent/chat", headers=headers, json={
        "text": "根据我的进展，下一周怎么练？",
        "page": "progress",
        "context": {
            "weeklyCompletion": 62,
            "weeklyMinutes": 80,
            "recentFindings": ["最近两次未完成"],
        },
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["page_context"]["weeklyCompletion"] == 62
    assert data["run_id"]


def test_progress_review_answers_from_real_data(client: TestClient) -> None:
    headers = _headers(client)
    _finish_incomplete(client, headers)
    resp = client.post(f"{API}/agent/chat", headers=headers, json={
        "text": "解释一下我最近的训练趋势",
        "page": "progress",
        "context": {"weeklyCompletion": 25},
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["intent"] == "progress_review"
    assert data["progress_insights"]["session_count"] == 1
    assert "完成" in data["reply"] or "训练" in data["reply"]


def test_progress_finding_cta_creates_plan_not_feedback(client: TestClient) -> None:
    headers = _headers(client)
    _finish_incomplete(client, headers)
    resp = client.post(f"{API}/agent/chat", headers=headers, json={
        "text": "最近连续几次没完成训练，帮我调整一下下一周训练量",
        "page": "progress",
        "context": {"weeklyCompletion": 25},
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["intent"] == "create_plan", data
    assert data["plan_draft"] is not None
