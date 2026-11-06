"""三种检索引擎对比评测（id 级人工判定）。

评测集在 data/rag/eval_queries.json：
每条查询都指定了"应该被检索到的 chunk id"（人工选定，脚本按标题解析），
命中 = top-k 里出现任一期望 id。比"标题含关键词"严格，避免冷门变体擦边算对。

用法：
    cd backend && .venv/bin/python scripts/eval_rag.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge import rag  # noqa: E402

EVAL = Path(__file__).resolve().parents[1] / "data" / "rag" / "eval_queries.json"


def load_cases() -> list[dict]:
    return json.loads(EVAL.read_text(encoding="utf-8"))


def evaluate(engine: str, cases: list[dict], k: int = 3) -> dict:
    hit1 = hit3 = 0
    ranks: list[int] = []
    misses: list[tuple[str, str, list[str]]] = []
    by_category: dict[str, list[int]] = {}
    for case in cases:
        hits = rag.search(case["query"], engine=engine, limit=k)
        ids = [hit["id"] for hit in hits]
        rank = next((index + 1 for index, cid in enumerate(ids)
                     if cid in case["expected_ids"]), None)
        by_category.setdefault(case["category"], []).append(rank or 0)
        if rank:
            hit3 += 1
            ranks.append(rank)
            if rank == 1:
                hit1 += 1
        else:
            misses.append((case["category"], case["query"],
                           [hit["title"][:14] for hit in hits]))
    total = max(len(cases), 1)
    mrr = round(sum(1.0 / rank for rank in ranks) / total, 3)
    category_stats = {
        name: {
            "hit@1": round(sum(1 for r in values if r == 1) / len(values), 3),
            "hit@3": round(sum(1 for r in values if r) / len(values), 3),
            "n": len(values),
        }
        for name, values in by_category.items()
    }
    return {"hit@1": round(hit1 / total, 3), "hit@3": round(hit3 / total, 3),
            "mrr": mrr, "misses": misses, "by_category": category_stats}


def main() -> None:
    cases = load_cases()
    stats = rag.stats()
    print("语料：", stats)
    start = time.perf_counter()
    rag.search("深蹲动作要领", engine="hybrid", limit=5)
    print(f"首次检索（含建索引）：{int((time.perf_counter() - start) * 1000)} ms")
    print(f"评测集：{len(cases)} 条（id 级人工判定）")
    print(f"{'引擎':<10}{'hit@1':>8}{'hit@3':>8}{'MRR':>8}")
    results = {}
    for engine in ("keyword", "vector", "hybrid"):
        result = evaluate(engine, cases)
        results[engine] = result
        print(f"{engine:<10}{result['hit@1']:>8.0%}{result['hit@3']:>8.0%}{result['mrr']:>8.3f}")
    print("\n分类别（hybrid）：")
    for name, value in results["hybrid"]["by_category"].items():
        print(f"  {name:<12} hit@1 {value['hit@1']:.0%}  hit@3 {value['hit@3']:.0%}  n={value['n']}")
    misses = results["hybrid"]["misses"]
    if misses:
        print(f"\n未命中 {len(misses)} 条（hybrid）：")
        for category, query, titles in misses[:12]:
            print(f"  ✗ [{category}] {query} -> {titles}")
    else:
        print("\nhybrid 全部命中 top3")


if __name__ == "__main__":
    main()
