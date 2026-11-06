"""知识库出处：没有机构出处的条目不许进库。

这些断言不是为了"证明内容对"，而是保证"每条都能被追溯"：
字段齐全、id 唯一、链接是 https 且来自机构站点。
"""

from __future__ import annotations

import pytest

from app.knowledge.provenance import check_offline, is_root_url
from app.knowledge.seeds import SEEDS


def test_every_seed_has_verifiable_source() -> None:
    problems, _warnings = check_offline(SEEDS)
    assert problems == []


def test_ids_are_unique_and_content_is_meaningful() -> None:
    ids = [item["id"] for item in SEEDS]
    assert len(ids) == len(set(ids))
    assert all(len(item["content"]) >= 20 for item in SEEDS)


def test_sources_are_institutional() -> None:
    hosts = {item["source_url"].split("/")[2] for item in SEEDS}
    assert hosts <= {
        "www.cnsoc.org", "www.acsm.org", "www.nhs.uk", "www.mayoclinic.org",
        "www.who.int", "pmc.ncbi.nlm.nih.gov",
    }


def test_protein_claims_are_split_by_source() -> None:
    """一般人群与训练人群的推荐量出处不同，必须拆成两条，不能混引。"""
    by_id = {item["id"]: item for item in SEEDS}
    assert by_id["protein-basics"]["source_url"].endswith("cnsoc.org/")
    assert "pmc.ncbi.nlm.nih.gov" in by_id["protein-training"]["source_url"]
    assert "1.4" in by_id["protein-training"]["content"]


def test_missing_source_is_rejected() -> None:
    bad = [{"id": "x", "title": "t", "content": "这是一条足够长的测试内容，用来验证出处校验逻辑是否按预期工作",
            "topic": "safety", "source_title": "", "source_url": ""}]
    problems, _ = check_offline(bad)
    assert any("source_title" in line for line in problems)
    assert any("source_url" in line for line in problems)


def test_blog_source_is_rejected() -> None:
    bad = [{"id": "x", "title": "t", "content": "这是一条足够长的测试内容，用来验证出处校验逻辑是否按预期工作",
            "topic": "safety", "source_title": "某博客",
            "source_url": "https://example-blog.com/post"}]
    problems, _ = check_offline(bad)
    assert any("白名单" in line for line in problems)


@pytest.mark.parametrize("url", [
    "https://www.cnsoc.org/",
    "https://www.who.int",
])
def test_root_urls_are_warned_not_failed(url: str) -> None:
    """首页只能证明机构存在，证明不了这条结论；只提醒、不拦。"""
    assert is_root_url(url) is True
    good = [{"id": "x", "title": "t", "content": "这是一条足够长的测试内容，用来验证出处校验逻辑是否按预期工作",
             "topic": "safety", "source_title": "WHO", "source_url": url}]
    problems, warnings = check_offline(good)
    assert problems == []
    assert any("首页" in line for line in warnings)


def test_specific_page_is_not_flagged(url: str = "https://pmc.ncbi.nlm.nih.gov/articles/PMC5477153/") -> None:
    assert is_root_url(url) is False

def test_expanded_generated_entries_are_valid() -> None:
    """从 600 条外部语料生成的"清单型"条目也要过同一套出处校验。"""
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "data" / "knowledge" / "expanded.json"
    if not path.exists():
        pytest.skip("expanded.json 未生成")
    entries = json.loads(path.read_text(encoding="utf-8"))
    problems, _warnings = check_offline(entries)
    assert problems == []
    assert all(item.get("generated") is True for item in entries)
    assert all(item["id"].startswith("kb-") for item in entries)
