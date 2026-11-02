"""上下文装配：把「想给模型看的东西」显式化，并按预算裁剪。

分三层理解：

- `Section`        一段候选材料（原文 + 优先级 + 顺序 + 是否必需）
- `ContextBuilder` 收集材料，`build()` 时做裁剪决策
- `ContextBundle`  裁剪结果：最终 messages + 统计（用量 / 丢弃 / 是否压缩）

两个维度必须分开，别用一个数字兼任：

- `priority`（重要程度）只决定**丢谁**：预算不够时先牺牲数字小的；
- `order`（语义顺序）只决定**排在哪**：拼进 prompt 时数字小的在前。

如果拿 priority 当顺序用，那么「把 constraints 从 90 调到 95（因为它更重要）」
会顺带改变它在文本里的位置 —— 两个意图纠缠在一个数字上，改一个动两个。

为什么单独成模块：`build()` 设计成**纯函数**（不读数据库、不调模型），
所以可以脱离网络和 DB 做单测 —— 这是整个设计能被验证的前提。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# 中英混合文本的经验值：1 token ≈ 3.5 字符
CHARS_PER_TOKEN = 3.5
# 每条消息的固定开销（role、分隔符、框架加的标记）
PER_SECTION_OVERHEAD = 4
# 固定放在 system 消息里的那一段（身份与硬边界）
SYSTEM_SECTION = "system_policy"
# 优先级 >= 这个值的段落，装不下时先尝试「截断抢救」，而不是整段丢弃
TRUNCATE_PRIORITY_FLOOR = 50
# 剩余额度小于这个数就不必截断了 —— 截出来也没有信息量，纯属浪费
MIN_TRUNCATE_TOKENS = 30
# 截断标记：明确告诉模型「这段是不完整的」，避免它把残句当真
TRUNCATE_MARKER = "\n……（已截断）"


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。

    为什么不用 tiktoken：多一个依赖、多一次联网下载，而且 DeepSeek 的分词器
    和 cl100k_base 也不完全一致。粗估 + 预留余量已经够用，且完全可复现。
    """
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN) + PER_SECTION_OVERHEAD)


def normalize_content(content: str | dict | list) -> str:
    """把任意材料转成字符串。

    设计取舍：**在边界处规范化一次**，而不是给 Section 加一个「类型」字段。
    一旦存了类型标记，token 估算、拼消息、落库三处都要判断类型；
    而且那个标记随时可能和真实类型不符（Python 不强制类型）。
    统一转成文本之后，下游全部只处理 str，逻辑只剩一条路径。

    dict / list 用 `ensure_ascii=False` 保留中文，缩进 2 便于模型阅读。
    """
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class Section:
    """一段候选材料。

    `frozen=True`：创建后不可修改（≈ Java 的 record）。
    - 避免 build 过程中材料被意外篡改；
    - 也让它可以安全地参与去重 / 比较。

    字段说明：
    - name：语义标签，落库和日志里用它定位「这段是什么」
    - content：**已规范化成文本**的内容（dict/list 在 add_section 时就转好了）
    - priority：重要程度，越大越重要（100 最高）→ 决定**丢弃**顺序
    - order：在 prompt 里的位置，越小越靠前 → 决定**拼接**顺序
    - required：硬保护，预算再紧也不丢（安全约束、目标）
    """

    name: str
    content: str
    priority: int
    order: int
    required: bool = False

    @property
    def tokens(self) -> int:
        """这段材料占多少 token。

        用 property 而不是存字段：token 由 content 唯一决定，
        存下来就有「content 改了但 token 没更新」的不一致风险。
        """
        return estimate_tokens(self.content)


class ContextBudgetError(ValueError):
    """必需内容本身就超预算 —— 这是**配置错误**，不是数据波动。

    为什么单独定义异常（而不是复用 ValueError）：
    调用方需要能精确捕获它，然后决定「降级到模板文案」，
    而不能和「用户输入非法」之类的错误混在一起处理。
    """

    def __init__(self, required_tokens: int, budget: int) -> None:
        self.required_tokens = required_tokens
        self.budget = budget
        super().__init__(f"必需内容 {required_tokens} token 超过预算 {budget}")


@dataclass(frozen=True)
class ContextBundle:
    """一次装配的结果。字段与 `context_snapshots` 表一一对应。"""

    messages: list[dict]
    token_estimate: int
    sections: list[dict]
    dropped: list[dict]
    compressed: bool
    # 压缩方式（truncate / summarize），未压缩时为 None
    compression_method: str | None = None

    def to_snapshot_fields(self) -> dict:
        """映射到 ContextSnapshot 的列，方便调用方直接落库。"""
        return {
            "sections": self.sections,
            "token_count": self.token_estimate,
            "dropped": self.dropped,
            "compressed": self.compressed,
            "compression_method": self.compression_method,
        }


