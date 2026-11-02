"""上下文装配的单元测试（纯函数，不碰数据库、不调模型）。

覆盖的都是「规则性」的东西：
- required 永不丢；
- 预算不够先丢低优先级的；
- 装不下的跳过、不浪费额度；
- 高优先级长段落优先「截断抢救」而不是整段丢弃；
- 拼接顺序由 order 决定，与 priority 无关。
"""

from __future__ import annotations

import pytest

from app.llm.context import (
    TRUNCATE_MARKER,
    ContextBudgetError,
    ContextBuilder,
    estimate_tokens,
    normalize_content,
    truncate_content,
)


def _builder(**sections) -> ContextBuilder:
    """按 (priority, order, required, content) 快速搭一个 builder。"""
    builder = ContextBuilder(budget=sections.pop("budget", 2000))
    for name, (content, priority, order, required) in sections.items():
        builder.add_section(
            name, content, priority=priority, order=order, required=required
        )
    return builder


def test_required_sections_survive_any_budget() -> None:
    """required 段落再挤也不会被丢，只是把可选的挤掉。"""
    builder = _builder(
        budget=60,
        policy=("你是教练。" * 5, 100, 10, True),
        constraints=("右膝疼痛，本周最多练 2 天。", 90, 20, True),
        # 优先级 40 < TRUNCATE_PRIORITY_FLOOR(50)，装不下就只能丢，不会被"抢救"
        summary=("很长的计划摘要。" * 60, 40, 60, False),
    )
    bundle = builder.build()

    kept = {item["name"] for item in bundle.sections}
    assert {"policy", "constraints"} <= kept
    assert "summary" in {item["name"] for item in bundle.dropped}
    assert bundle.token_estimate <= 60


def test_drops_lowest_priority_first() -> None:
    """预算不足时，先牺牲优先级最低的段落。"""
    builder = _builder(
        budget=30,
        policy=("你是教练。", 100, 10, True),
        high=("重要材料。" * 10, 80, 30, False),
        low=("次要材料。" * 10, 10, 60, False),
    )
    bundle = builder.build()

    dropped = {item["name"] for item in bundle.dropped}
    assert dropped == {"low"}
    assert bundle.dropped[0]["reason"] == "over_budget"
    # 记录里必须带 priority 与 tokens，否则复盘时看不出「为什么是它」
    assert bundle.dropped[0]["priority"] == 10
    assert bundle.dropped[0]["tokens"] > 0


def test_skips_oversized_section_and_keeps_smaller_one() -> None:
    """装不下的跳过，继续看后面更小的 —— 别把预算浪费掉。"""
    builder = _builder(
        budget=100,
        policy=("你是教练。", 100, 10, True),          # 很小
        # 优先级 40 < 50：不参与"截断抢救"，装不下就直接丢，方便验证"跳过"
        big=("很大的一段。" * 80, 40, 30, False),      # 装不下
        small=("很小的一段。", 10, 60, False),          # 装得下
    )
    bundle = builder.build()

    kept = {item["name"] for item in bundle.sections}
    assert "big" not in kept
    assert "small" in kept, "跳过 big 之后应该还能装下 small"


def test_required_over_budget_raises() -> None:
    """required 本身超预算 → 明确抛异常，交给调用方降级。"""
    builder = _builder(
        budget=10,
        policy=("你是教练，只解释不生成。" * 20, 100, 10, True),
    )
    with pytest.raises(ContextBudgetError) as exc_info:
        builder.build()

    assert exc_info.value.budget == 10
    assert exc_info.value.required_tokens > 10


def test_long_section_is_truncated_instead_of_dropped() -> None:
    """剩余额度够用且优先级够高时，截断保住开头，而不是整段丢。"""
    builder = ContextBuilder(budget=80)
    builder.add_section("system_policy", "你是教练。", priority=100, order=10, required=True)
    builder.add_section("plan_summary", "第1天执行。\n" * 40, priority=50, order=60)

    bundle = builder.build()

    names = [item["name"] for item in bundle.sections]
    assert "plan_summary" in names, "应该被截断保留，而不是丢弃"
    assert bundle.compressed is True
    assert bundle.compression_method == "truncate"

    summary = next(item for item in bundle.sections if item["name"] == "plan_summary")
    assert summary["truncated"] is True
    assert summary["original_tokens"] > summary["tokens"]
    assert bundle.token_estimate <= 80


def test_truncate_keeps_whole_lines() -> None:
    """截断必须落在行边界上，不能切出半个字段。"""
    text = '{\n  "slot_key": "1:strength:1",\n  "action_ref": "glute_bridge",\n  "sets": 3\n}'
    cut = truncate_content(text, 25)

    assert cut.endswith(TRUNCATE_MARKER)
    assert '"action_ref": "glute_bridge",' in cut      # 前两行完整保留
    body = cut[: -len(TRUNCATE_MARKER)]
    assert body.count("{") >= 1
    assert estimate_tokens(cut) <= 25


def test_messages_order_follows_order_not_priority() -> None:
    """priority 再高也不该改变正文位置 —— 位置由 order 决定。"""
    builder = ContextBuilder(budget=2000)
    builder.add_section("system_policy", "系统边界。", priority=100, order=10, required=True)
    builder.add_section("late_but_important", "放最后。", priority=95, order=90)
    builder.add_section("early_but_minor", "放最前。", priority=10, order=20)

    bundle = builder.build()
    user_content = bundle.messages[1]["content"]

    assert user_content.index("early_but_minor") < user_content.index("late_but_important")


def test_dict_content_is_json_encoded_with_chinese() -> None:
    """dict 在边界处就被转成 JSON，中文不转义。"""
    encoded = normalize_content({"name": "臀桥", "sets": 3})
    assert '"name": "臀桥"' in encoded
    assert "\\u" not in encoded


def test_duplicate_section_name_is_rejected() -> None:
    """同名重复登记一定是调用方写错了，直接报错而不是覆盖。"""
    builder = ContextBuilder(budget=100)
    builder.add_section("a", "x", priority=1, order=1)
    with pytest.raises(ValueError):
        builder.add_section("a", "y", priority=2, order=2)
