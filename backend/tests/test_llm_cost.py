"""真实 token / 成本：usage 读取、预算拦截、降级留痕。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.llm.deepseek import DeepSeekExplainer
from app.llm.types import LlmBudget


def _explainer(handler, *, key="test"):
    return DeepSeekExplainer(
        api_key=key,
        base_url="https://mock/v1",
        model="deepseek-flash",
        timeout_s=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_explain_returns_usage_and_text() -> None:
    """usage 必须从响应里读出来 —— 这是唯一可信的用量来源。"""

    def handler(request):
        assert json.loads(request.content)["messages"]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"summary":"改了3处","why":"膝痛","risk":"注意","next":"确认"}'}}],
                "usage": {"prompt_tokens": 812, "completion_tokens": 96, "total_tokens": 908},
            },
        )

    result = _explainer(handler).explain([{"role": "user", "content": "x"}], [], {})

    assert result.fallback is False
    assert result.tokens_in == 812
    assert result.tokens_out == 96
    assert result.total_tokens == 908
    assert "改动：改了3处" in result.text


def test_no_key_falls_back_with_reason() -> None:
    result = _explainer(lambda r: None, key="").explain(
        [], [{"op": "remove_action", "before_action_ref": "row"}], {"changed_slots": 1, "total_slots": 12}
    )
    assert result.fallback is True
    assert result.error == "no_api_key"
    assert result.tokens_in == 0
    assert "本次计划调整" in result.text


def test_http_error_falls_back_and_keeps_latency() -> None:
    result = _explainer(lambda r: httpx.Response(500, text="boom")).explain(
        [{"role": "user", "content": "x"}],
        [{"op": "remove_action", "before_action_ref": "row"}],
        {"changed_slots": 1, "total_slots": 12},
    )
    assert result.fallback is True
    assert result.error and "RuntimeError" in result.error
    assert result.latency_ms >= 0


def test_budget_blocks_oversized_prompt() -> None:
    """prompt 估算就超上限 → 根本不调模型（省钱）。"""
    budget = LlmBudget(max_tokens_per_run=100)
    assert budget.check_prompt(50) is None
    assert "max_tokens_per_run" in (budget.check_prompt(500) or "")


def test_budget_flags_usage_and_cost() -> None:
    budget = LlmBudget(max_tokens_per_run=1000, max_cost_cny_per_run=0.01)
    assert budget.check_usage(500, 200) is None
    assert "max_tokens_per_run" in (budget.check_usage(900, 300) or "")

    expensive = LlmBudget(max_tokens_per_run=10_000_000, max_cost_cny_per_run=0.001)
    assert "max_cost" in (expensive.check_usage(900, 300) or "")


def test_cost_formula() -> None:
    budget = LlmBudget(price_in_cny_per_mtok=2.0, price_out_cny_per_mtok=8.0)
    # 812 * 2/1e6 + 96 * 8/1e6 = 0.002392
    assert budget.cost_cny(812, 96) == pytest.approx(0.002392, rel=1e-6)
