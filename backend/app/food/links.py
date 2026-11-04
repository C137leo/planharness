"""平台搜索深链：只做搜索，不做登录/支付/自动下单。

为什么是深链而不是"下单接口"：
- 京东/淘宝/美团没有面向个人开发者的稳定下单 API；
- 浏览器自动化会遇到登录、验证码、反爬和支付，还可能违反平台条款；
- 深链能落地、不碰账号和钱，用户点一下跳到平台自己选。
"""

from __future__ import annotations

from urllib.parse import quote

# 三个平台都搜"成品餐"（用户确认的口径），所以关键词统一加"外卖/套餐"这类词
PLATFORM_SEARCH: dict[str, str] = {
    "jd": "https://search.jd.com/Search?keyword={q}",
    "taobao": "https://s.taobao.com/search?q={q}",
    "meituan": "https://www.meituan.com/s/{q}/",
}

PLATFORM_LABELS: dict[str, str] = {
    "jd": "京东",
    "taobao": "淘宝",
    "meituan": "美团",
}

DISCLAIMER = "价格为平台实时结果，库存与配送范围以平台为准；本页只做搜索，不会代下单。"


def search_keyword(title: str, platform: str) -> str:
    """按平台拼搜索词：成品餐 → 直接搜菜名 + 套餐。"""
    clean = (title or "").replace("+", " ").strip()
    if platform == "meituan":
        return f"{clean} 外卖"
    return f"{clean} 套餐"


def platform_links(title: str) -> dict[str, str]:
    """返回 {平台: 深链}。title 是这份餐的名字（例如"鸡胸肉米饭 + 西兰花"）。"""
    links: dict[str, str] = {}
    for platform, template in PLATFORM_SEARCH.items():
        keyword = search_keyword(title, platform)
        links[platform] = template.format(q=quote(keyword))
    return links
