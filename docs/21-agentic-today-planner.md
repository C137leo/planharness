# 21 今日规划 Agent：让模型自己决定调哪些工具

> 目标：用户说“帮我规划今天”，Agent 自己决定：
> 先查知识 / 先看今日计划 / 找三餐外卖 / 查注意事项 / 补营养知识。
>
> 不是代码里写死 `retrieve → food → tips` 的顺序。

---

## 0. 先分清：什么叫“不是 workflow”

### 错误做法（workflow）

```python
knowledge = search_knowledge(...)
meals = search_food_options(...)
tips = get_recovery_tips(...)
answer = llm.summarize(knowledge, meals, tips)
```

代码已经决定了顺序，模型只负责总结。

### 正确做法（agent loop）

```text
model 看用户请求 + 工具清单
  → 决定调用 search_knowledge
  → 读结果
  → 决定调用 get_today_plan
  → 读结果
  → 决定调用 search_food_options
  → 读结果
  → 决定调用 get_recovery_tips
  → 读结果
  → 输出最终 JSON
```

模型决定**调什么、按什么顺序、要不要多查一次**；运行时只负责：
- 只允许白名单工具；
- 最多 N 步；
- 工具结果结构化；
- 最终答案过确定性校验。

---

## 1. 工具清单

只暴露读工具，不允许下单/支付。

| 工具 | 输入 | 返回 | 作用 |
| --- | --- | --- | --- |
| `safety_screen` | text | `{blocked, reason, action}` | 红旗症状硬拦截 |
| `get_today_plan` | date | 今日训练/餐食/计划版本 | 当前计划事实 |
| `get_nutrition_targets` | — | kcal/protein/fat/carb | 营养目标 |
| `search_knowledge` | query, body_part? | `[{id,title,content,source}]` | 动作/安全/恢复知识 |
| `search_food_options` | meal_slot, preferences | 3 个 food option + links | 外卖/食材推荐 |
| `get_recovery_tips` | training_time, meal_time | `{tips[], citations[]}` | 饭后多久运动等 |

### 工具返回必须结构化

```json
{
  "status": "ok",
  "data": {...},
  "citations": [{"id": "...", "title": "...", "source_url": "..."}]
}
```

失败：

```json
{
  "status": "error",
  "error": "food_catalog_empty",
  "retryable": false
}
```

错误也要回给模型，让它决定换工具/换参数/告诉用户。

---

## 2. LangGraph 图：真正的 agent loop

```text
START
  → load_context
  → agent ──(tool_calls)──→ tools
     ↑                         │
     └─────────────────────────┘
  → (no tool_calls)
  → validate_answer
  → END
```

- `agent`：调模型，可能返回 tool_calls 或最终 JSON。
- `tools`：执行白名单工具，把结果写回 messages。
- 条件边：有 tool_calls 就回 agent；没有就进 `validate_answer`。
- 最大步数：`MAX_STEPS=6`，超过走 fallback。
- `validate_answer`：确定性检查，不通过就修复一次或降级。

State：

```python
class TodayState(TypedDict, total=False):
    user_id: str
    date: str
    messages: list[dict]       # system/user/assistant/tool
    step: int
    tool_events: list[dict]
    final: dict | None
    citations: list[dict]
    error: str
```

`AgentRun.trace` 记录：

```json
{"step":1,"node":"model","tool_calls":["safety_screen"]}
{"step":2,"node":"safety_screen","status":"ok"}
{"step":3,"node":"model","tool_calls":["search_knowledge","get_today_plan"]}
...
```

### 模型选择：deepseek-flash

`DEEPSEEK_MODEL=deepseek-flash`（DeepSeek-V4.1-Flash，1M 上下文）实测可用：
工具调用、`response_format=json_object`、整轮规划（3 餐 ×3 选项 + 麦当劳块）都正常。

它默认带思考，同一句话的耗时/输出 token 明显高于 deepseek-chat，
所以加了 `DEEPSEEK_REASONING_EFFORT`（`low` / `high` / `max`，留空=服务端默认 high）
并默认设为 `low`；本机实测同一个规划任务：

| 配置 | 耗时 | 输出 token |
|---|---|---|
| deepseek-chat | 12.6s | 2119 |
| flash（默认 high） | 21.2s | 3784 |
| flash（low） | 10.0s | 1778 |

注意点：flash 会思考，单轮可能吃到 30s 以上，`DEEPSEEK_TIMEOUT_S` 已从 30 调到 60，
否则超时会触发确定性降级（用户看到的就是"模板答案"而不是模型答案）。

