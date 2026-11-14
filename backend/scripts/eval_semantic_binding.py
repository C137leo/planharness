"""Stateful live evaluation with sealed oracles; no expected tool or intent reaches the agent."""
import argparse
import copy
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.llm.tool_client import tool_client_from_settings
from app.agent.business_snapshot import business_state, build_snapshot, digest
from app.evals.dialogue_runner import DialogueEnvironment, DAY
from app.domain.day_plan import effective_day, save_override
from app.models import User, new_id
from app.db import Base
from sqlalchemy import select
from app.seed import DEMO_USER_ID


def initialize(env, extra_warmup=False):
    with env.factory() as db:
        u = db.get(User, DEMO_USER_ID); value = effective_day(db, u, DAY)
        actions = [{"id": new_id(), "slot": slot, "name": name, "action_ref": ref, "sets": 4, "reps": 10,
                    "equipment": eq, "primary_muscle": muscle, "meta": {}}
                   for name, ref, slot, eq, muscle in [
                       ("山羊挺身", "back-extension", "strength", "bodyweight", "back"),
                       ("深蹲", "goblet-squat", "strength", "dumbbell", "quads"),
                       ("动态热身", "dynamic_warmup", "warmup", "bodyweight", "mobility"),
                       ("全身拉伸", "full_body_stretch", "mobility", "bodyweight", "mobility"),
                       ("引体向上", "pull-up", "strength", "pull-up bar", "back")]]
        if extra_warmup:
            actions.append({"id": new_id(), "slot": "warmup", "name": "肩部绕环", "action_ref": "arm-circles", "sets": 1, "reps": 10, "equipment": "bodyweight"})
        save_override(db, u, DAY, actions=actions, meals=value["meals"], constraints={}, summary="isolated fixture", reason="fixture", base_version=value["plan_version"])
        build_snapshot(db, u, env.thread, [str(DAY)])
        db.commit()


def state(env):
    with env.factory() as db:
        result = business_state(db, db.get(User, DEMO_USER_ID), [str(DAY)])
        excluded = {"agent_runs", "agent_messages", "plan_adjustments", "coach_task_executions", "coach_business_snapshots", "coach_binding_objects", "coach_pending_plans"}
        result["protected_tables"] = {table.name: sorted([json.loads(json.dumps(dict(row._mapping), default=str)) for row in db.execute(select(table))], key=lambda r: json.dumps(r, sort_keys=True))
                                      for table in Base.metadata.sorted_tables if table.name not in excluded}
        return result


