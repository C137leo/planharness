"""订餐推荐：3 个变体 + 平台深链。

约束：热量接近原餐（±10%）、蛋白不低于 90%、过敏原排除、
链接只到"搜索页"，不出现下单/支付路径。
"""

from __future__ import annotations

import pytest

from app.food.links import platform_links, search_keyword
from app.food.provider import build_options

MEAL = {"kcal": 700, "protein_g": 45, "slot": "lunch"}


def test_three_variants_with_close_calories() -> None:
    options = build_options(MEAL)
    assert len(options) == 3
    titles = {option.title for option in options}
    assert len(titles) == 3, "3 个变体必须不一样"
    for option in options:
        assert abs(option.kcal - MEAL["kcal"]) <= MEAL["kcal"] * 0.10
        assert option.protein_g >= MEAL["protein_g"] * 0.9


def test_allergy_removes_ingredient() -> None:
    """海鲜过敏时，变体里不能出现虾/三文鱼。"""
    options = build_options(MEAL, allergies=["海鲜过敏"])
    names = {item["name"] for option in options for item in option.items}
    assert "虾" not in names
    assert "三文鱼" not in names


def test_variant_has_platform_links() -> None:
    option = build_options(MEAL)[0].as_dict()
    links = option["links"]
    assert set(links) == {"jd", "taobao", "meituan"}
    for url in links.values():
        assert url.startswith("https://")
        assert "order" not in url.lower() and "pay" not in url.lower()


def test_platforms_search_prepared_meals() -> None:
    """用户确认：三个平台都搜成品餐，所以关键词带"套餐/外卖"。"""
    assert "套餐" in search_keyword("鸡胸肉米饭 + 西兰花", "jd")
    assert "外卖" in search_keyword("鸡胸肉米饭 + 西兰花", "meituan")
    assert "q=" in platform_links("鸡胸肉米饭")["taobao"]


def test_food_options_api(client, auth_headers) -> None:
    from datetime import date

    from app.skill.bridge import SkillBridge

    if not SkillBridge().available:
        pytest.skip("需要本机 skill")

    client.put("/api/v1/me/profile", headers=auth_headers, json={
        "goal": "fat_loss", "sex": "male", "birth_year": 1997,
        "height_cm": 178, "weight_kg": 78, "weekly_days": 3, "session_minutes": 40,
        "equipment": ["dumbbell"], "allergies": ["花生"], "diet_preferences": [],
    })
    draft = client.post("/api/v1/plans/drafts", headers=auth_headers,
                        json={"instruction": "减脂，每周3天"}).json()["data"]
    plan = client.post(f"/api/v1/plans/drafts/{draft['draft_id']}/confirm",
                       headers=auth_headers, json={"comment": "init"}).json()["data"]["plan"]

    slot = plan["meals"][0]["slot"]
    plan_date = date.today()
    resp = client.get(
        "/api/v1/food/options",
        params={"plan_id": plan["plan_id"], "date": plan_date.isoformat(), "meal_slot": slot},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data["options"]) == 3
    assert "平台为准" in data["disclaimer"]
    first = data["options"][0]
    assert first["links"]["meituan"].startswith("https://")
    assert first["reason"]

    # 缓存：再请求一次不应新增记录（这里只验证能稳定返回 3 条）
    again = client.get(
        "/api/v1/food/options",
        params={"plan_id": plan["plan_id"], "date": plan_date.isoformat(), "meal_slot": slot},
        headers=auth_headers,
    ).json()["data"]
    assert [item["id"] for item in again["options"]] == [item["id"] for item in data["options"]]