还有一个坑：补丁解释器原来给 `max_tokens=300`，而 flash 会先把 token 花在思考上
（实测 300 里有 288 是 reasoning），正文只剩半截 JSON，解析失败 → 自动降级成模板文案。
所以 `DeepSeekExplainer` 的预算改成 900（`max_tokens`），超时也统一走 `DEEPSEEK_TIMEOUT_S`，
不再写死 8 秒。

**trace 是边跑边写的**：`run()` 接受一个 `on_step` 回调，每记一步就回调一次；
后台任务在回调里把 `run.trace` 追加并 commit，前端每 800ms 轮询 `/runs/{id}` 就能看到进度。
不这样做的话，`trace` 要等整轮跑完才出现，用户点完"帮我规划"只能干等十几秒。

前端把节点翻成人话（`search_knowledge` → "查知识库"），
`agent/tools` 那一步还会带上本轮打算调的工具（`查知识库 ×3、排训练与恢复时间`），
连续相同的步骤合并成 `×N`，避免刷屏；跑完保留一条 `11s · 18 步完成 · 看过程`。

---

## 3. 最终输出契约

```json
{
  "summary": "今天以低冲击训练为主，三餐已给出 3 个可下单选项。",
  "schedule": [
    {"time": "18:30", "activity": "力量训练 35 分钟", "why": "..."}
  ],
  "meals": [
    {
      "slot": "lunch",
      "target_kcal": 620,
      "options": [
        {
          "title": "鸡胸糙米碗 + 西兰花",
          "kcal": 620,
          "protein_g": 45,
          "reason": "高蛋白、低脂、避开花生",
          "links": {"jd": "...", "taobao": "...", "meituan": "..."}
        }
      ]
    }
  ],
  "tips": [
    {"text": "饭后休息 60-90 分钟再训练", "source_ids": ["recovery-01"]}
  ],
  "nutrition_notes": [
    {"text": "今日蛋白目标 120g，午餐建议 35-45g", "source_ids": ["nutrition-01"]}
  ],
  "citations": [{"id": "...", "title": "...", "source_url": "..."}],
  "disclaimer": "本内容不构成医疗建议；如身体不适请立即就医。"
}
```

确定性校验：

- 至少 1 个训练/活动；
- 三餐各有 3 个选项；
- 过敏原不出现；
- 每个 tips/nutrition 有 source_ids；
- 引用必须真实存在；
- 出现诊断/用药建议 → 拒绝；
- 红旗症状 → blocked。

时间偏好不只靠模型自觉，`_enforce_schedule` 会用同一套解析覆盖模型给的 `schedule`：

| 用户说法 | 结果 |
| --- | --- |
| `分成两次训练：早上7:30-8:10、晚上18:30-19:10` | 两个时段，按给定区间 |
| `早上7点到8点训练` | 一个时段 `07:00-08:00`，不是 7 点、8 点两次 |
| `晚上8点训练` | `20:00` |
| `分三次训练` | `07:30 / 12:30 / 18:30`，按动作数均分 |
| `早餐7点、午餐12点` | 吃饭时间不算训练时段，仍走默认安排 |
| `每次训练控制在30分钟` | 每个时段不超过 30 分钟 |
| 连续提意见 | 只认最后一条里的时间，避免旧时间越积越多 |

---

## 4. System prompt

```text
你是今日活动规划 Agent。
你可以按任意顺序调用工具，但必须遵守：

1. 在给出训练/健康建议前，先调用 search_knowledge，并引用来源。
2. 在推荐餐食前，调用 search_food_options；不要自己编价格/链接。
3. 需要当前计划/营养目标时，调用 get_today_plan / get_nutrition_targets。
4. 用户提到胸痛、晕厥、严重肿胀、怀孕、进食障碍等，先调用 safety_screen；
   blocked=true 时停止规划，只给就医建议。
5. 你只推荐，不代下单；只返回平台链接。
6. 不要诊断疾病，不要给药物建议。
7. 最终只输出 JSON，不要 Markdown。
```

注意：这里没有规定“先 A 再 B”。  
模型可以：
- 先查知识，再查计划；
- 先查计划，再查知识；
- 先 safety，再 food；
- 多查一次知识。

只要最终答案满足校验。

---

## 5. 工具实现骨架

```python
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "检索动作要领、安全、恢复、营养知识；回答健康建议前必须调用",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "body_part": {"type": "string"}
                },
                "required": ["query"]
            }
        }
    },
    # get_today_plan / get_nutrition_targets / ...
]
```

Dispatch：

