"""知识库检索评测的门禁：不是“能查到就行”，而是按真实问法量 hit。

评测集在 data/knowledge/eval_queries.json：
- expected_ids：可接受答案（top3 命中任一即可）；
- primary_ids：理想答案（top1 必须命中才算 hit@1）；
- out_of_scope：expected_ids 为空，单独统计误返回，不混进 hit。

这里只锁“检索质量不回退”，不锁具体文案；改 chunk/打分器后必须跑。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from app.knowledge.retriever import search

EVAL = Path(__file__).resolve().parents[1] / "data" / "knowledge" / "eval_queries.json"


def load_cases() -> list[dict]:
    return json.loads(EVAL.read_text(encoding="utf-8"))


def test_eval_set_is_valid() -> None:
    cases = load_cases()
    assert len(cases) >= 80
    assert set(Counter(case["category"] for case in cases)) == {
        "literal", "paraphrase", "safety", "nutrition", "list", "out_of_scope",
    }
    queries = [case["query"] for case in cases]
    assert len(queries) == len(set(queries)), "query 重复会让指标虚高"
    for case in cases:
        assert set(case["primary_ids"]) <= set(case["expected_ids"])
        assert case["category"] != "out_of_scope" or not case["expected_ids"]
        assert case["category"] == "out_of_scope" or case["expected_ids"]


def test_hit_gate(client) -> None:
    cases = [case for case in load_cases() if case["expected_ids"]]
    with client.app.state.session_factory() as db:
        hit1 = hit3 = 0
        for case in cases:
            ids = [hit.chunk.id for hit in search(
                db, case["query"], body_part=case["body_part"], limit=3)]
            hit1 += bool(ids and ids[0] in case["primary_ids"])
            hit3 += any(cid in case["expected_ids"] for cid in ids)
    assert hit3 / len(cases) >= 0.98, f"hit@3 回退：{hit3}/{len(cases)}"
    assert hit1 / len(cases) >= 0.90, f"hit@1 回退：{hit1}/{len(cases)}"


def test_hard_cases_top1(client) -> None:
    """这三条是改 BM25 前 top1 错的典型，锁住防止回退。"""
    with client.app.state.session_factory() as db:
        cases = [
            ("训练前怎么热身", None, "warmup"),
            ("卧推肩膀疼可以换什么动作", "shoulder", "shoulder-swap"),
            ("晚上睡不够会影响训练吗", None, "sleep-recovery"),
        ]
        for query, body_part, expected in cases:
            hits = search(db, query, body_part=body_part, limit=1)
            assert hits and hits[0].chunk.id == expected, (query, [h.chunk.id for h in hits])


def test_unrelated_queries_are_empty(client) -> None:
    with client.app.state.session_factory() as db:
        assert search(db, "今天天气怎么样") == []
        assert search(db, "帮我写一段 Python") == []
