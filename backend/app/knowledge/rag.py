"""外部语料 RAG：free-exercise-db + USDA 的检索层（可与现有 27 条知识库并存）。

为什么要单独一层：
- 现有 `retriever.py` 只服务"自建 27 条 + 元数据过滤"，语料小、规则清晰；
- 外部语料（几十到几千条）需要能对比"关键词 vs 向量 vs 混合"，
  不能为了做实验去改线上检索路径。

三种引擎：
- `keyword`：BM25（中文 bigram + 英文单词）
- `vector`：特征哈希向量（离线、无外部依赖，**不是语义模型**）+ 余弦
- `hybrid`：RRF 融合两路排名（默认）

诚实说明：这里的 vector 是 hash-ngram 向量，能证明"向量检索通路 + 排序融合"可跑，
但**不等于**句向量语义检索。换成真 embedding 时只需替换 `embed()`。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.knowledge.retriever import tokenize

CHUNKS_PATH = Path(os.environ.get(
    "RAG_CHUNKS_PATH",
    Path(__file__).resolve().parents[2] / "data" / "rag" / "chunks.jsonl",
))
EMBED_DIM = 512
_CACHE: dict = {"mtime": 0.0, "chunks": [], "lex": None, "vec": None}


@dataclass
class RagHit:
    chunk: dict
    score: float

    def as_dict(self) -> dict:
        item = dict(self.chunk)
        item["score"] = round(self.score, 4)
        return item


def load_chunks() -> list[dict]:
    """按 mtime 缓存：脚本重建语料后不用重启进程。"""
    if not CHUNKS_PATH.exists():
        return []
    mtime = CHUNKS_PATH.stat().st_mtime
    if mtime != _CACHE["mtime"]:
        chunks = []
        for line in CHUNKS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                chunks.append(json.loads(line))
        _CACHE.update({"mtime": mtime, "chunks": chunks, "lex": None, "vec": None})
        _CACHE["lex"] = _build_lexical(chunks)
        _CACHE["vec"] = [_embed(_text_of(item)) for item in chunks]
        _CACHE["token_sets"] = [set(tokens) for tokens in _CACHE["lex"]["docs"]]
    return _CACHE["chunks"]


def _text_of(item: dict) -> str:
    return " ".join([str(item.get("title") or ""), str(item.get("content") or ""),
                     " ".join(str(tag) for tag in item.get("tags") or [])])


# 查询里的"部位/品类"意图：词面之外再加一档，否则"下肢力量"会被上肢动作抢走
_BODY_INTENT = {
    "legs": ("下肢", "腿", "深蹲", "弓步", "腿举", "小腿"),
    "hip": ("臀", "髋"),
    "chest": ("胸", "卧推", "飞鸟", "俯卧撑"),
    "back": ("背", "划船", "下拉", "引体", "硬拉"),
    "shoulder": ("肩", "推举", "侧平举", "面拉"),
    "arm": ("手臂", "弯举", "臂屈伸", "三头", "二头"),
    "core": ("核心", "腹", "平板", "卷腹"),
}
_FOOD_CATEGORY = {
    "主食": ("主食", "碳水", "米饭", "燕麦", "红薯", "土豆", "面包"),
    "蛋白质来源": ("蛋白质", "蛋白", "肉", "鸡胸", "牛肉", "鱼", "虾", "鸡蛋"),
    "蔬菜": ("蔬菜", "西兰花", "菠菜", "菜"),
    "水果": ("水果", "香蕉", "苹果", "莓"),
    "坚果": ("坚果", "杏仁", "花生", "核桃"),
    "奶制品": ("牛奶", "酸奶", "奶酪", "奶"),
    "油脂": ("油", "脂肪"),
}


def _intent_boost(chunk: dict, query: str) -> float:
    text = query.lower()
    boost = 0.0
    body_part = str(chunk.get("body_part") or "")
    for part, words in _BODY_INTENT.items():
        if any(word in text for word in words):
            if body_part == part:
                boost += 4.0
            elif body_part in ("legs", "hip") and part in ("legs", "hip"):
                boost += 2.0
            break
    tags = " ".join(str(tag) for tag in chunk.get("tags") or [])
    for category, words in _FOOD_CATEGORY.items():
        if any(word in text for word in words) and category in tags:
            boost += 3.0
            break
    return boost


# ---------------------------------------------------------------- 词面向量（BM25）

def _build_lexical(chunks: list[dict]) -> dict:
    docs = [tokenize(_text_of(item)) for item in chunks]
    df: dict[str, int] = {}
    for tokens in docs:
        for token in set(tokens):
            df[token] = df.get(token, 0) + 1
    avg_len = sum(len(tokens) for tokens in docs) / max(len(docs), 1)
    total = len(docs)
    idf = {token: math.log(1 + (total - count + 0.5) / (count + 0.5))
           for token, count in df.items()}
    title_sets = [set(tokenize(str(item.get("title") or ""))) for item in chunks]
    return {"docs": docs, "idf": idf, "avg_len": avg_len, "title_sets": title_sets}


def _bm25(query: str, limit: int) -> list[RagHit]:
    chunks = load_chunks()
    if not chunks:
        return []
    lex = _CACHE["lex"]
    q_tokens = tokenize(query)
    if not q_tokens:
        return []
    k1, b = 1.5, 0.75
    scored: list[RagHit] = []
    for index, tokens in enumerate(lex["docs"]):
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        score = 0.0
        for token in q_tokens:
            if token not in counts:
                continue
            freq = counts[token]
            denom = freq + k1 * (1 - b + b * len(tokens) / max(lex["avg_len"], 1))
            score += lex["idf"].get(token, 0.0) * freq * (k1 + 1) / denom
        # 一个词都没命中就不参与（否则下面的加权会让每个查询都返回"策展条目"）
        if score <= 0:
            continue
        # 标题命中加权：语料变大后，"高脚杯深蹲"会被一堆"XX深蹲"压到后面，
        # 标题里直接出现查询词应当明显更相关
        title_hits = {token for token in q_tokens if token in lex["title_sets"][index]}
        if title_hits:
            score += 1.2 * len(title_hits)
        if query.strip() and query.strip().lower() in chunks[index]["title"].lower():
            score += 3.0
        # 策展条目（人工确认过的 43 动作 + 32 食物）加权：
        # 语料一大，"杰斐逊深蹲"这种冷门变体会和"杠铃深蹲"抢位置。
        # 权重给得比较重（+6），因为这是一次明确的"质量优先于覆盖率"的取舍。
        if (chunks[index].get("meta") or {}).get("priority"):
            score += 6.0
        score += _intent_boost(chunks[index], query)
        if score > 0:
            scored.append(RagHit(chunks[index], score))
    scored.sort(key=lambda hit: -hit.score)
    return scored[:limit]


# ---------------------------------------------------------------- 哈希向量（离线近似）

def _tokens_for_vector(text: str) -> list[str]:
    tokens = tokenize(text)
    for part in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(part) > 1:
            tokens.append(part)
    return tokens


def _embed(text: str) -> list[float]:
    vector = [0.0] * EMBED_DIM
    for token in _tokens_for_vector(text):
        digest = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
        vector[digest % EMBED_DIM] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _vector_search(query: str, limit: int) -> list[RagHit]:
    """哈希向量的余弦检索。

    加了一道"词面重叠"门槛：特征哈希会发生碰撞，纯随机词也能算出 0.2+ 的余弦
    （实测 `zzzzqqqq` 会命中"香蕉"）。不设门槛，向量路会把任何查询都当有结果，
    融合后反而污染关键词结果。
    """
    chunks = load_chunks()
    if not chunks:
        return []
    query_tokens = set(tokenize(query))
    if not query_tokens:
        return []
    query_vec = _embed(query)
    scored: list[RagHit] = []
    for index, doc_vec in enumerate(_CACHE["vec"]):
        if not (query_tokens & _CACHE["token_sets"][index]):
            continue
        score = sum(a * b for a, b in zip(query_vec, doc_vec))
        if score > 0:
            scored.append(RagHit(chunks[index], score))
    scored.sort(key=lambda hit: -hit.score)
    return scored[:limit]


# ---------------------------------------------------------------- 混合（RRF）

def _rrf(rankings: list[list[RagHit]], limit: int, k: int = 60) -> list[RagHit]:
    merged: dict[str, tuple[dict, float]] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            chunk_id = hit.chunk["id"]
            chunk, score = merged.get(chunk_id, (hit.chunk, 0.0))
            bonus = 0.03 if (hit.chunk.get("meta") or {}).get("priority") else 0.0
            merged[chunk_id] = (chunk, score + 1.0 / (k + rank) + bonus)
    hits = [RagHit(chunk, score) for chunk, score in merged.values()]
    hits.sort(key=lambda hit: -hit.score)
    return hits[:limit]


def search(query: str, *, engine: str = "hybrid", limit: int = 5) -> list[dict]:
    """对外接口：返回 chunk 字典 + score。engine ∈ keyword/vector/hybrid。"""
    engine = (engine or "hybrid").lower()
    if engine == "keyword":
        return [hit.as_dict() for hit in _bm25(query, limit)]
    if engine == "vector":
        return [hit.as_dict() for hit in _vector_search(query, limit)]
    pool = max(limit * 3, 10)
    return [hit.as_dict() for hit in _rrf(
        [_bm25(query, pool), _vector_search(query, pool)], limit)]


def stats() -> dict:
    chunks = load_chunks()
    kinds: dict[str, int] = {}
    for item in chunks:
        kinds[item.get("kind") or "other"] = kinds.get(item.get("kind") or "other", 0) + 1
    return {"chunks": len(chunks), "kinds": kinds, "path": str(CHUNKS_PATH)}