def judge(before, after, result, expect):
    errors = []
    if result.get("overall_status") not in expect["statuses"]:
        errors.append("status:" + str(result.get("overall_status")))
    old, new = before["days"][str(DAY)], after["days"][str(DAY)]
    before_actions, after_actions = {a["id"]: a for a in old["actions"]}, {a["id"]: a for a in new["actions"]}
    allowed_actions = expect.get("allow_actions", [])
    for id in before_actions.keys() | after_actions.keys():
        a, b = before_actions.get(id), after_actions.get(id)
        if a != b and (a or b)["name"] not in allowed_actions:
            errors.append("unauthorized_action:" + (a or b)["name"])
    for name in (expect.get("absent", []) + (expect.get("absent_after_success", []) if result.get("overall_status") == "success" else [])):
        if any(a["name"] == name for a in new["actions"]): errors.append("not_removed:" + name)
    for name in expect.get("present", []):
        if not any(a["name"] == name for a in new["actions"]): errors.append("not_present:" + name)
    allowed_meals = expect.get("allow_meals", [])
    for a, b in zip(old["meals"], new["meals"]):
        if a != b and a["slot"] not in allowed_meals: errors.append("unauthorized_meal:" + a["slot"])
    for slot, food, present in (expect.get("food", []) + (expect.get("food_after_success", []) if result.get("overall_status") == "success" else [])):
        meal = next(m for m in new["meals"] if m["slot"] == slot)
        if any(food == i["name"] for i in meal["items"]) != present: errors.append("food:" + slot + food)
    for key in ("profile", "sessions", "memories", "protected_tables"):
        if before[key] != after[key]: errors.append("unauthorized_resource:" + key)
    if expect.get("bound_names"):
        tasks = result.get("task_plan") or []
        ids = {i for t in tasks for i in (t["resolution"].get("candidate_ids") or [t["resolution"].get("selected_id")]) if i}
        expected_ids = {a["id"] for a in old["actions"] if a["name"] in expect["bound_names"]}
        if ids != expected_ids: errors.append("binding_targets")
    if expect.get("candidate_names_required"):
        ids = {i for t in result.get("task_plan", []) for i in (t["resolution"].get("candidate_ids") or [t["resolution"].get("selected_id")]) if i}
        names = {a["name"] for a in old["actions"] if a["id"] in ids}
        if not set(expect["candidate_names_required"]) <= names or not names <= set(expect["candidate_names_allowed"]):
            errors.append("candidate_bounds")
    if expect.get("ambiguous"):
        tasks = result.get("task_plan") or []
        if not any(t["resolution"]["status"] == "ambiguous" and len(t["resolution"]["candidate_ids"]) >= 2 for t in tasks): errors.append("missing_ambiguity")
    for name, sets in expect.get("sets", []):
        if not any(a["name"] == name and a["sets"] == sets for a in new["actions"]): errors.append("sets:" + name)
    for slot, name, grams in expect.get("grams", []):
        meal = next(m for m in new["meals"] if m["slot"] == slot)
        if not any(i["name"] == name and i["grams"] == grams for i in meal["items"]): errors.append("grams:" + slot + name)
    for text in expect.get("reply_contains", []):
        if text not in result.get("reply", ""): errors.append("reply_fact:" + text)
    return errors


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--suite", type=Path, required=True); ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args(); suite = json.loads(args.suite.read_text())
    settings = Settings(); model = tool_client_from_settings(settings)
    if not model.available(): raise SystemExit("configured live model required")
    report = {"id": suite["id"], "mode": "live", "split": suite["split"], "model": model.model,
              "suite_hash": digest(suite), "source_hash": digest({str(p): p.read_text() for p in sorted(Path("app").rglob("*.py"))}),
              "conversations": [], "complete": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save(): args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    save()
    for conversation in suite["conversations"]:
        record = {"id": conversation["id"], "turns": []}; report["conversations"].append(record)
        with tempfile.TemporaryDirectory(prefix="semantic-binding-") as tmp:
            env = DialogueEnvironment(Path(tmp) / "db.sqlite", conversation["id"], model)
            initialize(env, conversation.get("extra_warmup", False))
            try:
                for index, turn in enumerate(conversation["turns"], 1):
                    before = state(env); call_start = len(env.client.calls)
                    response = env.http.post("/api/v1/agent/chat", headers=env.headers,
                        json={"text": turn["text"], "date": str(DAY), "thread": env.thread,
                              "request_id": conversation["id"] + ":" + str(index)})
                    result = response.json().get("data") or {"reply": response.text, "overall_status": "http_error"}
                    after = state(env)
                    errors = judge(before, after, result, turn["expect"])
                    row = {"turn": index, "user_text": turn["text"], "understood_goals": result.get("task_plan"),
                           "context_and_model_calls": env.client.calls[call_start:], "result": result,
                           "before": before, "after": after, "actual_changes": {"before_hash": digest(before), "after_hash": digest(after)},
                           "oracle": turn["expect"], "errors": errors, "passed": not errors,
                           "unauthorized_changes": [e for e in errors if e.startswith("unauthorized")]}
                    record["turns"].append(row); save()
                    print(json.dumps({"conversation": conversation["id"], "turn": index, "passed": not errors,
                                      "errors": errors, "reply": result.get("reply")}, ensure_ascii=False), flush=True)
            finally:
                env.close()
    rows = [t for c in report["conversations"] for t in c["turns"]]
    report.update(complete=True, total=len(rows), passed=sum(t["passed"] for t in rows),
                  unauthorized_changes=sum(len(t["unauthorized_changes"]) for t in rows))
    report["pass_rate"] = report["passed"] / len(rows)
    save(); model._client.close()


if __name__ == "__main__": main()
