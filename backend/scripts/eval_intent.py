"""跑意图理解评估集。

用法：
  .venv/bin/python scripts/eval_intent.py --live
  .venv/bin/python scripts/eval_intent.py --live --limit 10

只调用模型做理解，不写任何业务数据。没有 --live 时只打印用例数量。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import intent_router  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.evals.intent_cases import CASES  # noqa: E402
from app.llm.tool_client import tool_client_from_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="真实调用模型")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args()
    cases = CASES[args.offset: (args.offset + args.limit) if args.limit else None]
    if not args.live:
        print(json.dumps({"cases": len(cases), "live": False,
                          "ids": [item["id"] for item in cases]}, ensure_ascii=False, indent=2))
        return 0
    client = tool_client_from_settings(get_settings())
    if not client.available():
        print("LLM 不可用（DEEPSEEK_API_KEY 为空）", file=sys.stderr)
        return 2
    rows = []
    for case in cases:
        result = intent_router.understand(
            client, case["text"], context=case["context"], fallback_intent="clarify",
        )
        intent_ok = result.intent in case["expected_intents"]
        scope_ok = result.scope in case.get("expected_scope", [result.scope])
        slots_ok = True
        missing_detail = []
        for key, expected in (case.get("slot_expect") or {}).items():
            actual = result.slots.get(key)
            if actual != expected:
                slots_ok = False
                missing_detail.append({"slot": key, "expected": expected, "actual": actual})
        rows.append({
            "id": case["id"], "text": case["text"], "intent": result.intent,
            "scope": result.scope, "confidence": round(result.confidence, 2),
            "slots": result.slots, "missing": result.missing_slots,
            "clarify": result.clarification_needed, "error": result.error,
            "intent_ok": intent_ok, "scope_ok": scope_ok, "slots_ok": slots_ok,
            "slot_mismatch": missing_detail,
        })
        mark = "PASS" if (intent_ok and scope_ok and slots_ok) else "FAIL"
        print(f"[{mark}] {case['id']}: {result.intent}/{result.scope} "
              f"conf={result.confidence:.2f} missing={result.missing_slots}")
        if not (intent_ok and scope_ok and slots_ok):
            print("       text:", case["text"])
            print("       slots:", json.dumps(result.slots, ensure_ascii=False),
                  "mismatch:", missing_detail, "err:", result.error[:120])
    passed = sum(1 for row in rows if row["intent_ok"] and row["scope_ok"] and row["slots_ok"])
    print(json.dumps({"passed": passed, "total": len(rows),
                      "accuracy": round(passed / len(rows), 3) if rows else 0}, ensure_ascii=False))
    out = Path("eval_intent_result.json")
    out.write_text(json.dumps({"rows": rows, "passed": passed, "total": len(rows)},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
