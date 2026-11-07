"""检索：BM25（中文 bigram + 单字）+ 元数据加权，不引入向量。

第一版是手写加权词面分，84 条评测后被 BM25 打败（hit@1 82% → 92%），
所以线上默认改成 BM25；旧的 `score()` 保留给 `scripts/eval_knowledge.py`
做基线对比，不再被 `search()` 使用。

为什么仍然不上 embedding：
- 43 条内容，BM25 + 元数据已接近饱和（hit@3 100%）；
- 向量方案要引入 pgvector、模型调用和索引，收益在当前规模下不明显；
- 先把"引用必须真实、无结果必须拒绝"这两件事做对，比换检索算法更重要。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.models import KnowledgeChunk

STOP = set("的了吗呢吧啊我你他她它是有和与或怎么如何什么为什么哪里哪个可以需要应该")
PUNCT = re.compile(r"[\s，。！？、；：“”‘’（）,.!?;:()\[\]【】]+")


@dataclass
class Hit:
    chunk: KnowledgeChunk
    score: float


def tokenize(text: str) -> list[str]:
    """中文按 bigram、英文按单词切。"""
    clean = PUNCT.sub(" ", (text or "").lower())
    tokens: list[str] = []
    for part in clean.split():
        if re.fullmatch(r"[a-z0-9]+", part):
            tokens.append(part)
            continue
        chars = [ch for ch in part if ch not in STOP]
        if len(chars) == 1:
            tokens.append(chars[0])
        for index in range(len(chars) - 1):
            tokens.append(chars[index] + chars[index + 1])
        # 单字也入索引：中文里"睡""膝"这种核心字比 bigram 更稳
        tokens.extend(chars)
    return tokens


def score(chunk: KnowledgeChunk, tokens: list[str], body_part: str | None) -> float:
    """第一版手写加权分：标题 3 / 标签 2 / 正文 1。

    保留它是因为评测需要"改前 vs 改后"的对照；线上 `search()` 已经不用它。
    """
    title = (chunk.title or "").lower()
    content = (chunk.content or "").lower()
    tags = " ".join(chunk.tags or [])
    value = 0.0
    for token in tokens:
        if token in title:
            value += 3.0
        if token in tags or token in (chunk.topic or ""):
            value += 2.0
        if token in content:
            value += 1.0
    if body_part and chunk.body_part in (body_part, "general"):
        value += 2.0 if chunk.body_part == body_part else 0.5
    return value


BM25_K1 = 1.5
BM25_B = 0.75
# 84 条评测里，阈值 6.0 不损失 hit@1/hit@3，同时挡掉一部分完全无关的问题；
# 再往上调会开始误伤"腿怎么练"这类短查询。
BM25_MIN_SCORE = 6.0


def _bm25_hits(rows: list[KnowledgeChunk], tokens: list[str],
               body_part: str | None, topic: str | None,
               min_score: float, limit: int) -> list[Hit]:
    if not tokens:
        return []
    # 标题重复一次：43 条语料里标题就是最强的意图信号，BM25 本身不区分字段
    docs = [
        tokenize(" ".join([row.title or "", row.title or "",
                           row.content or "", " ".join(row.tags or [])]))
        for row in rows
    ]
    total = len(rows)
    df: dict[str, int] = {}
    for doc in docs:
        for token in set(doc):
            df[token] = df.get(token, 0) + 1
    idf = {
        token: math.log(1 + (total - count + 0.5) / (count + 0.5))
        for token, count in df.items()
    }
    avg_len = sum(len(doc) for doc in docs) / max(total, 1)
    hits: list[Hit] = []
    for row, doc in zip(rows, docs):
        if topic and row.topic != topic:
            continue
        counts: dict[str, int] = {}
        for token in doc:
            counts[token] = counts.get(token, 0) + 1
        value = 0.0
        for token in tokens:
            freq = counts.get(token)
            if not freq:
                continue
            denom = freq + BM25_K1 * (1 - BM25_B + BM25_B * len(doc) / max(avg_len, 1))
            value += idf.get(token, 0.0) * freq * (BM25_K1 + 1) / denom
        if value <= 0:
            continue
        if body_part and row.body_part in (body_part, "general"):
            value += 2.0 if row.body_part == body_part else 0.5
        if value >= min_score:
            hits.append(Hit(row, value))
    hits.sort(key=lambda item: (-item.score, item.chunk.title))
    return hits[:limit]


def search(db, query: str, *, body_part: str | None = None,
           topic: str | None = None, limit: int = 3,
           min_score: float = BM25_MIN_SCORE) -> list[Hit]:
    rows = db.query(KnowledgeChunk).all()
    return _bm25_hits(rows, tokenize(query), body_part, topic, min_score, limit)
