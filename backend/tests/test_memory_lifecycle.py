"""记忆生命周期、临时上下文 TTL、计划调整标识、Profile/Memory 单一事实来源。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.domain.memory import load_memory_context, load_temporary
from app.domain.memory_lifecycle import run_lifecycle_cleanup
from app.domain.day_plan import effective_day
from app.domain.timeutil import user_today
from app.models import (
    Memory,
    MemoryRevision,
    Plan,
    PlanAdjustment,
    Profile,
    TemporaryContext,
    User,
    WorkoutLog,
    WorkoutSession,
)
from tests.conftest import register_and_login

API = "/api/v1"
DEMO_USER_ID = "00000000-0000-0000-0000-000000000001"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _demo_user(app) -> User:
    with app.state.session_factory() as db:
        return db.get(User, DEMO_USER_ID)


def test_old_short_term_memory_expires_but_training_history_survives(
    client, app_fixture, auth_headers
) -> None:
    _, app = app_fixture
    # 真实训练历史：一条 API 记录 + 一条直接写入的 WorkoutLog
    created = client.post(f"{API}/workout-logs", headers=auth_headers, json={
        "action_ref": "pushup", "kind": "strength", "duration_min": 20,
    })
    assert created.status_code == 200, created.text
    now = _now()
    with app.state.session_factory() as db:
        db.add(WorkoutLog(
            user_id=DEMO_USER_ID, date=now.date(), action_ref="row",
            kind="strength", duration_min=10,
        ))
        old = Memory(
            user_id=DEMO_USER_ID, kind="short_term", category="session",
            key="session:old-30d", value="30 天前的普通训练摘要",
            status="active", importance=0.3,
            created_at=now - timedelta(days=40),
            updated_at=now - timedelta(days=40),
            expires_at=now - timedelta(days=10),
        )
        db.add(old)
        db.commit()
        old_id = old.id

    result = run_lifecycle_cleanup(app.state.session_factory(), now=now)
    assert result["short_term_expired"] >= 1
    assert result["errors"] == 0

    with app.state.session_factory() as db:
        row = db.get(Memory, old_id)
        assert row is not None, "短期记忆应保留行，只改状态"
        assert row.status == "expired"
        # 真实训练历史没有被清理
        assert db.query(WorkoutLog).count() >= 2
        assert db.query(WorkoutSession).count() >= 0
        assert db.query(MemoryRevision).filter(
            MemoryRevision.memory_id == old_id,
            MemoryRevision.new_status == "expired",
        ).count() == 1

    logs = client.get(f"{API}/workout-logs?days=90", headers=auth_headers)
    assert logs.status_code == 200
    assert len(logs.json()["data"]["items"]) >= 2, "训练历史不能因短期记忆过期被删"
    visible = client.get(f"{API}/agent/memories", headers=auth_headers).json()["data"]["items"]
    assert all(item["id"] != old_id for item in visible), "读取接口必须过滤过期记忆"


def test_short_term_behavior_is_distilled_before_expiry(
    client, app_fixture, auth_headers
) -> None:
    _, app = app_fixture
    # 造两场未完成训练 + 当前短期状态
    for _ in range(2):
        start = client.post(f"{API}/workout-sessions", headers=auth_headers, json={})
        session_id = start.json()["data"]["session"]["session_id"]
        client.post(
            f"{API}/workout-sessions/{session_id}/finish", headers=auth_headers,
            json={"completed": False, "completion_pct": 25, "difficulty": 5, "fatigue": 5},
        )
    now = _now()
    with app.state.session_factory() as db:
        state = db.query(Memory).filter(
            Memory.user_id == DEMO_USER_ID,
            Memory.category == "training_state",
        ).first()
        assert state is not None
        state.expires_at = now - timedelta(days=1)
        state.updated_at = now - timedelta(days=2)
        db.commit()
        sessions_before = db.query(WorkoutSession).count()

    run_lifecycle_cleanup(app.state.session_factory(), now=now)
    with app.state.session_factory() as db:
        assert db.query(WorkoutSession).count() == sessions_before
        behavior = db.query(Memory).filter(
            Memory.user_id == DEMO_USER_ID,
            Memory.kind == "long_term",
            Memory.category == "behavior",
            Memory.key == "training_pattern",
        ).first()
        assert behavior is not None, "短期状态过期前应提炼长期行为规律"
        assert behavior.structured.get("consecutive_incomplete") >= 2
        state = db.query(Memory).filter(
            Memory.user_id == DEMO_USER_ID,
            Memory.category == "training_state",
        ).first()
        assert state.status == "expired"


def test_expired_temporary_context_is_ignored_by_reads(app_fixture) -> None:
    _, app = app_fixture
    now = _now()
    with app.state.session_factory() as db:
        user = db.get(User, DEMO_USER_ID)
        profile = db.get(Profile, DEMO_USER_ID)
        old_day = user_today(profile) - timedelta(days=10)
        db.add(TemporaryContext(
            user_id=user.id, day=old_day, effective_date=old_day,
            key="available_minutes", value={"minutes": 15},
            status="active", expires_at=now - timedelta(hours=1),
        ))
        db.commit()
        assert load_temporary(db, user, old_day) == {}
        day = effective_day(db, user, user_today(profile), fallback_next=True)
        assert day["source"] == "plan"


def test_today_shortening_does_not_change_version_or_long_term_profile(
    client, app_fixture, auth_headers
) -> None:
    _, app = app_fixture
    before_plan = client.get(f"{API}/plans/current", headers=auth_headers).json()["data"]
    before_profile = client.get(f"{API}/me/profile", headers=auth_headers).json()["data"]

    resp = client.post(f"{API}/agent/chat", headers=auth_headers, json={
        "text": "今天加班，只有 20 分钟，不去健身房了。", "page": "today",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["intent"] == "adjust_today"
    assert data["day"]["source"] == "agent"
    assert data["day"]["total_minutes"] <= 25

    after_plan = client.get(f"{API}/plans/current", headers=auth_headers).json()["data"]
    after_profile = client.get(f"{API}/me/profile", headers=auth_headers).json()["data"]
    assert after_plan["version"] == before_plan["version"]
    assert after_profile["session_minutes"] == before_profile["session_minutes"]
    assert after_profile["weekly_days"] == before_profile["weekly_days"]
    assert after_profile["preferred_time_window"] == before_profile["preferred_time_window"]

    with app.state.session_factory() as db:
        active = db.query(PlanAdjustment).filter(
            PlanAdjustment.user_id == DEMO_USER_ID,
            PlanAdjustment.status == "active",
        ).first()
        assert active is not None
        assert active.base_version == before_plan["version"]
        long_term = db.query(Memory).filter(
            Memory.user_id == DEMO_USER_ID,
            Memory.kind == "long_term",
        ).all()
        assert all("20" not in (row.value or "") for row in long_term)


def test_explicit_long_term_time_change_syncs_profile_and_memory(
    client, app_fixture, auth_headers
) -> None:
    resp = client.post(f"{API}/agent/chat", headers=auth_headers, json={
        "text": "以后尽量把训练安排在午休。", "page": "profile",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["intent"] == "update_preference"
    profile = client.get(f"{API}/me/profile", headers=auth_headers).json()["data"]
    assert profile["preferred_time_window"] == "12:00-13:00"
    memory = client.get(f"{API}/agent/memories", headers=auth_headers).json()["data"]["items"]
    schedule = [item for item in memory
                if item["category"] == "schedule" and item["kind"] == "long_term"]
    assert len(schedule) == 1
    assert schedule[0]["source"] == "profile_sync"
    assert schedule[0]["canonical"] is True

    # 明确说"以后晚上" = 长期偏好变化：Profile 和 Memory 同步更新，并留变更记录
    resp = client.post(f"{API}/agent/chat", headers=auth_headers, json={
        "text": "以后尽量把训练安排在晚上。", "page": "profile",
    })
    assert resp.status_code == 200, resp.text
    profile = client.get(f"{API}/me/profile", headers=auth_headers).json()["data"]
    assert profile["preferred_time_window"] == "19:00-20:00"
    schedule = [item for item in client.get(f"{API}/agent/memories", headers=auth_headers)
                .json()["data"]["items"]
                if item["category"] == "schedule"]
    assert len(schedule) == 1
    assert "19:00-20:00" in schedule[0]["value"]
    with app_fixture[1].state.session_factory() as db:
        revisions = db.query(MemoryRevision).filter(
            MemoryRevision.user_id == DEMO_USER_ID,
            MemoryRevision.category == "schedule",
        ).all()
        assert len(revisions) >= 2
        assert any("12:00-13:00" in (row.old_value or "") for row in revisions)


def test_evening_availability_only_creates_temporary_context(
    client, app_fixture, auth_headers
) -> None:
    _, app = app_fixture
    client.put(f"{API}/me/profile", headers=auth_headers, json={
        "preferred_time_label": "午休",
        "preferred_time_window": "12:00-13:00",
    })
    resp = client.post(f"{API}/agent/chat", headers=auth_headers, json={
        "text": "今天晚上有空。", "page": "profile",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["intent"] == "adjust_today"
    profile = client.get(f"{API}/me/profile", headers=auth_headers).json()["data"]
    assert profile["preferred_time_window"] == "12:00-13:00"
    with app.state.session_factory() as db:
        temp = db.query(TemporaryContext).filter(
            TemporaryContext.user_id == DEMO_USER_ID,
            TemporaryContext.key == "time_window",
        ).first()
        assert temp is not None
        assert temp.value["window"] == "19:00-20:00"
        assert temp.effective_date == user_today(db.get(Profile, DEMO_USER_ID))
        # 时间窗口不是计划内容变更，不应产生 PlanAdjustment
        assert db.query(PlanAdjustment).filter(
            PlanAdjustment.user_id == DEMO_USER_ID,
            PlanAdjustment.status == "active",
        ).count() == 0


def test_plan_adjustment_is_visible_and_restorable(
    client, app_fixture, auth_headers
) -> None:
    resp = client.post(f"{API}/agent/chat", headers=auth_headers, json={
        "text": "今天只有 20 分钟，不去健身房了。", "page": "plan",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["day"]["adjustment_id"]

    day = date.today()
    adjustments = client.get(
        f"{API}/agent/adjustments?start={day.isoformat()}&end={day.isoformat()}",
        headers=auth_headers,
    ).json()["data"]["items"]
    active = [item for item in adjustments if item["status"] == "active"]
    assert active and active[0]["adjustment_id"] == data["day"]["adjustment_id"]
    assert active[0]["summary"]

    reset = client.post(f"{API}/agent/day/reset", headers=auth_headers,
                        json={"date": day.isoformat()})
    assert reset.status_code == 200, reset.text
    assert reset.json()["data"]["source"] == "plan"
    after_reset = client.get(
        f"{API}/agent/adjustments?start={day.isoformat()}&end={day.isoformat()}",
        headers=auth_headers,
    ).json()["data"]["items"]
    assert all(item["status"] != "active" for item in after_reset)
    assert any(item["status"] == "reset" for item in after_reset)

    restored = client.post(
        f"{API}/agent/adjustments/{active[0]['adjustment_id']}/restore",
        headers=auth_headers,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["day"]["source"] == "agent"


def test_conflicting_time_preference_requires_scope_confirmation(
    client, auth_headers
) -> None:
    client.post(f"{API}/agent/chat", headers=auth_headers, json={
        "text": "以后尽量把训练安排在午休。", "page": "profile",
    })
    resp = client.post(f"{API}/agent/chat", headers=auth_headers, json={
        "text": "训练时间改到早上。", "page": "profile",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["intent"] == "update_preference"
    assert "以后都按新的来" in data["reply"] or "只调整今天" in data["reply"]
    profile = client.get(f"{API}/me/profile", headers=auth_headers).json()["data"]
    assert profile["preferred_time_window"] == "12:00-13:00", "模糊冲突不能直接改档案"

    resp = client.post(f"{API}/agent/chat", headers=auth_headers, json={
        "text": "以后训练安排在早上。", "page": "profile",
    })
    assert resp.status_code == 200, resp.text
    profile = client.get(f"{API}/me/profile", headers=auth_headers).json()["data"]
    assert profile["preferred_time_window"] == "07:00-08:00"


def test_memories_are_isolated_between_users(client, auth_headers) -> None:
    client.post(f"{API}/agent/chat", headers=auth_headers, json={
        "text": "以后训练安排在午休。", "page": "profile",
    })
    other = register_and_login(client, phone="13900000001", password="test-pass-2027")
    other_memories = client.get(f"{API}/agent/memories", headers=other)
    assert other_memories.status_code == 200
    assert other_memories.json()["data"]["items"] == []
    answer = client.post(f"{API}/agent/chat", headers=other, json={
        "text": "你记得我什么？", "page": "profile",
    }).json()["data"]
    assert "0 条长期偏好" in answer["reply"]
    mine = client.get(f"{API}/agent/memories", headers=auth_headers).json()["data"]["items"]
    assert any(item["category"] == "schedule" for item in mine)


def test_timezone_controls_effective_date(
    client, app_fixture, auth_headers
) -> None:
    _, app = app_fixture
    # UTC+14：服务器日期和用户日期可能不同
    client.put(f"{API}/me/profile", headers=auth_headers, json={
        "timezone": "Pacific/Kiritimati",
    })
    resp = client.post(f"{API}/agent/chat", headers=auth_headers, json={
        "text": "今天只有 20 分钟，不去健身房了。", "page": "today",
    })
    assert resp.status_code == 200, resp.text
    with app.state.session_factory() as db:
        profile = db.get(Profile, DEMO_USER_ID)
        expected = user_today(profile)
        temp = db.query(TemporaryContext).filter(
            TemporaryContext.user_id == DEMO_USER_ID,
            TemporaryContext.key == "available_minutes",
        ).first()
        assert temp.effective_date == expected
        assert temp.expires_at is not None


def test_cleanup_writes_audit_log_and_memory_status(client, app_fixture, auth_headers) -> None:
    _, app = app_fixture
    run_lifecycle_cleanup(app.state.session_factory())
    status = client.get(f"{API}/agent/memory/status", headers=auth_headers)
    assert status.status_code == 200, status.text
    body = status.json()["data"]
    assert body["short_term_retention_days"] == 30
    assert body["temporary_context_retention_days"] == 14
    assert body["last_run"] is not None
    assert body["last_run"]["short_term_retention_days"] == 30


def test_profile_form_edit_syncs_canonical_memory(client, auth_headers) -> None:
    resp = client.put(f"{API}/me/profile", headers=auth_headers, json={
        "preferred_time_label": "晚上",
        "preferred_time_window": "19:00-20:00",
        "preferred_location": "home",
        "likes": ["划船"],
    })
    assert resp.status_code == 200, resp.text
    items = client.get(f"{API}/agent/memories", headers=auth_headers).json()["data"]["items"]
    schedule = next(item for item in items if item["category"] == "schedule")
    assert schedule["canonical"] is True
    assert schedule["source"] == "profile_sync"
    assert "19:00-20:00" in schedule["value"]
    assert any(item["category"] == "like" and item["key"] == "划船" for item in items)
    assert any(item["category"] == "location" for item in items)


def test_editing_canonical_memory_updates_profile(client, auth_headers) -> None:
    client.post(f"{API}/agent/chat", headers=auth_headers, json={
        "text": "以后尽量把训练安排在午休。", "page": "profile",
    })
    memory = next(item for item in client.get(f"{API}/agent/memories", headers=auth_headers)
                  .json()["data"]["items"] if item["category"] == "schedule")
    resp = client.put(f"{API}/agent/memories/{memory['id']}", headers=auth_headers,
                      json={"value": "晚上 20:00 训练"})
    assert resp.status_code == 200, resp.text
    profile = client.get(f"{API}/me/profile", headers=auth_headers).json()["data"]
    assert profile["preferred_time_window"] == "20:00-21:00"


def test_finishing_workout_marks_temporary_context_completed(
    client, app_fixture, auth_headers
) -> None:
    _, app = app_fixture
    client.post(f"{API}/agent/chat", headers=auth_headers, json={
        "text": "今天只有 20 分钟，不去健身房了。", "page": "today",
    })
    start = client.post(f"{API}/workout-sessions", headers=auth_headers, json={})
    session_id = start.json()["data"]["session"]["session_id"]
    finish = client.post(f"{API}/workout-sessions/{session_id}/finish",
                         headers=auth_headers, json={"completed": True})
    assert finish.status_code == 200, finish.text
    with app.state.session_factory() as db:
        user = db.get(User, DEMO_USER_ID)
        rows = db.query(TemporaryContext).filter(
            TemporaryContext.user_id == user.id,
            TemporaryContext.status == "completed",
        ).all()
        assert rows, "结束训练后临时上下文应标记 completed，而不是物理删除"
        assert all(row.completed_at is not None for row in rows)


def test_deleting_canonical_memory_clears_profile_without_reappearing(
    client, auth_headers
) -> None:
    client.post(f"{API}/agent/chat", headers=auth_headers, json={
        "text": "以后尽量把训练安排在午休。", "page": "profile",
    })
    memory = next(item for item in client.get(f"{API}/agent/memories", headers=auth_headers)
                  .json()["data"]["items"] if item["category"] == "schedule")
    deleted = client.delete(f"{API}/agent/memories/{memory['id']}", headers=auth_headers)
    assert deleted.status_code == 200, deleted.text
    profile = client.get(f"{API}/me/profile", headers=auth_headers).json()["data"]
    assert profile["preferred_time_window"] == ""
    # 再跑一次维护，不应把已删除的 canonical 记忆重新写回来
    run_lifecycle_cleanup(client.app.state.session_factory())
    items = client.get(f"{API}/agent/memories", headers=auth_headers).json()["data"]["items"]
    assert all(item["id"] != memory["id"] for item in items)
    assert all(item["category"] != "schedule" for item in items)