```python
def dispatch(name: str, args: dict, ctx) -> dict:
    if name not in ALLOWED_TOOLS:
        return {"status": "error", "error": "tool_not_allowed"}
    try:
        if name == "search_knowledge":
            hits = knowledge.search(args["query"], args.get("body_part"))
            return {"status": "ok", "data": hits, "citations": [...]}
        ...
    except Exception as exc:
        return {"status": "error", "error": type(exc).__name__, "detail": str(exc)[:200]}
```

不要抛到接口层；工具失败是 Agent 的观察结果。

---

## 6. Agent loop 骨架

```python
for step in range(MAX_STEPS):
    reply = client.complete(messages, tools=TOOLS, tool_choice="auto")
    message = reply["message"]
    calls = message.get("tool_calls") or []

    if not calls:
        final = parse_json(message["content"])
        return validate(final)

    messages.append({"role": "assistant", "content": message.get("content") or "",
                     "tool_calls": calls})
    for call in calls:
        result = dispatch(call["function"]["name"],
                          json.loads(call["function"]["arguments"]),
                          ctx)
        messages.append({
            "role": "tool",
            "tool_call_id": call["id"],
            "content": json.dumps(result, ensure_ascii=False)[:6000],
        })

raise RuntimeError("MAX_STEPS exceeded")
```

关键：

- 每轮模型看到上一轮 tool result；
- 模型可以改变计划；
- 不是代码提前算好所有东西。

---

## 7. API

```text
POST /api/v1/agent/today-plan
  { "text": "帮我规划今天", "date": "2026-11-05" }
  → 202 { "run_id": "..." }

GET /api/v1/runs/{run_id}
  → { status, trace, final }

POST /api/v1/agent/today-plan/{run_id}/feedback
  { "text": "午餐换清淡一点" }
  → 202 { "run_id": "..." }
```

建议异步：

- Agent 可能 5～15 秒；
- 前端轮询 run；
- 不要用一个 30 秒同步请求卡住。

---

## 8. UI

今天页顶部：

```text
┌────────────────────────────┐
│ 今天想怎么安排？           │
│ [帮我规划今天           ]  │
│ [今天想休息] [少油一点]    │
│ [开始规划]                 │
└────────────────────────────┘
```

运行中：显示真实工具步骤：

```text
✓ 安全检查
✓ 查询今日计划
✓ 检索训练知识
✓ 搜索 3 个午餐选项
✓ 查询恢复建议
→ 生成今日方案
```

最终卡片：

```text
今日安排
18:30 力量训练 35 分钟

午餐（3 选 1）
[鸡胸糙米碗] 620 kcal / 蛋白 45g
[京东] [淘宝] [美团]
...

注意事项
饭后 60-90 分钟再训练
...

营养知识
今日蛋白目标 120g...
[来源：...]

[重新规划] [换一换午餐]
```

结果卡下面还有一块「不满意？再调一下」：

```text
不满意？再调一下
[分成两次训练：早上7:30-8:10、晚上18:30-19:10]
[训练改到晚上20:00] [每次训练控制在30分钟]
[按这个再规划]
```

点「按这个再规划」会 `POST /agent/today-plan/{run_id}/feedback`，
后端把上一轮原话和这次意见拼起来重跑（不是从零开始），前端照常轮询 trace 和新结果。

不要显示“mock/临时库/演示补丁”等开发提示。  
只保留医疗免责声明。

---

## 9. Evals

至少 6 条：

| 场景 | 断言 |
| --- | --- |
| 正常“规划今天” | 调用 knowledge/food/plan；输出 3 餐 × 3 选项 |
| 只说“随便” | 追问或给默认方案，不编过敏原 |
| 花生过敏 | 所有餐食无花生 |
| 膝痛 | 查知识 + 避开高冲击动作 |
| 胸痛 | safety_screen blocked，停止规划 |
| 工具超时 | 降级/重试，不 500 |

指标：

- `tool_coverage`：是否用了必要工具；
- `citation_valid`；
- `allergen_violation=0`；
- `meal_options_count=3`；
- `safety_block_rate`；
- `p95_latency`；
- `max_steps_exceeded=0`。

关键：**不要断言固定工具顺序**，只断言“必要工具被调用 + 结果合法”。

---

## 10. 实施顺序

1. `search_knowledge` + `knowledge_chunks`；
2. `search_food_options` + 平台深链；
3. `get_today_plan` / `get_nutrition_targets`；
4. `safety_screen` / `get_recovery_tips`；
5. LangGraph agent loop；
6. API + run trace；
7. UI；
8. Evals。

先做工具，再做 loop；不然模型没有可调用的东西。
