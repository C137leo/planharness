"""TodayAgent：模型自主决定工具顺序 + 确定性校验 + 降级。

不 mock 工具本身：真正跑 knowledge / food / plan 工具，只用一个假模型驱动 tool_calls。
断言只看"必要工具被调用 + 结果合法"，不断言固定顺序（顺序本来就是模型决定的）。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from app.agent.today_agent import run
from app.models import User


class FakeClient:
    """第一次调用发起工具调用，第二次用工具结果拼最终 JSON。"""

    def __init__(self, tool_calls, *, always_tools: bool = False):
        self.tool_calls = tool_calls
        self.always_tools = always_tools
        self.calls = 0

    def available(self) -> bool:
        return True

    def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1 or self.always_tools:
            calls = [
                {"id": f"call_{index}", "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args)}}
                for index, (name, args) in enumerate(self.tool_calls)
            ]
            return {"message": {"role": "assistant", "content": "", "tool_calls": calls},
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
        # 从工具结果里拼答案
        meals, citations, tips = [], {}, []
        for message in messages:
            if message.get("role") != "tool":
                continue
            payload = json.loads(message["content"])
            if payload.get("status") != "ok":
                continue
            data = payload.get("data") or {}
            for citation in payload.get("citations") or []:
                citations[citation["id"]] = citation
            if isinstance(data, dict) and data.get("options"):
                meals.append({"slot": data["meal_slot"], "target_kcal": data.get("target_kcal", 0),
                              "options": data["options"]})
            if isinstance(data, dict) and data.get("tips"):
                tips = data["tips"]
        final = {
            "summary": "今天按计划训练，餐食给了替换选项。",
            "schedule": [{"time": "18:30", "activity": "力量训练 35 分钟", "why": "按计划"}],
            "meals": meals,
            "tips": tips or [{"text": "训练前热身", "source_ids": list(citations)[:1]}],
            "nutrition_notes": [{"text": "蛋白按目标吃够", "source_ids": list(citations)[:1]}],
            "citations": list(citations.values()),
            "disclaimer": "本内容不构成医疗建议；如身体不适请立即就医。",
        }
        return {"message": {"role": "assistant", "content": json.dumps(final, ensure_ascii=False)},
                "usage": {"prompt_tokens": 200, "completion_tokens": 80}}


@pytest.fixture()
def ready(app_fixture):
    """带一份计划 + 数据的 (db, user, day)。"""
    client, app = app_fixture
    headers = client.post("/api/v1/auth/demo", json={}).json()["data"]["access_token"]
    auth = {"Authorization": f"Bearer {headers}"}
    client.put("/api/v1/me/profile", headers=auth, json={
        "goal": "fat_loss", "sex": "male", "birth_year": 1997, "height_cm": 178,
        "weight_kg": 78, "weekly_days": 3, "session_minutes": 40,
        "equipment": ["dumbbell"], "allergies": [], "diet_preferences": [],
    })
    draft = client.post("/api/v1/plans/drafts", headers=auth,
                        json={"instruction": "减脂，每周3天"}).json()["data"]
    client.post(f"/api/v1/plans/drafts/{draft['draft_id']}/confirm", headers=auth,
                json={"comment": "init"})
    me = client.get("/api/v1/auth/me", headers=auth).json()["data"]
    with app.state.session_factory() as db:
        user = db.get(User, me["user_id"])
        yield db, user, dt.date.today()


def test_agent_loop_uses_tools_and_builds_meals(ready) -> None:
    db, user, day = ready
    client = FakeClient([
        ("get_today_plan", {}),
        ("search_knowledge", {"query": "训练前热身"}),
        ("search_food_options", {"meal_slot": "lunch"}),
        ("search_food_options", {"meal_slot": "dinner"}),
        ("search_food_options", {"meal_slot": "breakfast"}),
    ])
    result = run(db, user, day, "帮我规划今天", client=client)
    assert not result.fallback
    meals = result.final["meals"]
    assert len(meals) == 3
    assert all(len(meal["options"]) == 3 for meal in meals)
    assert result.final["citations"], "必须带引用"
    nodes = [event["node"] for event in result.trace]
    assert "validate" in nodes


def test_tool_order_is_model_choice(ready) -> None:
    """先找外卖再查知识也允许：代码不强制顺序。"""
    db, user, day = ready
    client = FakeClient([
        ("search_food_options", {"meal_slot": "lunch"}),
        ("search_food_options", {"meal_slot": "dinner"}),
        ("search_food_options", {"meal_slot": "breakfast"}),
        ("search_knowledge", {"query": "饭后多久训练"}),
    ])
    result = run(db, user, day, "先帮我点餐再讲注意事项", client=client)
    assert not result.fallback
    assert len(result.final["meals"]) == 3


def test_red_flag_blocks_planning(ready) -> None:
    db, user, day = ready
    client = FakeClient([("safety_screen", {"text": "训练时胸痛，还能练吗"})])
    result = run(db, user, day, "训练时胸痛，还能练吗", client=client)
    assert result.refused
    assert result.final["blocked"] is True
    assert result.final["meals"] == []


def test_tool_error_is_observation_not_crash(ready) -> None:
    """给一个不存在的餐次，工具返回 error，模型继续，最终仍可通过校验。"""
    db, user, day = ready
    client = FakeClient([
        ("search_food_options", {"meal_slot": "snack"}),
        ("search_food_options", {"meal_slot": "lunch"}),
        ("search_food_options", {"meal_slot": "dinner"}),
        ("search_food_options", {"meal_slot": "breakfast"}),
    ])
    result = run(db, user, day, "规划今天", client=client)
    assert result.final is not None
    assert not result.refused


def test_max_steps_falls_back(ready) -> None:
    db, user, day = ready
    client = FakeClient([("get_today_plan", {})], always_tools=True)
    result = run(db, user, day, "规划今天", client=client)
    assert result.fallback, "步数超限必须降级，而不是抛 500"
    assert result.final is not None


def test_no_model_uses_deterministic_tools(ready) -> None:
    db, user, day = ready
    result = run(db, user, day, "帮我规划今天", client=None)
    assert result.fallback
    assert result.final["meals"], "降级也要给出餐食选项"
    assert result.final["citations"], "降级也要带引用"
