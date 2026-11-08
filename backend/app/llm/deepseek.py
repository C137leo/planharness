import json
import time

import httpx

from app.llm.fallback import fallback_explanation
from app.llm.guard import LLMDisabledError, ensure_llm_enabled
from app.llm.types import ExplainResult


class DeepSeekExplainer:

    def __init__(self,*,api_key:str,base_url:str,model:str,timeout_s: float =8.0,
                 max_tokens: int = 900, reasoning_effort: str = "",
                 client:httpx.Client|None=None):
        self.api_key=api_key
        self.base_url=base_url
        self.model=model
        self.timeout_s=timeout_s
        # 300 是给"不思考的模型"的预算；deepseek-flash 会先花 token 思考，
        # 实测 300 里有 288 用在 reasoning 上，正文只剩半截 JSON → 只能降级成模板。
        self.max_tokens=max_tokens
        self.reasoning_effort=(reasoning_effort or "").strip().lower()
        self._client = client or httpx.Client(timeout=timeout_s, trust_env=False)


    def available(self)-> bool:
        return bool(self.api_key and self.base_url)

    def explain(self, messages:list[dict], operations:list[dict], diff:dict) -> ExplainResult:
        """用装配好的 messages 调用模型；任何异常都降级为模板文案。

        返回值带用量与耗时 —— 这些是「成本控制」的原始数据，
        不能只在日志里打一行就算完。
        """
        # 维护开关打开时不发请求，也不伪装成普通降级
        ensure_llm_enabled()
        if not self.available():
            return ExplainResult(
                text=fallback_explanation(operations, diff),
                fallback=True,
                error="no_api_key",
            )

        started = time.perf_counter()
        try:
            body = {
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": self.max_tokens,
                "response_format": {"type": "json_object"},
            }
            if self.reasoning_effort:
                body["reasoning_effort"] = self.reasoning_effort
            content, usage = self.call_deepseek(body)
            text = self.format_explanation(content)
            return ExplainResult(
                text=text,
                tokens_in=usage.get("prompt_tokens", 0) or 0,
                tokens_out=usage.get("completion_tokens", 0) or 0,
                fallback=False,
                latency_ms=int((time.perf_counter() - started) * 1000),
                model=self.model,
            )
        except LLMDisabledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 任何失败都必须能降级
            return ExplainResult(
                text=fallback_explanation(operations, diff),
                fallback=True,
                latency_ms=int((time.perf_counter() - started) * 1000),
                model=self.model,
                error=f"{type(exc).__name__}: {str(exc)[:120]}",
            )

    def call_deepseek(self, body: dict) -> tuple[str, dict]:
        """返回 (content, usage)。usage 里的 token 数是唯一可信的用量来源。"""
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        response = self._client.post(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            },
            json=body,
            timeout=self.timeout_s,
        )
        if response.status_code != 200:
            raise RuntimeError(f"deepseek {response.status_code}: {response.text[:200]}")
        data = response.json()
        content = (data["choices"][0]["message"].get("content") or "").strip()
        return content, dict(data.get("usage") or {})


    def format_explanation(self,raw: str) -> str:
        s = _strip_fence(raw)
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            # 兜底：模型偶尔会在 JSON 前后加一句解释，截取花括号区间再试一次
            l, r = s.find("{"), s.rfind("}")
            if l == -1 or r <= l:
                raise ValueError(f"no json object in response: {s[:120]!r}")
            data = json.loads(s[l:r + 1])

        if not isinstance(data, dict):
            raise ValueError(f"expected json object, got {type(data).__name__}")

        missing = [
            k for k in FIELDS
            if not isinstance(data.get(k), str) or not data[k].strip()
        ]
        if missing:
            raise ValueError(f"missing/empty fields: {missing}")

        return "\n".join(f"{LABELS[k]}：{data[k].strip()}" for k in FIELDS)

FIELDS = ("summary", "why", "risk", "next")
LABELS = {"summary": "改动", "why": "原因", "risk": "风险", "next": "下一步"}

def _strip_fence(raw: str) -> str:
    """去掉 ```json ... ``` 围栏；没有围栏就原样返回。"""
    s = raw.strip()
    if not s.startswith("```"):
        return s
    nl = s.find("\n")  # 跳过 ``` 或 ```json 这一行
    if nl == -1:  # 只有 ```xxx``` 单行
        return s.strip("`").strip()
    s = s[nl + 1:]
    if s.rstrip().endswith("```"):  # 去掉结尾围栏
        s = s.rstrip()[:-3]
    return s.strip()

def explainer_from_settings(settings) -> "DeepSeekExplainer":
    """按配置构造 explainer；路由和图共用同一份装配逻辑。"""
    return DeepSeekExplainer(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout_s=float(getattr(settings, "deepseek_timeout_s", 30.0) or 30.0),
        reasoning_effort=str(getattr(settings, "deepseek_reasoning_effort", "") or ""),
    )
