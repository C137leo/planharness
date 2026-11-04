"""检索：中文 bigram 打分 + 元数据过滤（第一版不上向量）。

为什么先不用 embedding：
- 26 条内容，关键词 + 元数据就够用；
- 向量方案要引入 pgvector、模型调用和索引，收益在当前规模下不明显；
- 先把"引用必须真实、无结果必须拒绝"这两件事做对，比换检索算法更重要。
"""

from __future__ import annotations

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


def search(db, query: str, *, body_part: str | None = None,
           topic: str | None = None, limit: int = 3,
           min_score: float = 2.0) -> list[Hit]:
    rows = db.query(KnowledgeChunk).all()
    tokens = tokenize(query)
    hits: list[Hit] = []
    for row in rows:
        if topic and row.topic != topic:
            continue
        value = score(row, tokens, body_part)
        if value >= min_score:
            hits.append(Hit(row, value))
    hits.sort(key=lambda item: (-item.score, item.chunk.title))
    return hits[:limit]
