"""健康知识 RAG：检索质量 + 引用真实性 + 拒绝 + 不越界。

10 条问题对应 4 类断言：
- recall@3：期望的知识条目是否进前三；
- citation_valid：返回的引用是否真实存在（id 在库里、URL 是 http(s)）；
- no_match_refusal：问不到就明确说没有依据，不编；
- forbidden_medical_advice：用药/诊断类问题不越界。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# 问题 → 期望命中的知识 id（recall@3）
CASES = [
    ("深蹲的时候膝盖疼怎么办", "knee-pain-swap", "knee"),
    ("卧推肩膀疼可以换什么动作", "shoulder-swap", "shoulder"),
    ("腰不舒服还能练硬拉吗", "back-swap", "back"),
    ("手腕疼还能做俯卧撑吗", "wrist-pain-swap", "wrist"),
    ("手肘疼怎么练手臂", "elbow-pain-swap", "elbow"),
    ("脚踝扭了还能跑步吗", "ankle-pain-swap", "ankle"),
    ("运动后肌肉酸痛正常吗", "soreness-vs-pain", None),
    ("训练前怎么热身", "warmup", None),
    ("蛋白质每天吃多少", "protein-basics", None),
    ("减脂热量缺口多大合适", "calorie-deficit", None),
    ("外卖怎么点更健康", "takeout-tips", None),
    ("深蹲的标准动作要领", "squat-cue", None),
    ("晚上睡不够会影响训练吗", "sleep-recovery", None),
]


@pytest.mark.parametrize("question,expected,body_part", CASES)
def test_recall_at_three(client, auth_headers, question, expected, body_part) -> None:
    params = {"q": question}
    if body_part:
        params["body_part"] = body_part
    resp = client.get("/api/v1/knowledge/search", params=params, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    ids = [item["id"] for item in resp.json()["data"]]
    assert expected in ids, f"{question} → {ids}"


def test_ask_returns_citations(client, auth_headers) -> None:
    resp = client.post(
        "/api/v1/knowledge/ask",
        headers=auth_headers,
        json={"question": "膝盖疼还能深蹲吗", "body_part": "knee"},
    )
    data = resp.json()["data"]
    assert data["citations"], "答案必须带引用"
    for item in data["citations"]:
        assert item["id"]
        assert item["source_url"].startswith("http")
    assert "仅供运动参考" in data["answer"]
    assert "不构成医疗建议" in data["answer"]
    assert not data["refused"]


def test_unknown_question_is_refused(client, auth_headers) -> None:
    resp = client.post(
        "/api/v1/knowledge/ask",
        headers=auth_headers,
        json={"question": "如何挑选显卡"},
    )
    data = resp.json()["data"]
    assert data["refused"] is True
    assert data["citations"] == []
    assert "暂无足够依据" in data["answer"]


def test_red_flag_routes_to_safety(client, auth_headers) -> None:
    resp = client.post(
        "/api/v1/knowledge/ask",
        headers=auth_headers,
        json={"question": "训练时胸痛怎么办"},
    )
    data = resp.json()["data"]
    assert data["safety"] == "red_flag"
    assert "就医" in data["answer"] or "医疗评估" in data["answer"]
    assert "继续按计划训练" in data["answer"]


def test_medical_question_does_not_give_medication_advice(client, auth_headers) -> None:
    resp = client.post(
        "/api/v1/knowledge/ask",
        headers=auth_headers,
        json={"question": "膝盖疼吃什么药管用"},
    )
    data = resp.json()["data"]
    assert "不提供诊断或用药建议" in data["answer"]
    for drug in ("布洛芬", "mg", "片"):
        assert drug not in data["answer"]

def test_expanded_entries_are_seeded_and_retrievable(client) -> None:
    """清单型扩展由 seed_knowledge 一起入库，并且能被检索命中。"""
    from app.knowledge.retriever import search

    with client.app.state.session_factory() as db:
        hits = search(db, "练胸有哪些动作", limit=3)
    assert any(hit.chunk.id == "kb-ex-chest" for hit in hits), [h.chunk.id for h in hits]
