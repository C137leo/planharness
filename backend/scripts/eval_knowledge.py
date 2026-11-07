"""知识库检索评测：/knowledge/search 到底能不能打。

这不是"再拍一个 13 条 smoke test"，而是按真实问法建 84 条评测集：
- literal 18：术语/标题级问法；
- paraphrase 20：用户换说法、不照标题说；
- safety 12：同部位不同严重程度（替代 vs 红旗）；
- nutrition 14：结论 + 清单，测"吃多少"和"吃什么"会不会串台；
- list 10：清单型问题；
- out_of_scope 10：知识库没有答案时，测会不会硬返回。

指标：
- hit@1：top1 是否命中 primary_ids（理想答案）；
- hit@3：top3 是否命中 expected_ids（可接受答案）；
- MRR：首个可接受答案排名的倒数均值；
- 无答案误返回率：out_of_scope 中 search 仍返回结果的比例。

对照：
- legacy_weighted：第一版手写加权分（无阈值，纯排序，作为 before）；
- deployed_bm25+thr：当前线上 BM25 + min_score=6.0（after）；
- bm25：同语料、同分词的标准 BM25；
- bm25_meta：BM25 + body_part 加分；
- bm25_title：BM25 + 标题重复一次 + body_part 加分（线上采用的排序）。

用法：
    cd backend && .venv/bin/python scripts/eval_knowledge.py
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import Base, make_engine, make_session_factory  # noqa: E402
from app.knowledge.retriever import score as weighted_score  # noqa: E402
from app.knowledge.retriever import search as deployed_search  # noqa: E402
from app.knowledge.retriever import tokenize  # noqa: E402
from app.knowledge.service import seed_knowledge  # noqa: E402
from app.models import KnowledgeChunk  # noqa: E402

EVAL = ROOT / "data" / "knowledge" / "eval_queries.json"


def load_cases() -> list[dict]:
    return json.loads(EVAL.read_text(encoding="utf-8"))


def seed_rows():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    db = factory()
    seed_knowledge(db)
    return db, db.query(KnowledgeChunk).all()


def weighted_rank(rows, query: str, body_part: str | None, limit: int = 3) -> list[str]:
    tokens = tokenize(query)
    scored = [(weighted_score(row, tokens, body_part), row) for row in rows]
    scored = [(value, row) for value, row in scored if value > 0]
    scored.sort(key=lambda item: (-item[0], item[1].title))
    return [row.id for _, row in scored[:limit]]


class BM25:
    def __init__(self, rows, *, title_repeat: int = 0, meta: bool = False):
        self.rows = rows
        self.meta = meta
        self.docs = [
            tokenize(" ".join([row.title] * (1 + title_repeat)
                              + [row.content, " ".join(row.tags or [])]))
            for row in rows
        ]
        self.df: dict[str, int] = {}
        for tokens in self.docs:
            for token in set(tokens):
                self.df[token] = self.df.get(token, 0) + 1
        total = len(self.rows)
        self.idf = {
            token: math.log(1 + (total - count + 0.5) / (count + 0.5))
            for token, count in self.df.items()
        }
        self.avg_len = sum(len(tokens) for tokens in self.docs) / max(total, 1)

    def rank(self, query: str, body_part: str | None, limit: int = 3) -> list[str]:
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        k1, b = 1.5, 0.75
        scored = []
        for row, tokens in zip(self.rows, self.docs):
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            value = 0.0
            for token in q_tokens:
                freq = counts.get(token)
                if not freq:
                    continue
                denom = freq + k1 * (1 - b + b * len(tokens) / max(self.avg_len, 1))
                value += self.idf.get(token, 0.0) * freq * (k1 + 1) / denom
            if value <= 0:
                continue
            if self.meta and body_part:
                if row.body_part == body_part:
                    value += 2.0
                elif row.body_part == "general":
                    value += 0.5
            scored.append((value, row))
        scored.sort(key=lambda item: (-item[0], item[1].title))
        return [row.id for _, row in scored[:limit]]


def summarize(name: str, records: list[dict]) -> dict:
    total = len(records)
    hit1 = sum(1 for item in records if item["hit1"])
    hit3 = sum(1 for item in records if item["rank"])
    mrr = sum(1 / item["rank"] for item in records if item["rank"]) / max(total, 1)
    by_cat = defaultdict(list)
    for item in records:
        by_cat[item["category"]].append(item)
    categories = {
        cat: {
            "n": len(items),
            "hit@1": round(sum(1 for i in items if i["hit1"]) / len(items), 3),
            "hit@3": round(sum(1 for i in items if i["rank"]) / len(items), 3),
        }
        for cat, items in by_cat.items()
    }
    return {
        "name": name,
        "n": total,
        "hit@1": round(hit1 / total, 3),
        "hit@3": round(hit3 / total, 3),
        "mrr": round(mrr, 3),
        "by_category": categories,
    }


def run_method(name: str, cases: list[dict], get_ids) -> dict:
    records = []
    for case in cases:
        if not case["expected_ids"]:
            continue
        ids = get_ids(case)
        rank = next((index + 1 for index, cid in enumerate(ids)
                     if cid in case["expected_ids"]), None)
        records.append({
            "query": case["query"],
            "category": case["category"],
            "expected": case["expected_ids"],
            "ids": ids,
            "rank": rank,
            "hit1": bool(ids and ids[0] in case["primary_ids"]),
        })
    return summarize(name, records)


def main() -> None:
    cases = load_cases()
    db, rows = seed_rows()
    known = {row.id for row in rows}
    missing = sorted({cid for case in cases for cid in case["expected_ids"] if cid not in known})
    if missing:
        raise SystemExit(f"评测集引用了不存在的 chunk: {missing}")

    answerable = [case for case in cases if case["expected_ids"]]
    out_of_scope = [case for case in cases if not case["expected_ids"]]
    print(f"知识库：{len(rows)} 条；评测：{len(cases)} 条 "
          f"({dict(Counter(case['category'] for case in cases))})")
    print(f"可回答 {len(answerable)} 条，无答案 {len(out_of_scope)} 条\n")

    bm25 = BM25(rows)
    bm25_meta = BM25(rows, meta=True)
    bm25_title = BM25(rows, title_repeat=1, meta=True)

    methods = {
        "legacy_weighted": lambda case: weighted_rank(rows, case["query"], case["body_part"]),
        "deployed_bm25+thr": lambda case: [
            hit.chunk.id for hit in deployed_search(
                db, case["query"], body_part=case["body_part"], limit=3)
        ],
        "bm25": lambda case: bm25.rank(case["query"], case["body_part"]),
        "bm25_meta": lambda case: bm25_meta.rank(case["query"], case["body_part"]),
        "bm25_title": lambda case: bm25_title.rank(case["query"], case["body_part"]),
    }

    results = {}
    print(f"{'方法':<20}{'hit@1':>8}{'hit@3':>8}{'MRR':>8}")
    for name, get_ids in methods.items():
        result = run_method(name, answerable, get_ids)
        results[name] = result
        print(f"{name:<20}{result['hit@1']:>8.0%}{result['hit@3']:>8.0%}{result['mrr']:>8.3f}")

    print("\n分类别（deployed_bm25+thr）：")
    for category, value in results["deployed_bm25+thr"]["by_category"].items():
        print(f"  {category:<12} hit@1 {value['hit@1']:.0%}  hit@3 {value['hit@3']:.0%}  n={value['n']}")

    print("\n分类别（bm25_title）：")
    for category, value in results["bm25_title"]["by_category"].items():
        print(f"  {category:<12} hit@1 {value['hit@1']:.0%}  hit@3 {value['hit@3']:.0%}  n={value['n']}")

    # 失败样例：用线上真实行为，方便定位
    print("\n未进 top3（deployed_bm25+thr）：")
    misses = 0
    for case in answerable:
        ids = [hit.chunk.id for hit in deployed_search(
            db, case["query"], body_part=case["body_part"], limit=3)]
        if not any(cid in case["expected_ids"] for cid in ids):
            misses += 1
            print(f"  ✗ [{case['category']}] {case['query']} 期望={case['expected_ids']} 实际={ids}")
    if not misses:
        print("  无")

    print("\n无答案问题的误返回（deployed_bm25+thr）：")
    false_positive = 0
    for case in out_of_scope:
        hits = deployed_search(db, case["query"], body_part=case["body_part"], limit=3)
        if hits:
            false_positive += 1
            print(f"  ! {case['query']} -> {[hit.chunk.id for hit in hits]}")
        else:
            print(f"  ✓ {case['query']} -> 拒绝")
    print(f"误返回率：{false_positive}/{len(out_of_scope)} = "
          f"{false_positive / max(len(out_of_scope), 1):.0%}")


if __name__ == "__main__":
    main()
