"""API → real SQLite writes → fresh-session reads. Semantics are mocked explicitly."""
import datetime as dt
import json

import pytest
from sqlalchemy import select

from app.agent import legacy_coach_agent as c, intent_router
from app.api import coach_routes
from app.domain.day_plan import effective_day, save_override
from app.models import AgentRun, Plan, PlanAdjustment, User

from app.evals.dialogue_runner import DAY
ACTIONS = [{"slot": "strength", "name": name, "action_ref": ref, "sets": 3,
            "reps": 10, "duration_min": 8, "equipment": "bodyweight", "meta": {}}
           for name, ref in [("深蹲", "squat"), ("划船", "row"), ("硬拉", "deadlift")]]
MEALS = [{"slot": slot, "title": slot, "items": [{"name": "鸡胸肉", "grams": 100, "kcal": 120, "protein": 20},
                                                   {"name": "米饭", "grams": 100, "kcal": 116, "protein": 2}],
          "kcal": 236, "protein_g": 22} for slot in ["lunch", "dinner"]]


def task(tid, operation, slots=None, **extra):
    nutrition = operation in {"modify_meal_plan", "replace_meal_item"}
    return {"task_id": tid, "domain": "nutrition" if nutrition else "training",
            "intent": "nutrition_advice" if nutrition else "adjust_today", "scope": "today",
            "operation": operation, "slots": slots or {}, "user_goal": operation,
            "dependencies": [], **extra}


@pytest.fixture()
def harness(client, auth_headers, monkeypatch):
    factory = client.app.state.session_factory
    with factory() as db:
        user = db.scalar(select(User))
        uid = user.id
        plan = db.scalar(select(Plan).where(Plan.user_id == uid, Plan.status == "active"))
        save_override(db, user, DAY, actions=ACTIONS, meals=MEALS, summary="fixture",
                      constraints={}, reason="fixture", base_version=plan.current_version)
        db.commit()
    tasks = []
    monkeypatch.setattr(coach_routes, "_tool_client", lambda request: object())
    monkeypatch.setattr(c, "understand_tasks", lambda *args, **kw: (tasks, "", "", ""))

    def chat(key="test-key", text="今天有点累，第一个跳过，第三个换拉伸，晚饭不要鸡胸肉"):
        return client.post("/api/v1/agent/chat", headers=auth_headers,
                           json={"text": text, "date": DAY.isoformat(), "request_id": key})

    def state(day=DAY):
        with factory() as db:
            return effective_day(db, db.get(User, uid), day)
    return tasks, chat, state, factory, uid


def test_acceptance_three_tasks_persist_and_replay(harness):
    tasks, chat, state, factory, uid = harness
    tasks.extend([task("T1", "skip_action", {"target_index": 1}),
                  task("T2", "replace_action", {"target_index": 3, "replacement_category": "stretching"}),
                  task("T3", "modify_meal_plan", {"meal_slot": "dinner", "avoid_foods": ["鸡胸肉"]})])
    before_other = state(DAY + dt.timedelta(days=1))
    response = chat()
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert [r["status"] for r in result["task_results"]] == ["success"] * 3
    after = state()
    assert [a["action_ref"] for a in after["actions"]] == ["row", "full_body_stretch"]
    assert after["meals"][0]["items"][0]["name"] == "鸡胸肉"  # lunch stays unchanged
    assert all(i["name"] != "鸡胸肉" for i in after["meals"][1]["items"])
    assert state(DAY + dt.timedelta(days=1))["actions"] == before_other["actions"]
    with factory() as db:
        count = len(db.scalars(select(PlanAdjustment)).all())
    assert chat().json()["data"] == result
    with factory() as db:
        assert len(db.scalars(select(PlanAdjustment)).all()) == count
    assert chat(text="不同请求").status_code == 409


