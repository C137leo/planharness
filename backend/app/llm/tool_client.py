"""带 function-calling 的模型客户端。

和 `DeepSeekExplainer` 的区别：这里要的是"模型决定调哪个工具"，
所以请求里带 `tools`，响应里读 `tool_calls`，而不是只要一段 JSON 文本。

用量口径和 explanation 一样：只信服务端返回的 `usage`，不自己估。
"""

from __future__ import annotations

import time

import httpx

from app.llm.guard import ensure_llm_enabled


class ToolCallingClient:
    def __init__(self, *, api_key: str, base_url: str, model: str,
                 timeout_s: float = 30.0, reasoning_effort: str = "",
                 client: httpx.Client | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        # low / high / max；留空 = 不传，用服务端默认（deepseek-flash 默认 high）
        self.reasoning_effort = (reasoning_effort or "").strip().lower()
        self._client = client or httpx.Client(timeout=timeout_s, trust_env=False)

    def available(self) -> bool:
        return bool(self.api_key and self.base_url)

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 tool_choice: str = "auto", temperature: float = 0.2,
                 max_tokens: int = 1200) -> dict:
        """返回 {message, usage, latency_ms, model}；任何网络/协议错误都抛异常，由调用方降级。"""
        # 运营开关优先级最高：即使有 Key，也不允许请求 DeepSeek
        ensure_llm_enabled()
        if not self.available():
            raise RuntimeError("no_api_key")

        body: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice
        if self.reasoning_effort:
            # 思考档位：flash 默认 high，规划这种"多步工具调用"用 low 会明显快
            body["reasoning_effort"] = self.reasoning_effort

        started = time.perf_counter()
        response = self._client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json=body,
            timeout=self.timeout_s,
        )
        if response.status_code != 200:
            raise RuntimeError(f"deepseek {response.status_code}: {response.text[:200]}")

        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = dict(choice.get("message") or {})
        return {
            "message": message,
            "finish_reason": choice.get("finish_reason"),
            "usage": dict(data.get("usage") or {}),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "model": self.model,
        }


def tool_client_from_settings(settings) -> ToolCallingClient:
    return ToolCallingClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout_s=float(getattr(settings, "deepseek_timeout_s", 30.0) or 30.0),
        reasoning_effort=str(getattr(settings, "deepseek_reasoning_effort", "") or ""),
    )
