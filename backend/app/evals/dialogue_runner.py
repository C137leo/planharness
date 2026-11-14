"""Stateful business evaluation. Expected outcomes NEVER enter the agent's inputs.

Only each conversation's initial state is seeded. All turns share one SQLite file,
user and thread; every snapshot uses a new Session. Replay is labelled separately
and must not be reported as natural-language accuracy.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.agent import coach_agent
from app.db import Base, make_engine, make_session_factory
from app.domain.day_plan import effective_day, save_override
from app.models import AgentMessage, Plan, Profile, User, WorkoutSession
from app.seed import DEMO_USER_ID, seed_demo
from app.domain.nutrition_catalog import ingredient_key

REFERENCE_DAY = dt.date(2026, 11, 11)
DAY = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()
ACTIONS = [
    {"slot": "strength", "name": name, "action_ref": ref, "sets": 4,
     "reps": 10, "duration_min": 8, "equipment": "bodyweight", "meta": {}}
    for name, ref in [("深蹲", "goblet-squat"), ("划船", "one-arm-dumbbell-row"),
                      ("俯卧撑", "push-up")]
]
MEALS = [
    {"slot": slot, "title": title, "items": [
        {"name": "鸡胸肉", "grams": 150, "kcal": 180, "protein": 30, "fat": 4, "carb": 0},
        {"name": "米饭", "grams": 150, "kcal": 174, "protein": 3, "fat": 0.4, "carb": 39}],
     "kcal": 354, "protein_g": 33, "fat_g": 4.4, "carb_g": 39}
    for slot, title in [("lunch", "午餐"), ("dinner", "晚餐")]
]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                      default=str).encode()).hexdigest()


def diff(before, after, path=""):
    """Leaf paths make the allowlist fail closed (including added/deleted keys)."""
    if isinstance(before, dict) and isinstance(after, dict):
        changes = []
        for key in sorted(before.keys() | after.keys()):
            child = f"{path}/{key}"
            if key not in before or key not in after:
                changes.append({"path": child, "before": before.get(key), "after": after.get(key)})
            else:
                changes += diff(before[key], after[key], child)
        return changes
    if before != after:
        return [{"path": path, "before": before, "after": after}]
    return []


class RecordingClient:
    def __init__(self, client):
        self.client = client
        self.calls = []
        self.model = getattr(client, "model", "replay")

    def available(self):
        return self.client.available()

    def complete(self, messages, **kwargs):
        call = {"messages": copy.deepcopy(messages), "parameters": kwargs}
        self.calls.append(call)
        started = time.monotonic()
        try:
            reply = self.client.complete(messages, **kwargs)
            call["response"] = copy.deepcopy(reply)
            (call["response"].get("message") or {}).pop("reasoning_content", None)
            return reply
        except Exception as exc:
            # No provider bodies or credentials in evaluation errors.
            call["error_type"] = type(exc).__name__
            raise
        finally:
            call["latency_ms"] = round((time.monotonic() - started) * 1000)


class DialogueEnvironment:
    def __init__(self, database_path: Path, thread: str, client):
        self.engine = make_engine(f"sqlite:///{database_path}")
        Base.metadata.create_all(self.engine)
        self.factory = make_session_factory(self.engine)
        self.thread = thread
        self.client = RecordingClient(client)
        # Mount the real authenticated API router without importing app.main's
        # global default app (which could initialize a developer database).
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.coach_routes import router, coach_client_dependency
        from app.config import Settings
        self.app = FastAPI()
        self.app.state.session_factory = self.factory
        self.app.state.settings = Settings(_env_file=None, database_url=f"sqlite:///{database_path}", secret_key="isolated-dialogue-evaluation-32-char-secret")
        self.app.include_router(router)
        self.app.dependency_overrides[coach_client_dependency] = lambda: self.client
        self.http = TestClient(self.app)
        with self.factory() as db:
            seed_demo(db)
            db.flush()
            user = db.get(User, DEMO_USER_ID)
            plan = db.scalar(select(Plan).where(Plan.user_id == user.id))
            save_override(db, user, DAY, actions=copy.deepcopy(ACTIONS), meals=copy.deepcopy(MEALS),
                          summary="隔离评测初始安排", constraints={}, reason="fixture",
                          base_version=plan.current_version)
            db.commit()
            from app.auth import issue_token
            self.headers = {"Authorization": "Bearer " + issue_token(user, self.app.state.settings)}

    def snapshot(self):
        with self.factory() as db:
            user = db.get(User, DEMO_USER_ID)
            days = {}
            for offset in (0, 1, 7):
                day = DAY + dt.timedelta(days=offset)
                payload = effective_day(db, user, day, fallback_next=False)
                # Stable, semantically addressable objects; no generated adjustment IDs.
                days[day.isoformat()] = {
                    "actions": {a["action_ref"]: a for a in payload["actions"]},
                    "action_order": [a["action_ref"] for a in payload["actions"]],
                    "meals": {m["slot"]: m for m in payload["meals"]},
                }
            excluded = {"agent_runs", "agent_messages", "plan_adjustments", "temporary_contexts", "coach_task_executions"}
            tables = {}
            for table in Base.metadata.sorted_tables:
                if table.name in excluded:
                    continue
                rows = [dict(row._mapping) for row in db.execute(select(table))]
                tables[table.name] = sorted(json.loads(json.dumps(rows, default=str)),
                                            key=lambda r: json.dumps(r, sort_keys=True))
            temporary = [dict(r._mapping) for r in db.execute(select(Base.metadata.tables["temporary_contexts"]))]
            return {"days": days, "tables": tables,
                    "temporary_contexts": json.loads(json.dumps(temporary, default=str))}

    def history(self):
        with self.factory() as db:
            rows = db.scalars(select(AgentMessage).where(AgentMessage.thread == self.thread)
                              .order_by(AgentMessage.created_at)).all()
            return [{"role": row.role, "text": row.text, "result": row.result} for row in rows]

    def step(self, text, index):
        before, history_before = self.snapshot(), self.history()
        calls_start = len(self.client.calls)
        response = self.http.post("/api/v1/agent/chat", headers=self.headers, json={
            "text": text, "date": DAY.isoformat(), "thread": self.thread,
            "request_id": f"{self.thread}:{index}"})
        if response.status_code != 200:
            raise RuntimeError(f"evaluation API status {response.status_code}")
        result = response.json()["data"]
        after, history_after = self.snapshot(), self.history()
        calls = self.client.calls[calls_start:]
        context = None
        for call in calls:
            for message in call["messages"]:
                if message["role"] == "user":
                    try:
                        payload = json.loads(message["content"])
                    except (ValueError, TypeError):
                        continue
                    if isinstance(payload, dict) and "context" in payload:
                        context = payload["context"]
                        break
            if context is not None:
                break
        return {"turn": index, "user_text": text, "context_used": context,
                "history_before": history_before, "history_after": history_after,
                "recognized_goals": [t.get("user_goal") for t in result.get("task_plan", [])],
                "semantic_result": [c.get("response", {}).get("message") for c in calls],
                "selected_tools": [t["detail"] for t in result.get("trace", [])
                                   if t["node"] == "task_execution"],
                "result": result, "reply": result.get("reply"), "model_calls": calls,
                "before": before, "after": after, "actual_data_changes": diff(before, after)}

    def close(self):
        self.http.close()
        self.engine.dispose()



def evaluation_path(path, evaluation_day=None):
    """Shift fixture-date oracle paths, never rewrite the user's raw utterance."""
    parts = path.split("/")
    if len(parts) >= 3 and parts[1] == "days":
        try:
            delta = dt.date.fromisoformat(parts[2]) - REFERENCE_DAY
        except ValueError:
            return path
        parts[2] = ((evaluation_day or DAY) + delta).isoformat()
    return "/".join(parts)