class ContextBuilder:
    """收集候选材料；真正的裁剪发生在 build()。

    为什么把「收集」和「裁剪」分开：
    - 收集阶段只关心"有哪些材料"，可以随时加；
    - 裁剪阶段一次性做决策，结果可复现、可单测。
    两者混在一起，就会出现"加到最后已经开始丢前面的"这种难调试的状态。
    """

    def __init__(self, *, budget: int) -> None:
        """budget 是本次装配允许使用的 token 上限。"""
        if budget <= 0:
            raise ValueError(f"budget 必须为正数，实际 {budget}")
        self.budget = budget
        self._sections: list[Section] = []

    def add_section(
        self,
        name: str,
        content: str | dict | list,
        *,
        priority: int,
        order: int,
        required: bool = False,
    ) -> None:
        """登记一段候选材料。

        这里只做三件事：防重复、**规范化内容**、放入列表。
        **不做任何预算判断** —— 判断统一交给 build()。

        注意 `priority` 和 `order` 都是 int，且都紧跟在 content 后面。
        所以强制它们 **keyword-only**（`*` 之后的参数必须写参数名），
        否则 `add_section("x", "y", 90, 20)` 很容易把两者写反 —— 而且不报错。
        """
        if not name:
            raise ValueError("section 的 name 不能为空")
        if any(section.name == name for section in self._sections):
            # 重复 name 一定是调用方写错了；默默覆盖会让排查变得极难
            raise ValueError(f"section 重复登记: {name}")
        self._sections.append(
            Section(
                name=name,
                content=normalize_content(content),
                priority=priority,
                order=order,
                required=required,
            )
        )

    @property
    def total_tokens(self) -> int:
        """全部分件的 token 之和（裁剪前）。"""
        return sum(section.tokens for section in self._sections)

    def required_sections(self) -> list[Section]:
        """必须留下的部分（顺序保持登记顺序）。"""
        return [section for section in self._sections if section.required]

    def optional_sections(self) -> list[Section]:
        """可丢的部分，已按**优先级**从高到低排好（用于决定丢弃顺序）。"""
        return sorted(
            (section for section in self._sections if not section.required),
            key=lambda section: section.priority,
            reverse=True,
        )

    def build(self) -> ContextBundle:
        """裁剪 + 统计 + 拼消息。

        裁剪规则（与产品约定一致）：
        1. required 一律先留下，它们占多少就扣多少；
        2. 剩余额度按优先级从高到低分配；
        3. 装不下的**跳过**（继续看后面更小的，别浪费额度），并记入 dropped。

        为什么 required 超预算要抛异常而不是硬塞：
        硬塞会让请求超过模型窗口；丢 required 又等于取消了它「必需」的语义。
        两种都不可接受，所以把问题抛给调用方去降级（例如改用模板文案）。
        """
        required = self.required_sections()
        optional = self.optional_sections()  # 已按优先级降序

        required_tokens = sum(section.tokens for section in required)
        if required_tokens > self.budget:
            raise ContextBudgetError(required_tokens, self.budget)

        kept: list[Section] = list(required)
        dropped: list[dict] = []
        truncated: dict[str, int] = {}  # name -> 截断前的原始 token 数
        remaining = self.budget - required_tokens

        for section in optional:
            if section.tokens <= remaining:
                kept.append(section)
                remaining -= section.tokens
                continue

            # 装不下：优先级够高的先尝试「截断抢救」，留住开头那部分信息
            if section.priority >= TRUNCATE_PRIORITY_FLOOR and remaining >= MIN_TRUNCATE_TOKENS:
                cut = truncate_content(section.content, remaining)
                if cut:
                    truncated[section.name] = section.tokens
                    kept.append(
                        Section(
                            name=section.name,
                            content=cut,
                            priority=section.priority,
                            order=section.order,
                        )
                    )
                    remaining = 0
                    continue

            dropped.append(
                {
                    "name": section.name,
                    "priority": section.priority,
                    "tokens": section.tokens,
                    "reason": "over_budget",
                }
            )

        return ContextBundle(
            messages=_build_messages(kept),
            token_estimate=sum(section.tokens for section in kept),
            sections=[
                {
                    "name": s.name,
                    "tokens": s.tokens,
                    "priority": s.priority,
                    "order": s.order,
                    "truncated": s.name in truncated,
                    **(
                        {"original_tokens": truncated[s.name]}
                        if s.name in truncated
                        else {}
                    ),
                }
                for s in kept
            ],
            dropped=dropped,
            compressed=bool(truncated),
            compression_method="truncate" if truncated else None,
        )


def truncate_content(content: str, max_tokens: int) -> str:
    """把文本截到 max_tokens 以内，**按行边界**截，而不是按字符硬切。

    为什么按行：硬切会切出半个 JSON、半句话，模型会当成完整内容去理解，
    比"没看到"更危险。按行截断至少保证前 N 条是完整的。
    如果连一行都放不下，就返回空串（调用方会退化成"丢弃"）。

    这里刻意用 `estimate_tokens` 反复校验，而不是一次算准字符数：
    估算函数是唯一的真相来源，用它验收才不会出现"算的时候 3.5、验的时候另一套"。
    """
    if estimate_tokens(content) <= max_tokens:
        return content

    # 字符预算：预留固定开销和标记本身
    chars = int((max_tokens - PER_SECTION_OVERHEAD) * CHARS_PER_TOKEN) - len(TRUNCATE_MARKER)
    while chars > 0:
        cut = content[:chars]
        newline = cut.rfind("\n")
        if newline > 0:
            cut = cut[:newline]
        candidate = cut.rstrip() + TRUNCATE_MARKER
        if estimate_tokens(candidate) <= max_tokens:
            return candidate
        chars = int(chars * 0.8)  # 中文密度比估算更高时逐步回退
    return ""


def _build_messages(sections: list[Section]) -> list[dict]:
    """把留下的 section 拼成 DeepSeek 需要的 [system, user]。

    - `system` 只放身份与硬边界（名字固定的那一段）；
    - 其余**按 order 排序**（不是按 priority），用「【标签】+ 内容」拼接、
      空行分隔，便于模型分辨段落边界。

    用稳定排序：order 相同的保持登记顺序，结果可预测、可复现。
    """
    ordered = sorted(sections, key=lambda s: s.order)
    system = next((s for s in ordered if s.name == SYSTEM_SECTION), None)
    body = [s for s in ordered if s is not system]
    return [
        {"role": "system", "content": system.content if system else ""},
        {
            "role": "user",
            "content": "\n\n".join(f"【{s.name}】\n{s.content}" for s in body),
        },
    ]
