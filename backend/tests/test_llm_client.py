"""模型客户端：思考档位只在配置时才带上，工具调用参数不能被改坏。

背景：`deepseek-flash` 支持 `reasoning_effort`（low/high/max），默认 high；
规划这种多步工具调用用 low 会快一倍，所以留成可配置项，不配就不传。
"""

from __future__ import annotations

import json

from app.llm.tool_client import ToolCallingClient, tool_client_from_settings


class FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


class FakeHttp:
    def __init__(self):
        self.body: dict = {}

    def post(self, url, headers=None, json=None, timeout=None):
        self.body = json or {}
        return FakeResponse()


def _client(**kwargs) -> tuple[ToolCallingClient, FakeHttp]:
    http = FakeHttp()
    return ToolCallingClient(api_key="k", base_url="https://example.com/v1",
                             model="deepseek-flash", client=http, **kwargs), http


def test_reasoning_effort_omitted_by_default() -> None:
    client, http = _client()
    client.complete([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    assert "reasoning_effort" not in http.body


def test_reasoning_effort_passed_when_configured() -> None:
    client, http = _client(reasoning_effort="low")
    client.complete([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    assert http.body["reasoning_effort"] == "low"
    # 工具调用参数照旧
    assert http.body["tool_choice"] == "auto"
    assert http.body["tools"] == [{"type": "function"}]


def test_reasoning_effort_normalised(monkeypatch) -> None:
    client, http = _client(reasoning_effort=" LOW ")
    client.complete([{"role": "user", "content": "hi"}])
    assert http.body["reasoning_effort"] == "low"


def test_settings_are_forwarded() -> None:
    class S:
        deepseek_api_key = "k"
        deepseek_base_url = "https://example.com/v1"
        deepseek_model = "deepseek-flash"
        deepseek_timeout_s = 60
        deepseek_reasoning_effort = "low"

    client = tool_client_from_settings(S())
    assert client.model == "deepseek-flash"
    assert client.timeout_s == 60
    assert client.reasoning_effort == "low"


def test_response_shape_is_preserved() -> None:
    client, _ = _client()
    out = client.complete([{"role": "user", "content": "hi"}])
    assert out["message"]["content"] == "ok"
    assert out["finish_reason"] == "stop"
    assert out["usage"]["prompt_tokens"] == 10
