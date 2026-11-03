"""LLM 调用的统一返回类型与预算工具。

为什么要 `ExplainResult` 而不是直接返回字符串：
一次调用除了文本，还带**用量**（tokens_in/out）、**耗时**、**是否降级**。
这些是「成本控制」和「可解释性」的原始数据，丢了就再也算不出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExplainResult:
    """一次解释生成的完整结果。"""

    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    fallback: bool = False          # True = 用了模板兜底（没调模型或调用失败）
    latency_ms: int = 0
    model: str = ""
    error: str | None = None        # 降级原因（超时 / 解析失败 / 无 Key）

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass(frozen=True)
class LlmBudget:
    """单次运行的用量与成本上限。

    价格默认值只是量级参考（DeepSeek 官方定价会变），
    真要算钱请按当期价格改 `price_in/out_cny_per_mtok`。
    """

    max_tokens_per_run: int = 20_000
    max_cost_cny_per_run: float = 0.05
    price_in_cny_per_mtok: float = 2.0
    price_out_cny_per_mtok: float = 8.0

    def cost_cny(self, tokens_in: int, tokens_out: int) -> float:
        return round(
            tokens_in / 1_000_000 * self.price_in_cny_per_mtok
            + tokens_out / 1_000_000 * self.price_out_cny_per_mtok,
            6,
        )

    def check_prompt(self, estimated_tokens: int) -> str | None:
        """调用前检查：prompt 本身就超预算就别调了（省一次钱）。"""
        if self.max_tokens_per_run and estimated_tokens > self.max_tokens_per_run:
            return (
                f"prompt_tokens_estimate={estimated_tokens} > "
                f"max_tokens_per_run={self.max_tokens_per_run}"
            )
        return None

    def check_usage(self, tokens_in: int, tokens_out: int) -> str | None:
        """调用后检查：实际用量 / 成本是否超限。"""
        total = tokens_in + tokens_out
        if self.max_tokens_per_run and total > self.max_tokens_per_run:
            return f"total_tokens={total} > max_tokens_per_run={self.max_tokens_per_run}"
        cost = self.cost_cny(tokens_in, tokens_out)
        if self.max_cost_cny_per_run and cost > self.max_cost_cny_per_run:
            return f"cost_cny={cost} > max_cost_cny_per_run={self.max_cost_cny_per_run}"
        return None


def as_explain_result(value) -> ExplainResult:
    """兼容旧调用方：允许 explainer 直接返回字符串。"""
    if isinstance(value, ExplainResult):
        return value
    return ExplainResult(text=str(value or ""), fallback=True)
