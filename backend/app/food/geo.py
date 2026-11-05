"""按 IP 猜城市：给麦当劳门店搜索一个默认位置。

为什么不直接要浏览器定位：麦当劳 MCP 的 `query-nearby-stores` 只接受
`city` + `keyword` 两个文本参数，没有经纬度入口。所以我们要的是一个**城市名**，
IP 定位正好够用；浏览器定位（更准）拿不到城市名也白搭。

取到的是"大概在哪座城市"，不精确到街道——这对"默认帮你看最近几家店"已经够了，
页面也会写明来源，允许用户自己改。

隐私：只把 IP 发给 ip-api.com 做一次查询（1 小时缓存），日志里只留打码后的 IP。
"""

from __future__ import annotations

import ipaddress
import time

import httpx

GEO_URL = "http://ip-api.com/json/{ip}?lang=zh-CN&fields=status,country,regionName,city,query"
TIMEOUT_S = 3.0
CACHE_TTL_S = 3600

# 城市名 → 麦当劳搜索用的关键词："济南市" 这种带行政后缀的搜不出来／搜偏
_CITY_SUFFIXES = ("市辖区", "自治州", "地区", "市", "盟")

_CACHE: dict[str, tuple[float, dict]] = {}


def client_ip(request) -> str:
    """取真实客户端 IP：反代后面拿 X-Forwarded-For 的第一段。"""
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    return getattr(getattr(request, "client", None), "host", "") or ""


def is_private(ip: str) -> bool:
    """本机/内网地址：定位它没有意义（开发机上就是 127.0.0.1）。"""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return address.is_private or address.is_loopback or address.is_link_local


def city_for_search(city: str) -> str:
    """去行政后缀，得到麦当劳那边能搜到店的城市名。"""
    name = (city or "").strip()
    for suffix in _CITY_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def mask(ip: str) -> str:
    """日志/前端展示用：IPv4 抹掉最后一段，IPv6 只留前两段。"""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    if address.version == 4:
        parts = ip.split(".")
        return f"{'.'.join(parts[:3])}.xxx"
    return ":".join(ip.split(":")[:2]) + ":xxxx"


def _lookup(ip: str) -> dict:
    """查一次 ip-api.com（免费、无需 key、支持中文）。失败不抛，返回空。"""
    key = ip or "self"
    cached = _CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < CACHE_TTL_S:
        return cached[1]
    # ip 为空时就是 /json/?... → ip-api 会按请求来源定位
    url = GEO_URL.format(ip=ip)
    try:
        with httpx.Client(timeout=TIMEOUT_S, trust_env=False) as http:
            response = http.get(url)
        data = response.json() if response.status_code == 200 else {}
    except Exception:  # noqa: BLE001 - 定位失败只是少了默认值，不该影响点单
        data = {}
    if data.get("status") != "success":
        data = {}
    _CACHE[key] = (time.monotonic(), data)
    return data


def locate_from_request(request) -> dict:
    """优先按客户端 IP；本机/内网就退回服务器出口 IP（本地开发时的真实城市）。"""
    ip = client_ip(request)
    if ip and not is_private(ip):
        data = _lookup(ip)
        if data.get("city"):
            return {
                "source": "ip",
                "city": data["city"],
                "region": data.get("regionName") or "",
                "city_for_search": city_for_search(data["city"]),
                "ip": mask(ip),
            }
    # 本机开发：127.0.0.1 没有位置信息，用服务器自己的公网出口 IP 兜底
    data = _lookup("")
    if data.get("city"):
        return {
            "source": "server_ip",
            "city": data["city"],
            "region": data.get("regionName") or "",
            "city_for_search": city_for_search(data["city"]),
            # 用的是服务器出口 IP，就别把 127.0.0.1 打码后当作用户 IP 展示
            "ip": mask(data.get("query") or ""),
        }
    return {"source": "unknown", "city": "", "region": "", "city_for_search": "", "ip": ""}
