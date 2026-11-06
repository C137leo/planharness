"""检查知识库条目的出处是否齐全、是否可信（可选联网探活）。

为什么需要它：知识库是"我们写的摘要 + 权威机构出处"，不是从 skill 里拷来的。
一旦有人加了没出处的条目、或者把出处写成某个博客，这个脚本要能立刻发现。

用法：
    cd backend && .venv/bin/python scripts/check_knowledge_sources.py
    cd backend && .venv/bin/python scripts/check_knowledge_sources.py --online   # 逐个 URL 探活

离线检查（CI 可跑）：
- 必填字段：id / title / content / topic / source_title / source_url
- id 唯一、content 长度 >= 20
- source_url 必须是 https，且域名在白名单里

联网检查（手工执行）：
- 对每个 URL 发 HEAD（失败退 GET），报告状态码；只提示，不阻断
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlparse

from app.knowledge.provenance import check_offline as run_offline_check


def check_online(seeds: list[dict]) -> list[str]:
    import httpx

    notes: list[str] = []
    seen: set[str] = set()
    with httpx.Client(timeout=12, follow_redirects=True, trust_env=False) as client:
        for item in seeds:
            url = str(item.get("source_url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                response = client.head(url)
                if response.status_code >= 400:
                    response = client.get(url)
                notes.append(f"{response.status_code} {url}")
            except Exception as exc:  # noqa: BLE001 - 探活失败只提示
                notes.append(f"ERR {url} ({type(exc).__name__})")
    return notes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--online", action="store_true", help="逐个 URL 探活（需要网络）")
    args = parser.parse_args()

    from app.knowledge.seeds import SEEDS

    problems, warnings = run_offline_check(SEEDS)
    print(f"条目数：{len(SEEDS)}")
    by_host: dict[str, int] = {}
    for item in SEEDS:
        host = urlparse(str(item.get("source_url") or "")).hostname or "?"
        by_host[host] = by_host.get(host, 0) + 1
    for host, count in sorted(by_host.items(), key=lambda pair: -pair[1]):
        print(f"  {count:>2}  {host}")
    if args.online:
        print("\n联网探活：")
        for line in check_online(SEEDS):
            print("  ", line)
    if warnings:
        print("\n提醒（首页链接）：")
        for line in warnings:
            print("  ·", line)
    if problems:
        print("\n离线检查未通过：")
        for line in problems:
            print("  ✗", line)
        return 1
    print("\n离线检查通过（字段齐全 / id 唯一 / 出处为机构站点）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
