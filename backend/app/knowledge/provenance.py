"""知识库出处校验：没有出处的内容不许进库。

这套规则既给 `scripts/check_knowledge_sources.py` 用，也给单元测试用，
避免"脚本能过、CI 不查"的两套标准。
"""

from __future__ import annotations

from urllib.parse import urlparse

# 只收机构/指南站点：博客、营销号、内容农场一律不收
ALLOWED_HOSTS = {
    "www.cnsoc.org", "cnsoc.org",
    "www.acsm.org", "acsm.org",
    "www.nhs.uk", "nhs.uk",
    "www.mayoclinic.org", "mayoclinic.org",
    "www.who.int", "who.int",
    "pmc.ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov",
    "www.issn.net", "jissn.biomedcentral.com",
    # 外部语料（自由许可）：free-exercise-db（Unlicense）/ USDA（CC0）
    "github.com", "www.github.com", "fdc.nal.usda.gov",
}

REQUIRED_FIELDS = ("id", "title", "content", "topic", "source_title", "source_url")
MIN_CONTENT_CHARS = 20


def is_root_url(url: str) -> bool:
    """只给了站点首页（没有具体页面）——能验"机构存在"，验不了"这条结论"。"""
    path = (urlparse(url).path or "/").strip("/")
    return path == ""


def check_offline(seeds: list[dict]) -> tuple[list[str], list[str]]:
    """返回 (错误, 提醒)。错误会让 CI 失败；提醒只提示。"""
    problems: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for item in seeds:
        seed_id = str(item.get("id") or "")
        label = seed_id or "<missing id>"
        for field in REQUIRED_FIELDS:
            if not str(item.get(field) or "").strip():
                problems.append(f"{label}: 缺少 {field}")
        if seed_id in seen:
            problems.append(f"{label}: id 重复")
        seen.add(seed_id)
        content = str(item.get("content") or "")
        if len(content) < MIN_CONTENT_CHARS:
            problems.append(f"{label}: content 太短（{len(content)}）")
        url = str(item.get("source_url") or "")
        parsed = urlparse(url)
        if parsed.scheme != "https":
            problems.append(f"{label}: source_url 必须是 https（{url}）")
        if parsed.hostname not in ALLOWED_HOSTS:
            problems.append(f"{label}: 出处域名不在白名单（{parsed.hostname}）")
        if is_root_url(url):
            warnings.append(f"{label}: 只给了站点首页，建议补具体页面链接（{url}）")
    return problems, warnings
