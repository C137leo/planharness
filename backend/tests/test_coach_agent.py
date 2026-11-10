"""统一教练 Agent 的验收场景。

覆盖产品要求的 9 个场景：建计划、临时缩短、长期偏好、连续未完成、
训练中器械被占用、不会动作、完成反馈、记忆管理、跨页面统一入口。
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

API = "/api/v1"


def _headers(client: TestClient) -> dict[str, str]:
    token = client.post(f"{API}/auth/demo", json={}).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _setup_plan(client: TestClient, headers: dict[str, str]) -> None:
    """生成并确认一份真实计划（走 skill + 安全门 + 草案确认）。"""
    client.put(f"{API}/me/profile", headers=headers, json={
        "goal": "fat_loss", "sex": "male", "birth_year": 1997, "height_cm": 178,
        "weight_kg": 78, "weekly_days": 3, "session_minutes": 30,
        "equipment": ["dumbbell"], "allergies": [], "diet_preferences": [],
    })
    draft = client.post(f"{API}/plans/drafts", headers=headers,
                        json={"instruction": "减脂，每周 3 天，每次 30 分钟"}).json()["data"]
    if draft["status"] == "ready":
        client.post(f"{API}/plans/drafts/{draft['draft_id']}/confirm",
                    headers=headers, json={"comment": "test"})


def _chat(client: TestClient, headers: dict[str, str], text: str,
          page: str = "today", session_id: str = "") -> dict:
    resp = client.post(f"{API}/agent/chat", headers=headers, json={
        "text": text, "page": page, "session_id": session_id or None,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0, body
    return body["data"]


def _complete_item(client: TestClient, headers: dict[str, str], session_id: str,
                   item_name: str) -> dict:
    """把当前动作的所有组点完。"""
    for _ in range(10):
        session = client.get(f"{API}/workout-sessions/{session_id}",
                             headers=headers).json()["data"]
        current = session.get("current_item")
        if not current or current.get("name") != item_name:
            return session
        resp = client.post(f"{API}/workout-sessions/{session_id}/sets", headers=headers)
        assert resp.status_code == 200, resp.text
        session = resp.json()["data"]["session"]
    return session


def test_1_create_plan_from_chat(client: TestClient) -> None:
    headers = _headers(client)
    old_version = client.get(f"{API}/plans/current", headers=headers).json()["data"]["version"]

    data = _chat(client, headers, "我想减脂，每周训练三次，每次 30 分钟。")
    assert data["intent"] == "create_plan"
    assert data["plan_draft"] and data["plan_draft"]["status"] == "ready"
    assert len(data["plan_draft"]["days"]) == 3
    # 目标与天数先写入档案（长期信息），记忆里也有
    profile = client.get(f"{API}/me/profile", headers=headers).json()["data"]
    assert profile["goal"] == "fat_loss"
    assert profile["weekly_days"] == 3
    assert profile["session_minutes"] == 30
    memories = client.get(f"{API}/agent/memories", headers=headers).json()["data"]["items"]
    categories = {item["category"] for item in memories}
    assert {"goal", "weekly_days", "session_minutes"} <= categories

    draft_id = data["plan_draft"]["draft_id"]
    confirmed = client.post(f"{API}/plans/drafts/{draft_id}/confirm",
                            headers=headers, json={"comment": "确认"})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["data"]["committed_version"] == old_version + 1


def test_2_temporary_minutes_do_not_change_long_term(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)
    before_plan = client.get(f"{API}/plans/current", headers=headers).json()["data"]
    before_profile = client.get(f"{API}/me/profile", headers=headers).json()["data"]

    data = _chat(client, headers, "今天加班，只有 20 分钟，不去健身房了。")
    assert data["intent"] == "adjust_today"
    assert data["day"]["source"] == "agent"
    assert data["day"]["actions"], "必须给出今天可执行的替代安排"
    assert data["day"]["total_minutes"] <= 25
    assert all(item["equipment"] == "bodyweight" or item["equipment"] == "dumbbell"
               for item in data["day"]["actions"])

    after_plan = client.get(f"{API}/plans/current", headers=headers).json()["data"]
    after_profile = client.get(f"{API}/me/profile", headers=headers).json()["data"]
    assert after_plan["version"] == before_plan["version"], "临时调整不能改计划版本"
    assert after_profile["session_minutes"] == before_profile["session_minutes"]
    assert after_profile["weekly_days"] == before_profile["weekly_days"]

    # 长期记忆里不能出现"20 分钟"
    memories = client.get(f"{API}/agent/memories", headers=headers).json()["data"]["items"]
    long_term = [item for item in memories if item["kind"] == "long_term"]
    assert all("20" not in item["value"] for item in long_term)


def test_3_long_term_lunch_preference_reaches_next_plan(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)

    data = _chat(client, headers, "以后尽量把训练安排在午休。")
    assert data["intent"] == "update_preference"
    memories = client.get(f"{API}/agent/memories", headers=headers).json()["data"]["items"]
    schedule = [item for item in memories
                if item["category"] == "schedule" and item["kind"] == "long_term"]
    assert schedule and schedule[0]["structured"]["window"] == "12:00-13:00"

    draft = _chat(client, headers, "根据我的长期偏好重新生成计划。")
    assert draft["intent"] == "create_plan"
    memory_meta = draft["plan_draft"]["meta"]["memory"]
    assert any("12:00-13:00" in str(item) for item in memory_meta["preferred_windows"])


def test_4_two_incomplete_sessions_reduce_next_load(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)

    for _ in range(2):
        started = client.post(f"{API}/workout-sessions", headers=headers, json={})
        assert started.status_code == 200, started.text
        session_id = started.json()["data"]["session"]["session_id"]
        finished = client.post(
            f"{API}/workout-sessions/{session_id}/finish",
            headers=headers,
            json={"completed": False, "completion_pct": 30,
                  "difficulty": 4, "fatigue": 4, "note": "太累了"},
        )
        assert finished.status_code == 200, finished.text

    memories = client.get(f"{API}/agent/memories", headers=headers).json()["data"]["items"]
    state = [item for item in memories if item["category"] == "training_state"]
    assert state and state[0]["value"] == "2"

    data = _chat(client, headers, "根据我最近的完成情况重新生成计划。")
    assert data["intent"] == "create_plan"
    draft = data["plan_draft"]
    assert draft["adaptive"]["load_reduced"] is True
    assert draft["adaptive"]["consecutive_incomplete"] == 2
    # 训练量必须真的降下来，而不只是文案里说降了
    for day in draft["days"]:
        for action in day["actions"]:
            if action.get("sets"):
                assert action["sets"] <= 2


def test_5_equipment_busy_keeps_completed_and_replaces_only_current(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)
    _chat(client, headers, "今天只有 20 分钟，不去健身房了。")

    started = client.post(f"{API}/workout-sessions", headers=headers, json={}).json()["data"]
    session = started["session"]
    session_id = session["session_id"]
    items = session["items"]
    assert len(items) >= 3, items

    first, second = items[0], items[1]
    _complete_item(client, headers, session_id, first["name"])
    session = _complete_item(client, headers, session_id, second["name"])
    assert session["items"][0]["status"] == "done"
    assert session["items"][1]["status"] == "done"

    data = _chat(client, headers, "这台器械有人用。", page="workout",
                 session_id=session_id)
    assert data["intent"] == "adjust_workout"
    updated = data["session"]
    # 前两个动作的完成记录必须原样保留
    assert updated["items"][0]["completed_sets"] == session["items"][0]["completed_sets"]
    assert updated["items"][0]["status"] == "done"
    assert updated["items"][1]["completed_sets"] == session["items"][1]["completed_sets"]
    assert updated["items"][1]["status"] == "done"
    # 当前动作被替换成不需要器械的版本
    current = updated["current_item"]
    assert current is not None
    assert current["equipment"] == "bodyweight"
    assert current["name"] not in (first["name"], second["name"])


def test_6_unknown_exercise_updates_current_task(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)
    started = client.post(f"{API}/workout-sessions", headers=headers, json={}).json()["data"]
    session_id = started["session"]["session_id"]
    current_name = started["session"]["current_item"]["name"]

    data = _chat(client, headers, "这个动作我不会。", page="workout",
                 session_id=session_id)
    assert data["intent"] == "exercise_guidance"
    assert data["guidance"] and data["guidance"][0]["cues"]
    assert data["session"]["current_item"]["guidance"], "指导必须写进当前训练任务"
    assert data["guidance"][0]["name"] == current_name


def test_7_finish_feedback_writes_real_records(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)

    data = _chat(client, headers, "今天有点累，但还是完成了。")
    assert data["intent"] == "log_feedback"
    assert data["session"]["status"] == "finished"
    assert data["feedback"]["completion_pct"] == 100
    assert data["feedback"]["difficulty"] == 4

    logs = client.get(f"{API}/workout-logs?days=7", headers=headers).json()["data"]["items"]
    assert logs, "完成训练后必须有真实 WorkoutLog，不能只回一句文字"
    assert all(item["duration_min"] >= 1 for item in logs)


def test_8_memory_management_edit_and_delete(client: TestClient) -> None:
    headers = _headers(client)
    _chat(client, headers, "以后尽量把训练安排在午休。")
    items = client.get(f"{API}/agent/memories", headers=headers).json()["data"]["items"]
    memory = next(item for item in items if item["category"] == "schedule")

    updated = client.put(f"{API}/agent/memories/{memory['id']}", headers=headers,
                         json={"value": "晚上 20:00 训练"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["value"] == "晚上 20:00 训练"

    deleted = client.delete(f"{API}/agent/memories/{memory['id']}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    remaining = client.get(f"{API}/agent/memories", headers=headers).json()["data"]["items"]
    assert all(item["id"] != memory["id"] for item in remaining)

    # 也支持自然语言删除："忘掉午休那条"
    _chat(client, headers, "以后尽量把训练安排在午休。")
    deleted_by_chat = _chat(client, headers, "忘掉午休那条。")
    assert deleted_by_chat["intent"] == "edit_memory"
    assert deleted_by_chat["changes"]
    remaining = client.get(f"{API}/agent/memories", headers=headers).json()["data"]["items"]
    assert all(item["category"] != "schedule" for item in remaining)


def test_9_same_agent_entry_across_pages(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)

    for page in ("today", "plan", "profile"):
        data = _chat(client, headers, "以后尽量把训练安排在午休。", page=page)
        assert data["intent"] == "update_preference"
        assert data["run_id"]
        assert data["scope"] == "long_term"

    today = _chat(client, headers, "今天只有 20 分钟。", page="today")
    plan_page = _chat(client, headers, "今天只有 20 分钟，不去健身房了。", page="plan")
    assert today["intent"] == "adjust_today"
    assert plan_page["intent"] == "adjust_today"
    # 两个入口改的是同一天的同一个 override，而不是各自一套逻辑
    assert plan_page["day"]["source"] == "agent"
    assert plan_page["day"]["date"] == date.today().isoformat()


def test_10_plan_level_change_goes_through_patch_approval(client: TestClient) -> None:
    """「这周只能练两天」是计划结构变更：必须走草案 + 确认，不能只改今天。"""
    headers = _headers(client)
    _setup_plan(client, headers)
    before = client.get(f"{API}/plans/current", headers=headers).json()["data"]

    data = _chat(client, headers, "这周只能练两天，每次 30 分钟", page="plan")
    assert data["intent"] == "adjust_plan"
    assert data["patch_request"], data
    request = data["patch_request"]
    assert request["status"] == "ready", request
    assert request["draft"]["operations"], "必须给出具体要改哪几处"
    # 确认前计划版本不变
    assert client.get(f"{API}/plans/current", headers=headers).json()["data"]["version"] == before["version"]

    approved = client.post(f"{API}/patch-requests/{request['request_id']}/approve",
                           headers=headers, json={"comment": "确认"})
    assert approved.status_code == 200, approved.text
    after = client.get(f"{API}/plans/current", headers=headers).json()["data"]
    assert after["version"] == before["version"] + 1


def test_11_in_workout_fatigue_adjusts_current_session_not_whole_day(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)
    started = client.post(f"{API}/workout-sessions", headers=headers, json={}).json()["data"]
    session_id = started["session"]["session_id"]
    total_before = len(started["session"]["items"])

    data = _chat(client, headers, "我有点累，能不能简单一点？", page="workout",
                 session_id=session_id)
    assert data["intent"] == "adjust_workout"
    assert data["session"]["session_id"] == session_id
    # 训练中不要把"今天"重排成另一个 override；只动当前会话
    assert data["day"]["source"] == "plan"
    assert len(data["session"]["items"]) == total_before


def test_12_long_term_lunch_also_changes_today_schedule(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)
    _chat(client, headers, "以后尽量把训练安排在午休。")
    data = _chat(client, headers, "帮我规划今天", page="today")
    assert data["intent"] == "today_brief"
    schedule = data["today_brief"]["schedule"]
    assert schedule and schedule[0]["time"] == "12:00-13:00"


def test_13_long_term_dislike_changes_next_plan_content(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)
    chat = _chat(client, headers, "我不喜欢深蹲，以后别安排深蹲。")
    assert chat["intent"] == "update_preference"
    memories = client.get(f"{API}/agent/memories", headers=headers).json()["data"]["items"]
    assert any(item["category"] == "dislike" and item["key"] == "深蹲" for item in memories)

    data = _chat(client, headers, "根据我的长期偏好重新生成计划。")
    assert data["plan_draft"]["status"] == "ready"
    refs = [action["action_ref"] for day in data["plan_draft"]["days"] for action in day["actions"]]
    names = [str((action.get("meta") or {}).get("name_zh") or "") for day in data["plan_draft"]["days"] for action in day["actions"]]
    assert all("squat" not in ref.lower() for ref in refs), refs
    assert all("深蹲" not in name for name in names), names


def test_natural_language_next_week_adjust_is_plan() -> None:
    from app.agent.coach_agent import classify

    decision = classify(
        "根据我最近的训练和体重变化，帮我调整下一周计划",
        has_plan=True, in_workout=False, page="today",
    )
    assert decision.intent == "create_plan"
    assert decision.scope == "long_term"


def test_natural_language_today_plan_is_today_brief() -> None:
    from app.agent.coach_agent import classify

    decision = classify(
        "生成今天的训练计划",
        has_plan=True, in_workout=False, page="today",
    )
    assert decision.intent == "today_brief"
    assert decision.scope == "today"


def test_scope_reply_today_does_not_repeat_layer_question(client: TestClient) -> None:
    headers = _headers(client)
    data = _chat(client, headers, "先改今天")
    assert data["intent"] == "adjust_today"
    assert "只改今天" in data["reply"]
    assert "再确认一下你想改哪一层" not in data["reply"]


def test_conversations_are_listed_and_messages_are_isolated(client: TestClient) -> None:
    headers = _headers(client)
    first = client.post(f"{API}/agent/chat", headers=headers, json={
        "text": "你记得我什么？", "page": "today", "thread": "conv-a",
    })
    second = client.post(f"{API}/agent/chat", headers=headers, json={
        "text": "今天只有 20 分钟。", "page": "today", "thread": "conv-b",
    })
    assert first.status_code == 200 and second.status_code == 200

    conversations = client.get(f"{API}/agent/conversations", headers=headers)
    assert conversations.status_code == 200
    threads = {item["thread"] for item in conversations.json()["data"]}
    assert {"conv-a", "conv-b"} <= threads

    messages_a = client.get(
        f"{API}/agent/messages?thread=conv-a", headers=headers
    ).json()["data"]
    messages_b = client.get(
        f"{API}/agent/messages?thread=conv-b", headers=headers
    ).json()["data"]
    assert [item["role"] for item in messages_a] == ["user", "agent"]
    assert all("记得我什么" in item["text"] or item["role"] == "agent" for item in messages_a)
    assert len(messages_b) == 2 and all("记得我什么" not in item["text"] for item in messages_b)


def test_mcd_order_natural_language_opens_inline_order(client: TestClient) -> None:
    headers = _headers(client)
    data = _chat(
        client, headers,
        "我的麦当劳MCP Token 已保存，我在北京的国贸，帮我点餐",
    )
    assert data["intent"] == "mcd_order"
    assert data["mcd_order"]["instruction"]
    assert "北京的国贸" in data["mcd_order"]["instruction"]


class _FakeIntentClient:
    def available(self) -> bool:
        return True

    def complete(self, *args, **kwargs):
        return {"message": {"content": (
            '{"intent":"mcd_order","scope":"meal","reason":"语义解析",'
            '"city":"北京","store":"三里屯","foods":["炸鸡"]}'
        )}}


def test_model_semantic_classifier_extracts_order_entities() -> None:
    from app.agent.coach_agent import classify_with_model

    decision = classify_with_model(
        _FakeIntentClient(),
        "我想吃炸鸡,我在三里屯北京,帮我点餐",
        has_plan=True, in_workout=False, page="today",
    )
    assert decision is not None
    assert decision.intent == "mcd_order"
    assert decision.constraints["mcd_city"] == "北京"
    assert decision.constraints["mcd_store"] == "三里屯"
    assert decision.constraints["mcd_foods"] == ["炸鸡"]


def test_conversation_can_be_deleted(client: TestClient) -> None:
    headers = _headers(client)
    client.post(f"{API}/agent/chat", headers=headers, json={
        "text": "你记得我什么？", "page": "today", "thread": "conv-delete",
    })
    deleted = client.delete(f"{API}/agent/conversations/conv-delete", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] == 2
    messages = client.get(
        f"{API}/agent/messages?thread=conv-delete", headers=headers
    ).json()["data"]
    assert messages == []


def test_6b_teach_requested_exercise_outside_current_session(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)
    started = client.post(f"{API}/workout-sessions", headers=headers, json={}).json()["data"]
    session = started["session"]
    session_id = session["session_id"]
    current = session["current_item"]
    current_name = current["name"]

    target = "杠铃卧推" if current_name != "杠铃卧推" else "腿举"
    data = _chat(client, headers, f"我想让你教我{target}。", page="workout",
                 session_id=session_id)
    assert data["intent"] == "exercise_guidance"
    assert data["guidance"], data
    assert data["guidance"][0]["name"] == target
    assert target in data["reply"]
    cues = " ".join(data["guidance"][0]["cues"])
    assert "腿举" not in cues
    # 用户问的是另一个动作：不能把它的指导挂到当前训练动作上
    assert data["session"] is None
    latest = client.get(f"{API}/workout-sessions/{session_id}",
                        headers=headers).json()["data"]
    current_row = latest["current_item"]
    assert not (current_row.get("guidance") or "").strip()


def test_6c_teach_arbitrary_exercise_without_active_session(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)

    data = _chat(client, headers, "我想让你教我卧推。", page="today")
    assert data["intent"] == "exercise_guidance"
    assert data["guidance"][0]["name"] == "杠铃卧推"
    assert "卧推" in data["reply"]
    cues = " ".join(data["guidance"][0]["cues"])
    assert data["guidance"][0]["cues"]
    assert "胸" in cues


def test_6d_teach_exercise_outside_catalog_does_not_fall_back_to_current(
    client: TestClient,
) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)
    started = client.post(f"{API}/workout-sessions", headers=headers, json={}).json()["data"]
    session_id = started["session"]["session_id"]

    data = _chat(client, headers, "教我波比跳。", page="workout",
                 session_id=session_id)
    assert data["intent"] == "exercise_guidance"
    assert data["guidance"][0]["name"] == "波比跳"
    assert data["session"] is None
    cues = " ".join(data["guidance"][0]["cues"])
    assert "波比跳" in data["reply"]
    assert "动作路线" in cues
    assert started["session"]["current_item"]["name"] not in cues


def test_coach_review_uses_profile_memory_and_progress(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)
    client.put(f"{API}/me/profile", headers=headers, json={
        "goal": "fat_loss", "sex": "male", "birth_year": 1997, "height_cm": 178,
        "weight_kg": 78, "weekly_days": 3, "session_minutes": 30,
        "equipment": ["dumbbell"], "allergies": [], "diet_preferences": [],
    })
    data = _chat(client, headers, "根据我的档案和记忆，帮我看看下一步该怎么调整")
    assert data["intent"] == "coach_review"
    assert "目标" in data["reply"]
    assert "下一步建议" in data["reply"]
    assert "我还没听出" not in data["reply"]
    # 只给建议，不擅自改计划
    assert data.get("plan_draft") is None
    assert data.get("patch_request") is None


def test_explicit_clock_preference_is_applied(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)
    data = _chat(client, headers, "以后都改到20:00之后训练")
    assert data["intent"] == "update_preference"
    profile = client.get(f"{API}/me/profile", headers=headers).json()["data"]
    assert profile["preferred_time_window"] == "20:00-21:30"
    assert "20:00" in profile["preferred_time_label"]


def test_clarify_has_no_quick_tags(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)
    data = _chat(client, headers, "帮我弄一下")
    assert data["intent"] == "clarify"
    assert data.get("actions") == []
    assert "今天只有 30 分钟" in data["reply"]


def test_review_followup_executes_first_suggestion(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)
    review = _chat(client, headers, "根据我的档案和记忆，帮我看看下一步该怎么调整")
    assert review["intent"] == "coach_review"
    data = _chat(client, headers, "帮我弄一下")
    assert data["intent"] == "review_followup"
    assert "我还没听出" not in data["reply"]
    assert "20 分钟" in data["reply"]
    assert data.get("day") is not None


def test_history_keeps_expandable_result(client: TestClient) -> None:
    headers = _headers(client)
    _setup_plan(client, headers)
    data = _chat(client, headers, "根据我的档案和记忆，帮我看看下一步该怎么调整")
    assert data["intent"] == "coach_review"

    rows = client.get(f"{API}/agent/messages?thread=coach&limit=20",
                      headers=headers).json()["data"]
    agent = next(item for item in rows if item["role"] == "agent")
    saved = agent.get("result") or {}
    assert saved.get("intent") == "coach_review"
    assert saved.get("reply") == data["reply"]
