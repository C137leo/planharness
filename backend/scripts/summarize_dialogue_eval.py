"""Outcome metrics from saved real trajectories, without re-querying any model."""
import argparse
import json
from pathlib import Path
import statistics


def ratio(passed, total, basis):
    return {"passed": passed, "total": total, "rate": passed / total if total else None, "basis": basis}


def summarize(report, suite):
    expected = {(c["id"], i): t for c in suite["conversations"] for i, t in enumerate(c["turns"], 1)}
    rows = [(row, expected[(c["id"], row["turn"])]) for c in report["conversations"] for row in c["turns"]]
    refs = [(r, e) for r, e in rows if e["expect"].get("reference_to_previous")]
    corrections = [(r, e) for r, e in rows if set(e.get("tags", [])) & {"correction", "undo", "undo_latest", "undo_earlier"}]
    multi = [(r, e) for r, e in rows if set(e.get("tags", [])) & {"multi_task", "partial_success"}]
    domain = []
    for row, exp in rows:
        allowed = exp["expect"].get("allowed_changes", [])
        wanted = set()
        if any("/actions" in path for path in allowed):
            wanted.add("training")
        if any("/meals" in path for path in allowed):
            wanted.add("nutrition")
        if not wanted:
            continue
        actual = {t.get("domain") for t in row["result"].get("task_plan", [])
                  if t.get("domain") in {"training", "nutrition"} and t.get("execution_mode", "execute") == "execute"}
        for change in row["actual_data_changes"]:
            if "/actions/" in change["path"]:
                actual.add("training")
            if "/meals/" in change["path"]:
                actual.add("nutrition")
        domain.append(actual == wanted)
    mutation_ops = {"skip_action", "replace_action", "remove_action", "add_action", "change_sets", "change_reps",
                    "add_meal_item", "remove_meal_item", "replace_meal_item", "modify_meal_plan", "undo_task", "regenerate_training_today"}
    mutation_results = []
    for row, _ in rows:
        plan = {t["task_id"]: t for t in row["result"].get("task_plan", [])}
        mutation_results.extend(t for t in row["result"].get("task_results", [])
                                if plan.get(t["task_id"], {}).get("operation") in mutation_ops and
                                plan[t["task_id"]].get("execution_mode", "execute") == "execute")
    metrics = {
        "Domain Accuracy": {"rate": None, "basis": "No independent semantic-domain annotations; authorized effect paths are not equivalent to user-domain gold labels"},
        "Mutation-domain coverage proxy": ratio(sum(domain), len(domain), "Planned/observed domains compared with permitted effect paths; diagnostic only, not semantic accuracy"),
        "Goal Accuracy": {"rate": None, "basis": "No independent semantic-goal annotations; use objective Goal Completion, not an invented label score"},
        "Context Resolution": ratio(sum(r["passed"] for r, _ in refs), len(refs), "Reference turns satisfy concrete state and scope assertions with real history"),
        "Scope Accuracy": ratio(sum(not r["unexpected_changes"] for r, _ in rows), len(rows), "No business state changes outside authorized paths; no-op can be scope-safe but fail goal"),
        "Task Planning Accuracy (compound subset)": ratio(sum(r["passed"] and len(r["result"].get("task_results", [])) >= 2 for r, _ in multi), len(multi), "Compound goals retain independent results and meet postconditions"),
        "Tool Selection Accuracy": {"rate": None, "basis": "Tools and arguments recorded, but no independently labelled ideal-tool oracle"},
        "Execution Success": ratio(sum(t["status"] == "success" and (t.get("verification") or {}).get("passed") is True for t in mutation_results), len(mutation_results), "Conditional on actually planned execute-mode mutations; requires state reload verification"),
        "Goal Completion": ratio(sum(r["passed"] for r, _ in rows), len(rows), "All persisted postconditions, scope checks, statuses, availability, and audit requirements pass"),
        "Unauthorized Mutation Rate": ratio(sum(bool(r["unexpected_changes"]) for r, _ in rows), len(rows), "Violating turns / all completed turns; lower is better"),
        "Correction Success": ratio(sum(r["passed"] for r, _ in corrections), len(corrections), "Correction/undo-tagged turns meet concrete state/scope assertions"),
        "Conversation Success": ratio(sum(len(c["turns"]) == len(next(s for s in suite["conversations"] if s["id"] == c["id"])["turns"]) and all(t["passed"] for t in c["turns"]) for c in report["conversations"]), len(suite["conversations"]), "Every turn in a conversation passes; cascade failures remain failures"),
    }
    latencies = [r["result"].get("latency_ms", 0) for r, _ in rows]
    usages = [call.get("response", {}).get("usage", {}) for r, _ in rows for call in r["model_calls"]]
    return {"suite_id": report["suite_id"], "mode": report["mode"], "model": report["model"],
            "suite_sha256": report["suite_sha256"], "code_sha256": report["code_sha256"],
            "metrics": metrics, "model_calls": len(usages),
            "prompt_tokens": sum(u.get("prompt_tokens", 0) for u in usages),
            "completion_tokens": sum(u.get("completion_tokens", 0) for u in usages),
            "latency_ms_median": statistics.median(latencies) if latencies else None,
            "latency_ms_p95": sorted(latencies)[max(0, int(len(latencies)*0.95)-1)] if latencies else None,
            "note": "Rates describe this synthetic suite and model run. They are not population accuracy; failures can cascade across turns."}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--suite", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    output = summarize(json.loads(args.input.read_text()), json.loads(args.suite.read_text()))
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(json.dumps(output, ensure_ascii=False))
