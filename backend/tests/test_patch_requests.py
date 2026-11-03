"""一句话改计划（patch-requests）的接口测试。

覆盖：草案不写版本 / 追问 / 审批提交 / 撤销 / 轮次上限 / 拒绝 / 乐观锁。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

API = "/api/v1"


def _current(client: TestClient, headers: dict[str, str]) -> dict:
    resp = client.get(f"{API}/plans/current", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _ask(client: TestClient, headers: dict[str, str], text: str, **extra) -> dict:
    plan = _current(client, headers)
    resp = client.post(
        f"{API}/plans/{plan['plan_id']}/patch-requests",
        headers=headers,
        json={"text": text, **extra},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_rest_draft_does_not_touch_plan(client, auth_headers) -> None:
    """「今天想休息」只产草案，版本号必须原样不动。"""
    before = _current(client, auth_headers)
    data = _ask(client, auth_headers, "今天想休息")
    assert data["status"] == "ready"
    assert data["intent"]["kind"] == "rest"
    draft = data["draft"]
    assert draft["operations"], "休息至少要换掉一个训练日"
    assert draft["summary"]
    after = _current(client, auth_headers)
    assert after["version"] == before["version"], "草案阶段不许写新版本"
    assert after["actions"] == before["actions"]


def test_injury_swap_respects_day_limit(client, auth_headers) -> None:
    """「膝盖疼，这周只能练两天」既要避开膝盖，也要把训练日压到 2 天。"""
    data = _ask(client, auth_headers, "膝盖疼，这周只能练两天")
    assert data["status"] == "ready"
    ops = data["draft"]["operations"]
    assert ops, "必须给出操作"
    removed = [o for o in ops if o.get("op") == "remove_action"]
    assert removed, "4 天 → 2 天必须删掉两个训练日"
    summary = data["draft"]["summary"]
    assert "膝盖" in summary or "膝" in summary


def test_ambiguous_injury_asks_question(client, auth_headers) -> None:
    """只说「受伤了」不够，要追问部位，且不产草案。"""
    plan = _current(client, auth_headers)
    resp = client.post(
        f"{API}/plans/{plan['plan_id']}/patch-requests",
        headers=auth_headers,
        json={"text": "受伤了"},
    )
    data = resp.json()["data"]
    assert data["status"] == "clarifying"
    assert data["question"], "追问必须带问题"
    assert data["draft"] is None


def test_approve_commits_new_version(client, auth_headers) -> None:
    """approve 之后：版本 +1、计划内容变化、审计有 patch_committed。"""
    before = _current(client, auth_headers)
    data = _ask(client, auth_headers, "膝盖疼，换上肢")
    resp = client.post(
        f"{API}/patch-requests/{data['request_id']}/approve",
        headers=auth_headers,
        json={"comment": "测试通过"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["committed_version"] == before["version"] + 1
    assert body["plan"]["actions"] != before["actions"], "提交后动作必须变"

    before = _current(client, auth_headers)
    audit = client.get(f"{API}/audit/{before['plan_id']}", headers=auth_headers).json()["data"]
    actions = {e["action"] for e in audit}
    assert {"patch_created", "approval_granted", "patch_committed"} <= actions


def test_undo_restores_parent_version(client, auth_headers) -> None:
    """撤销：内容回到父版本，但版本号继续往上走（append-only）。"""
    original = _current(client, auth_headers)
    data = _ask(client, auth_headers, "膝盖疼，换上肢")
    client.post(
        f"{API}/patch-requests/{data['request_id']}/approve",
        headers=auth_headers,
        json={"comment": "先应用"},
    )
    resp = client.post(f"{API}/plans/{original['plan_id']}/undo", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["new_version"] == original["version"] + 2, "撤销也要产生新版本"
    # 内容必须回到父版本；行 id 会变（append-only，每次提交都写新行）
    def shape(actions):
        return sorted(
            (a["day_index"], a["slot"], a["seq"], a["action_type"], a["action_ref"])
            for a in actions
        )

    assert shape(body["plan"]["actions"]) == shape(original["actions"]), "内容必须回到父版本"


def test_feedback_rounds_are_capped(client, auth_headers) -> None:
    """最多改 3 轮，第 4 轮直接 422，避免无限拉扯。"""
    request_id = _ask(client, auth_headers, "膝盖疼，换上肢")["request_id"]
    for i in range(3):
        resp = client.post(
            f"{API}/patch-requests/{request_id}/feedback",
            headers=auth_headers,
            json={"text": f"再保守一点 {i}"},
        )
        assert resp.status_code == 200, resp.text
        revised = resp.json()["data"]
        assert revised["round"] == i + 2
        request_id = revised["request_id"]
    resp = client.post(
        f"{API}/patch-requests/{request_id}/feedback",
        headers=auth_headers,
        json={"text": "再改一次"},
    )
    assert resp.status_code == 422, "第 4 轮必须被拒"


def test_reject_keeps_plan(client, auth_headers) -> None:
    """拒绝草案：状态变 rejected，计划不动。"""
    before = _current(client, auth_headers)
    data = _ask(client, auth_headers, "膝盖疼，换上肢")
    resp = client.post(
        f"{API}/patch-requests/{data['request_id']}/reject",
        headers=auth_headers,
        json={"comment": "不要这个"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "rejected"
    assert _current(client, auth_headers)["version"] == before["version"]


def test_stale_base_version_is_rejected(client, auth_headers) -> None:
    """拿着旧版本号提交要 422（乐观锁），不能覆盖别人的改动。"""
    plan = _current(client, auth_headers)
    resp = client.post(
        f"{API}/plans/{plan['plan_id']}/patch-requests",
        headers=auth_headers,
        json={"text": "今天想休息", "base_version": plan["version"] + 5},
    )
    assert resp.status_code == 422, resp.text


def test_revision_supersedes_previous_draft(client, auth_headers) -> None:
    """「再改改」之后，上一版草案必须作废，否则待确认列表里堆着过期草案。"""
    first = _ask(client, auth_headers, "膝盖疼，换上肢")
    resp = client.post(
        f"{API}/patch-requests/{first['request_id']}/feedback",
        headers=auth_headers,
        json={"text": "再保守一点，别用跳的动作"},
    )
    assert resp.status_code == 200, resp.text
    revised = resp.json()["data"]
    assert revised["round"] == 2

    # 旧草案作废后不能再提交；只能提交新一轮这版
    stale = client.post(
        f"{API}/patch-requests/{first['request_id']}/approve",
        headers=auth_headers,
        json={"comment": "旧草案"},
    )
    assert stale.status_code == 422, "被取代的草案不能再提交"


def test_rest_and_time_intents_are_supported(client, auth_headers) -> None:
    """用户能自由说：休息、只能练两天。两者都要能出草案，而不是追问。"""
    rest = _ask(client, auth_headers, "今天想休息")
    assert rest["status"] == "ready"
    assert rest["draft"]["operations"], "休息至少要动一个槽位"

    limited = _ask(client, auth_headers, "这周只能练两天，每次 30 分钟")
    assert limited["status"] == "ready"
    assert limited["intent"]["kind"] == "time"
    assert any(op["op"] == "remove_action" for op in limited["draft"]["operations"])


def test_injury_stop_covers_all_injured_slots(client, auth_headers) -> None:
    """「停止腿部训练」不能只删一个动作：凡是涉及受伤部位的都要被处理完。"""
    data = _ask(client, auth_headers, "膝盖受伤了，先停止所有腿部训练")
    assert data["status"] == "ready"
    ops = data["draft"]["operations"]
    assert ops

    plan = _current(client, auth_headers)
    removed = {op["slot_key"] for op in ops if op["op"] == "remove_action"}
    risky = {"squat", "bodyweight_squat", "lunge", "jump_squat", "run"}
    for action in plan["actions"]:
        if action["action_type"] != "exercise" or action["action_ref"] not in risky:
            continue
        assert action["slot_key"] in removed, f"{action['slot_key']} 仍涉及膝主导动作但没被删"