def value_at(snapshot, path):
    value = snapshot
    recorded_day = dt.date.fromisoformat(min(snapshot["days"])) if isinstance(snapshot, dict) and snapshot.get("days") else DAY
    for key in evaluation_path(path, recorded_day).strip("/").split("/"):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def judge(row, expected):
    """Deterministic goal and scope judge, independent of intent/operation labels."""
    failures = []
    calls = row.get("model_calls") or []
    available = bool(calls) and any("response" in call for call in calls)
    row["model_available"] = available
    if not available:
        failures.append({"stage": "infrastructure", "reason": "No model response; not an NLU pass"})
    elif row["result"].get("error"):
        failures.append({"stage": "semantic_output", "error": row["result"]["error"]})
    for assertion in expected.get("assertions", []):
        actual = value_at(row["after"], assertion["path"])
        kind = assertion.get("kind", "equals")
        if kind == "equals":
            ok = actual == assertion.get("value")
        elif kind == "absent":
            ok = actual is None
        elif kind in {"contains_food", "excludes_food"}:
            present = assertion["value"] in [i.get("name") for i in (actual or [])]
            ok = present if kind == "contains_food" else not present
        elif kind == "unchanged":
            ok = actual == value_at(row["before"], assertion["path"])
        elif kind == "changed":
            ok = actual != value_at(row["before"], assertion["path"])
        elif kind == "food_grams":
            ok = sum(float(i.get("grams") or 0) for i in (actual or [])
                     if (i.get("ingredient_key") or ingredient_key(i.get("name")) or i.get("name")) == (ingredient_key(assertion["name"]) or assertion["name"])) == assertion["value"]
        else:
            ok = False
        if not ok:
            failures.append({"stage": "goal", "assertion": assertion, "actual": actual})
    recorded_day = dt.date.fromisoformat(min(row["after"]["days"]))
    allowed = [evaluation_path(p, recorded_day).rstrip("/") for p in expected.get("allowed_changes", [])]
    unexpected = [change for change in row["actual_data_changes"]
                  if not any(change["path"] == p or change["path"].startswith(p + "/") for p in allowed)]
    if unexpected:
        failures.append({"stage": "scope", "unexpected_changes": unexpected})
    if len(row["history_after"]) != len(row["history_before"]) + 2:
        failures.append({"stage": "history", "reason": "turn not persisted exactly once"})
    # No goal credit for a failed/clarifying task even if an earlier turn happened
    # to have put the database into the desired state.
    if expected.get("completion", True):
        results = row["result"].get("task_results") or []
        planned = {t["task_id"]: t for t in row["result"].get("task_plan", [])}
        if not results or any(r["status"] != "success" and not (
                r["status"] == "cancelled" and planned.get(r["task_id"], {}).get("execution_mode") == "noop" and not r.get("changes")) for r in results):
            failures.append({"stage": "execution_status", "results": results,
                             "error": row["result"].get("error")})
    tags = set(row.get("evaluation_tags") or [])
    task_results = row["result"].get("task_results") or []
    if not expected.get("completion", True) and not expected.get("clarification"):
        if "partial_success" in tags:
            if not any(r["status"] == "success" for r in task_results) or not any(r["status"] != "success" for r in task_results):
                failures.append({"stage": "partial_success", "reason": "expected independently tracked mixed outcomes"})
        elif not task_results or any(r["status"] not in ({"success", "confirmation_required", "clarification_required"} if "advice_only" in tags else {"success", "confirmation_required", "cancelled"}) for r in task_results):
            failures.append({"stage": "execution_status", "reason": "unsupported/failed is not a successful read or no-op"})
    if "audit_query" in tags or "audit_evidence" in tags:
        if not any(t.get("selected_tool") == "conversation_state.inspect_or_undo" for t in task_results):
            failures.append({"stage": "audit_evidence", "reason": "explanation did not consult verified execution records"})
    if expected.get("requires_advice"):
        plans = row["result"].get("task_plan") or []
        rendered = False
        for task in plans:
            details = task.get("proposal_details") or {}
            facts = list(details.get("benefits") or []) + list(details.get("tradeoffs") or [])
            note = (task.get("slots") or {}).get("note")
            if facts and all(str(fact) in row["reply"] for fact in facts):
                rendered = True
            if note and note != task.get("user_goal") and str(note) in row["reply"]:
                rendered = True
        if not rendered and not any(t.get("operation") == "answer_nutrition" for t in plans):
            failures.append({"stage": "advice_rendering", "reason": "no concrete prepared advice rendered"})
        if row["actual_data_changes"]:
            failures.append({"stage": "advice_scope", "reason": "advice-only request mutated business state"})
    if expected.get("requires_current_state"):
        views = [(r.get("inspection") or {}).get("current_state") for r in task_results]
        expected_day = row["after"]["days"][recorded_day.isoformat()]
        if not any(v and v.get("actions") == expected_day["actions"] and v.get("meals") == expected_day["meals"] for v in views):
            failures.append({"stage": "current_state", "reason": "answer did not return current reloaded business facts"})
    if expected.get("clarification"):
        if row["actual_data_changes"] or not row["reply"] or not any(
                r["status"] == "clarification_required" for r in row["result"].get("task_results", [])):
            failures.append({"stage": "clarification", "reason": "must ask without business mutation"})
    if expected.get("reference_to_previous"):
        ctx = row["context_used"] or {}
        last_tasks = (ctx.get("task_context") or {}).get("last_task_plan")
        if not (last_tasks or ctx.get("execution_history")) or not row["history_before"]:
            failures.append({"stage": "context", "reason": "previous task history missing"})
    for task_result in task_results:
        inspection = task_result.get("inspection")
        if not inspection:
            continue
        history = (row.get("context_used") or {}).get("execution_history") or []
        known = {event["execution_id"]: event for event in history}
        for fact in inspection.get("facts", []):
            reference = known.get(fact["execution_id"])
            if reference and (reference["changes"] != fact["changes"] or reference["status"] != fact["status"]):
                failures.append({"stage": "audit_facts", "reason": "inspection facts disagree with persisted history"})
        selector = task_result.get("tool_arguments") or {}
        candidates = [e for e in history if
                      (selector.get("meal_slot") in (None, "all") or any(x["kind"] == "meals" and x["key"] == selector["meal_slot"] for x in e["changes"])) and
                      (not selector.get("source_operation") or e["operation"] == selector["source_operation"]) and
                      (not selector.get("execution_id") or e["execution_id"] == selector["execution_id"])]
        if candidates and not inspection.get("matched_execution_ids") and not selector.get("action_ref"):
            failures.append({"stage": "audit_facts", "reason": "claimed no records despite matching persisted evidence"})
    row["reference_expected"] = expected.get("reference_to_previous", False)
    row["unexpected_changes"] = unexpected
    row["next_turn_reference_verified"] = expected.get("reference_to_previous", False) and not failures
    row["failures"] = failures
    row["passed"] = not failures
    return row