def test_partial_success_survives_write_failure(harness, monkeypatch):
    tasks, chat, state, _, _ = harness
    tasks.extend([task("T1", "skip_action", {"target_index": 1}),
                  task("T2", "modify_meal_plan", {"meal_slot": "dinner", "avoid_foods": ["鸡胸肉"]}),
                  task("T3", "replace_action", {"target_index": 3, "replacement_category": "stretching"})])
    original = c.save_override
    def fail_meal(*args, **kwargs):
        if kwargs.get("constraints", {}).get("meal_constraints"):
            raise RuntimeError("simulated database failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(c, "save_override", fail_meal)
    result = chat().json()["data"]
    assert [r["status"] for r in result["task_results"]] == ["success", "failed", "success"]
    assert result["overall_status"] == "partial_success"
    assert [a["action_ref"] for a in state()["actions"]] == ["row", "full_body_stretch"]
    assert state()["meals"][1]["items"][0]["name"] == "鸡胸肉"


def test_noop_tool_is_not_success(harness, monkeypatch):
    tasks, chat, state, _, _ = harness
    tasks.append(task("T1", "skip_action", {"target_index": 1}))
    monkeypatch.setattr(c, "save_override", lambda *a, **kw: {"id": "fake-success"})
    result = chat().json()["data"]
    assert result["task_results"][0]["failure_stage"] == "verification"
    assert len(state()["actions"]) == 3
    assert result["changes"] == []


@pytest.mark.parametrize("item,expected", [
    (task("T1", "teleport"), "unsupported"),
    (task("T1", "skip_action", {"target_index": 9}), "clarification_required"),
    (task("T1", "skip_action", {"target_index": 1}, scope="long_term"), "clarification_required"),
    (task("T1", "skip_action", {"target_index": 1}, dependencies=["missing"]), "blocked"),
    (task("T1", "modify_training", clarification_needed=True), "clarification_required"),
])
def test_safe_failure_states(harness, item, expected):
    tasks, chat, state, _, _ = harness
    tasks.append(item)
    result = chat().json()["data"]
    assert result["task_results"][0]["status"] == expected
    assert len(state()["actions"]) == 3


def test_context_bound_ref_and_exact_sets(harness):
    tasks, chat, state, _, _ = harness
    tasks.append(task("T1", "change_sets", {"sets": 1}, context_reference={"action_ref": "row"}))
    assert chat().json()["data"]["task_results"][0]["status"] == "success"
    assert state()["actions"][1]["sets"] == 1


class Model:
    def __init__(self, content):
        self.content = content
        self.calls = 0
    def available(self):
        return True
    def complete(self, *a, **kw):
        self.calls += 1
        if isinstance(self.content, Exception):
            raise self.content
        return {"message": {"content": self.content}}


def test_single_semantic_call_preserves_constraints():
    model = Model(json.dumps({"tasks": [task("T1", "modify_meal_plan", {"meal_slot": "dinner", "avoid_foods": ["鸡胸肉"]})]}))
    tasks, _, _, error = intent_router.understand_tasks(model, "晚饭不要鸡胸肉", context={})
    assert not error and model.calls == 1
    assert tasks[0]["slots"]["avoid_foods"] == ["鸡胸肉"]


@pytest.mark.parametrize("content", ["not json", '{"tasks":"bad"}',
    json.dumps({"tasks": [task("T1", "skip_action"), task("T1", "skip_action")]}),
    json.dumps({"tasks": [task("T1", "skip_action", dependencies=["T1"])]}),
    json.dumps({"tasks": [task("T1", "skip_action", confidence=3)]}), TimeoutError("timeout")])
def test_model_invalid_schema_or_timeout(content):
    tasks, _, _, error = intent_router.understand_tasks(Model(content), "调整", context={})
    assert not tasks and error


def test_simple_query_does_not_mutate(harness):
    tasks, chat, state, _, _ = harness
    tasks.append(task("T1", "brief_today", intent="today_brief", action_type="query"))
    before = state()
    result = chat(text="今天练什么").json()["data"]
    assert result["today_brief"]
    assert state()["actions"] == before["actions"]


def test_invalid_semantics_never_falls_back_to_mutation(harness, monkeypatch):
    _, chat, state, _, _ = harness
    before = state()
    monkeypatch.setattr(c, "understand_tasks", lambda *a, **kw: ([], "bad", "", "semantic_schema_invalid"))
    result = chat(text="今天休息").json()["data"]
    assert result["error"] == "semantic_schema_invalid"
    assert state()["actions"] == before["actions"]


def test_current_workout_is_the_mutation_target(harness, client, auth_headers):
    tasks, chat, state, _, _ = harness
    response = client.post("/api/v1/workout-sessions", headers=auth_headers,
                           json={"date": DAY.isoformat()})
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    sid = payload.get("session_id") or payload.get("id") or (payload.get("session") or {}).get("session_id")
    # Query actual session ID; the public payload varies between start and get.
    from app.models import WorkoutSession
    factory = client.app.state.session_factory
    with factory() as db:
        session = db.scalar(select(WorkoutSession))
        sid = session.id
    tasks.append(task("T1", "skip_action", {"target_index": 1}, intent="adjust_workout", scope="current_workout"))
    result = chat().json()["data"]
    assert result["task_results"][0]["status"] == "success"
    with factory() as db:
        session = db.get(WorkoutSession, sid)
        assert session.items[0]["status"] == "skipped"
    assert len(state()["actions"]) == 3


def test_commit_failure_is_reported_and_rolled_back(harness, monkeypatch):
    from sqlalchemy.orm import Session
    tasks, chat, state, _, _ = harness
    tasks.extend([task("T1", "skip_action", {"target_index": 1}),
                  task("T2", "replace_action", {"target_index": 3, "replacement_category": "stretching"})])
    commit = Session.commit
    calls = 0
    def fail_third(db):
        nonlocal calls
        calls += 1
        if calls == 3:  # request claim, T1, then T2
            raise RuntimeError("simulated commit failure")
        return commit(db)
    monkeypatch.setattr(Session, "commit", fail_third)
    result = chat().json()["data"]
    assert result["task_results"][0]["status"] == "success"
    assert result["task_results"][1]["failure_stage"] == "persistence"
    assert [a["action_ref"] for a in state()["actions"]] == ["row", "deadlift"]
