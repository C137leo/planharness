"""外部语料 RAG：语料能加载、三种引擎都能返回带出处的条目。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.knowledge import rag


def test_corpus_has_both_kinds() -> None:
    stats = rag.stats()
    assert stats["chunks"] >= 60
    assert stats["kinds"].get("exercise", 0) >= 40
    assert stats["kinds"].get("food", 0) >= 25


def test_keyword_hits_squat() -> None:
    hits = rag.search("深蹲动作要领", engine="keyword", limit=3)
    assert hits
    assert any("深蹲" in hit["title"] for hit in hits)
    assert hits[0]["source_url"].startswith("https://")


def test_vector_scores_are_normalised() -> None:
    hits = rag.search("鸡胸肉蛋白质", engine="vector", limit=3)
    assert hits
    assert all(0 <= hit["score"] <= 1.0001 for hit in hits)


def test_hybrid_returns_source_and_license() -> None:
    hits = rag.search("杠铃卧推", engine="hybrid", limit=3)
    assert hits
    for hit in hits:
        assert hit["license"] in {"Unlicense", "CC0-1.0"}
        assert hit["source_title"]
        assert hit["source_url"].startswith("https://")


def test_unknown_query_does_not_crash() -> None:
    # 纯 ASCII 生僻词：token 无法命中任何条目，应返回空列表而不是报错
    assert rag.search("zzzzqqqq", engine="hybrid", limit=3) == []
    # 中文查询即使生僻也会有单字/双字重合，允许返回结果，只要不抛异常
    assert isinstance(rag.search("完全不存在的关键词", engine="keyword", limit=3), list)


def test_rag_endpoint_requires_login(client: TestClient) -> None:
    assert client.get("/api/v1/knowledge/rag", params={"q": "深蹲"}).status_code == 401


def test_rag_endpoint_returns_hits(client: TestClient, auth_headers) -> None:
    body = client.get("/api/v1/knowledge/rag",
                      params={"q": "深蹲动作要领", "engine": "hybrid"},
                      headers=auth_headers).json()["data"]
    assert body["engine"] == "hybrid"
    assert body["hits"]
    assert body["stats"]["chunks"] >= 60

def test_eval_set_ids_exist_in_corpus() -> None:
    """评测集是人工判定：里面的期望 id 必须真实存在，否则评测结果没有意义。"""
    import json
    from pathlib import Path

    eval_path = Path(__file__).resolve().parents[1] / "data" / "rag" / "eval_queries.json"
    cases = json.loads(eval_path.read_text(encoding="utf-8"))
    assert len(cases) == 60
    ids = {chunk["id"] for chunk in rag.load_chunks()}
    for case in cases:
        assert case["expected_ids"], case["query"]
        assert set(case["expected_ids"]) <= ids, case["query"]
    categories = {case["category"] for case in cases}
    assert categories == {"literal", "paraphrase", "food"}
