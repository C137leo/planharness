"""Run stateful, sealed conversation suites through the real authenticated API."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.evals.dialogue_runner import DialogueEnvironment, DAY, REFERENCE_DAY, digest, judge
from app.llm.tool_client import tool_client_from_settings



def explore(settings, output):
    """Adaptive role-play: next messages arrive only after observing prior replies."""
    model = tool_client_from_settings(settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"mode": "live_adaptive_exploration", "model": model.model,
              "automated_pass_rate": None, "turns": []}
    with tempfile.TemporaryDirectory(prefix="coach-exploration-") as tmp:
        env = DialogueEnvironment(Path(tmp) / "isolated.db", "adaptive-exploration", model)
        try:
            print(json.dumps({"ready": True, "initial_state": env.snapshot()}, ensure_ascii=False), flush=True)
            for index, line in enumerate(sys.stdin, 1):
                if line.strip() == ":stop":
                    break
                text = json.loads(line)["text"]
                row = env.step(text, index)
                report["turns"].append(row)
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
                print(json.dumps({"turn": index, "text": text, "reply": row["reply"],
                                  "task_results": row["result"].get("task_results"),
                                  "changes": row["actual_data_changes"]}, ensure_ascii=False), flush=True)
        finally:
            env.close()
            model._client.close()
    return 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=3)
    args = parser.parse_args()
    suite = json.loads(args.suite.read_text())
    settings = Settings()
    probe = tool_client_from_settings(settings)
    configured = probe.available()
    probe._client.close()
    if not args.live or not configured:
        print("Use --live with a configured model; no synthetic NLU score is produced.")
        return 2
    if args.interactive:
        return explore(settings, args.output)
    code_paths = [p for p in Path("app").rglob("*.py") if "evals" not in p.parts]
    report = {"suite_id": suite["id"], "split": suite["split"], "mode": "live",
              "suite_sha256": hashlib.sha256(args.suite.read_bytes()).hexdigest(),
              "code_sha256": digest({str(p): p.read_text() for p in sorted(code_paths)}),
              "model": settings.deepseek_model, "evaluation_date": DAY.isoformat(),
              "oracle_reference_date": REFERENCE_DAY.isoformat(), "conversations": [], "complete": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.RLock()

    def save():
        with lock:
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    def conversation_run(conversation):
        model = tool_client_from_settings(settings)
        record = {"id": conversation["id"], "turns": []}
        with lock:
            report["conversations"].append(record)
        try:
            with tempfile.TemporaryDirectory(prefix="coach-dialogue-") as tmp:
                env = DialogueEnvironment(Path(tmp) / "isolated.db", conversation["id"], model)
                try:
                    for index, turn in enumerate(conversation["turns"], 1):
                        # No expected intent/tool, oracle, tags, or future utterances
                        # ever enter the actor. Turns within a conversation are serial.
                        row = env.step(turn["text"], index)
                        row["evaluation_tags"] = turn.get("tags") or []
                        judge(row, turn["expect"])
                        with lock:
                            record["turns"].append(row)
                            save()
                        print(json.dumps({"conversation": conversation["id"], "turn": index,
                                          "passed": row["passed"], "failures": row["failures"]},
                                         ensure_ascii=False), flush=True)
                finally:
                    env.close()
        except Exception as exc:
            with lock:
                record["infrastructure_error"] = type(exc).__name__
                save()
        finally:
            model._client.close()

    save()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(conversation_run, suite["conversations"]))
    rows = [t for c in report["conversations"] for t in c["turns"]]
    expected_turns = sum(len(c["turns"]) for c in suite["conversations"])
    available = sum(r.get("model_available", False) for r in rows)
    passed = sum(r["passed"] for r in rows)
    blocked = available < expected_turns
    conversation_passed = sum(len(c["turns"]) == len(next(x for x in suite["conversations"] if x["id"] == c["id"])["turns"])
                              and all(t["passed"] for t in c["turns"]) for c in report["conversations"])
    report["summary"] = {"model_available_turns": available, "expected_turns": expected_turns,
                         "evaluation_blocked": blocked, "turns_passed": passed, "turns_total": len(rows),
                         "turn_pass_rate": passed / expected_turns if not blocked else None,
                         "conversations_passed": conversation_passed,
                         "conversations_total": len(suite["conversations"]),
                         "conversation_pass_rate": conversation_passed / len(suite["conversations"]) if not blocked else None,
                         "scope_violations": sum(bool(r["unexpected_changes"]) for r in rows),
                         "unauthorized_mutation_rate": sum(bool(r["unexpected_changes"]) for r in rows) / len(rows) if rows else None}
    report["complete"] = len(rows) == expected_turns
    save()
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    return 2 if blocked else 0 if passed == expected_turns else 1


if __name__ == "__main__":
    raise SystemExit(main())
