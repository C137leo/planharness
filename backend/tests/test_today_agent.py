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
        # 新契约：模型只写文字，餐食由代码用工具结果拼
        final = {
            "summary": "今天按计划训练，餐食给了替换选项。",
            "schedule": [{"time": "18:30", "activity": "力量训练 35 分钟", "why": "按计划"}],
            "tips": tips or [{"text": "训练前热身", "source_ids": list(citations)[:1]}],
            "nutrition_notes": [{"text": "蛋白按目标吃够", "source_ids": list(citations)[:1]}],
        }
        return {"message": {"role": "assistant", "content": json.dumps(final, ensure_ascii=False)},
                "finish_reason": "stop",
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


def test_on_step_streams_progress_while_running(ready) -> None:
    """每记一步就回调一次：前端靠它边跑边看，而不是等整轮结束。"""
    db, user, day = ready
    steps: list[dict] = []
    client = FakeClient([
        ("get_today_plan", {}),
        ("search_knowledge", {"query": "训练前热身"}),
        ("search_food_options", {"meal_slot": "breakfast"}),
        ("search_food_options", {"meal_slot": "lunch"}),
        ("search_food_options", {"meal_slot": "dinner"}),
    ])
    result = run(db, user, day, "帮我规划今天", client=client, on_step=steps.append)

    assert steps, "必须能回调到进度"
    # 第一步是"在思考"，用户点完按钮马上有反馈
    assert steps[0]["node"] == "agent" and steps[0]["status"] == "thinking"
    assert "validate" in [step["node"] for step in steps]
    # 回调收到的和最终 trace 一致（没有漏步，也没有只存一半）
    assert [step["node"] for step in steps] == [step["node"] for step in result.trace]


def test_on_step_also_covers_deterministic_fallback(ready) -> None:
    db, user, day = ready
    steps: list[dict] = []
    run(db, user, day, "帮我规划今天", client=None, on_step=steps.append)
    assert steps[0]["node"] == "safety_screen" or steps[0]["node"] == "agent"
    assert any(step["node"] == "search_food_options" for step in steps)


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


class BrokenThenGoodClient(FakeClient):
    """先返回坏 JSON（模拟被截断），repair 时返回合法 JSON。"""

    def __init__(self, tool_calls, bad_content: str):
        super().__init__(tool_calls)
        self.bad_content = bad_content
        self.calls = 0

    def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            calls = [
                {"id": f"c{index}", "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args)}}
                for index, (name, args) in enumerate(self.tool_calls)
            ]
            return {"message": {"role": "assistant", "content": "", "tool_calls": calls},
                    "finish_reason": "tool_calls", "usage": {}}
        if self.calls == 2:
            return {"message": {"role": "assistant", "content": self.bad_content},
                    "finish_reason": "length", "usage": {}}
        answer = {"summary": "修复后的答案", "schedule": [], "tips": [], "nutrition_notes": []}
        return {"message": {"role": "assistant", "content": json.dumps(answer)},
                "finish_reason": "stop", "usage": {}}


def test_truncated_output_is_repaired(ready) -> None:
    """被截断（finish_reason=length）时先让模型重发一次 JSON，而不是直接降级。"""
    db, user, day = ready
    client = BrokenThenGoodClient([
        ("search_food_options", {"meal_slot": "breakfast"}),
        ("search_food_options", {"meal_slot": "lunch"}),
        ("search_food_options", {"meal_slot": "dinner"}),
    ], bad_content='{"summary": "被截断的')
    result = run(db, user, day, "规划今天", client=client)
    assert not result.fallback
    assert result.final["summary"] == "修复后的答案"
    nodes = [event["node"] for event in result.trace]
    assert "parse" in nodes, "trace 里要记录解析失败/修复"


def test_invalid_schema_is_repaired(ready) -> None:
    """JSON 合法但字段类型不对（Pydantic 拦下）也要走 repair。"""
    db, user, day = ready
    client = BrokenThenGoodClient([
        ("search_food_options", {"meal_slot": "breakfast"}),
        ("search_food_options", {"meal_slot": "lunch"}),
        ("search_food_options", {"meal_slot": "dinner"}),
    ], bad_content='{"summary": 12345}')
    result = run(db, user, day, "规划今天", client=client)
    assert not result.fallback
    assert result.final["meals"], "餐食必须由工具结果拼出来"


def test_meals_come_from_tool_results_not_model(ready) -> None:
    """模型输出里没有 meals 字段，最终答案仍有 3 餐 × 3 选项。"""
    db, user, day = ready
    client = FakeClient([
        ("search_food_options", {"meal_slot": "breakfast"}),
        ("search_food_options", {"meal_slot": "lunch"}),
        ("search_food_options", {"meal_slot": "dinner"}),
    ])
    result = run(db, user, day, "规划今天", client=client)
    assert len(result.final["meals"]) == 3
    assert all(len(meal["options"]) == 3 for meal in result.final["meals"])
