# 18 剩余工作清单（2026-11-03）

> 当前状态：LangGraph 已接管 API；AgentRun 生命周期、独立 verifier、验证失败自动回滚、上下文预算/快照、checkpointer 都已落地。
>
> 现在缺的不是“更多机制”，而是**证据、可视化和上线收口**。

---

## 0. 当前快照

| 项 | 状态 |
| --- | --- |
| 业务闭环 v1→v2→v3 | ✅ |
| LangGraph `interrupt` / checkpoint | ✅ |
| AgentRun 生命周期 | ✅ waiting / committed / rejected / failed |
| 独立 verifier + 自动回滚 | ✅ |
| ContextBuilder / token 估算 / 截断 | ✅ |
| ContextSnapshot 落库 | ✅ |
| 注册/登录/JWT 撤销 | ✅ |
| 测试 | ✅ 57 passed / 1 skipped |
| 真实 token usage / 成本 | ❌ `tokens_in/out` 仍是 0 |
| 任务级 Evals（8/20 场景） | ❌ |
| Console 真实 Run/Trace | ❌ 当前静态 |
| 审计哈希链 | ❌ |
| ToolRegistry / 白名单 | ❌（当前也没有真正工具调用） |
| 生产部署 | ⚠️ 脚本/清单在，A 类阻断项未全绿 |
| 域名/ICP/HTTPS | ⚠️ 备案审核中，外部 SNI 被拦 |

---

## 1. 最高优先级：任务级 Evals

现在 57 个 pytest 是**单元/集成测试**，不是“场景评测”。

### 要做什么

新建：

```text
app/evals/__init__.py
app/evals/runner.py
app/evals/cases.jsonl
```

先做 8 个场景：

| ID | 场景 | 断言 |
| --- | --- | --- |
| E01 | 正常计划 | v1 合规；verifier pass |
| E02 | 膝痛补丁 | 3/12；churn 25%；无禁忌动作；v2 |
| E03 | 审批拒绝 | v1 不变；run.status=rejected |
| E04 | 审批通过 | v2；verifier pass；run.status=succeeded |
| E05 | 上下文预算不足 | required 约束仍在；低优先级 dropped |
| E06 | LLM 超时/无 Key | fallback 文案；接口不 500 |
| E07 | 非法补丁 | verifier fail；自动回滚；run.status=failed |
| E08 | 重复审批 | 不产生额外版本 |

### 指标

```text
safety_violation_rate = 0
verification_pass_rate
approval_coverage
plan_churn
context_dropped_required = 0
fallback_success
duplicate_commit_rate = 0
p95_latency
```

### 输出

- `evals/reports/latest.json`
- `evals/reports/latest.md`
- CLI：`python -m app.evals.runner`
- 失败必须带 `failure_tags` + run_id

### 为什么先做

这是面试里“Harness 闭环”的证据。没有任务级 Evals，57 个 pytest 只能证明代码能跑，不能证明任务可靠。

---

## 2. 第二优先级：Console 真实 Run/Trace

当前 Console 页面是静态矩阵。

### 后端要加

```text
GET /api/v1/runs?limit=20
GET /api/v1/runs/{run_id}
```

返回：

```json
{
  "run": { "id": "...", "status": "succeeded", "tokens": ..., "latency_ms": ... },
  "patch": { "id": "...", "diff": {...}, "validation": {...} },
  "context": { "token_count": ..., "dropped": [...], "compressed": ... },
  "verification": { "passed": true, "checks": [...] },
  "trace": [{"node": "prepare", "status": "ok", "ts": "..."}]
}
```

### 还需要

- 图节点往 `AgentRun.trace` 追加事件；
- ContextSnapshot 通过 run 关联返回；
- Console 显示：节点时间线、context budget、verification checks、stop_reason。

### 为什么

这是把“Harness 控制平面”从代码变成可点击证据的地方。  
面试官不需要看日志文件，点开一个 run 就能看到：上下文、审批、验证、回滚。

---

## 3. 第三优先级：真实 token usage + 成本预算

现在只有**上下文估算**，没有模型真实 usage。

### 要改

```text
DeepSeekExplainer.call_deepseek:
  data["usage"]["prompt_tokens"] / ["completion_tokens"]

ExplainResult:
  text, tokens_in, tokens_out

create_knee_patch:
  run.tokens_in / run.tokens_out
  run.latency_ms
```

### 加预算

```text
max_tokens_per_run
max_cost_per_run
budget_exceeded → stop_reason
```

这一步做完，才能讲“成本控制”，否则只能讲“上下文裁剪”。

---

## 4. 第四优先级：审计哈希链

新增：

```text
AuditEvent:
  stream_id, seq, actor_type, actor_id, action,
  payload, payload_hash, prev_hash, hash, created_at
```

接入：

- patch_created
- approval_granted / rejected
- patch_committed
- verification_failed
- rollback_committed

API：

```text
GET /api/v1/audit/{stream_id}
GET /api/v1/audit/{stream_id}/verify
```

测试：

- 正常链 valid
- 篡改 payload → broken_at
- 删除中间事件 → 失败

---

## 5. 第五优先级：ToolRegistry（可选）

当前没有真正工具调用，都是直接 domain 函数。  
如果后面加“动作库检索 / 食物查询 / 计划解释工具”，再做：

```text
ToolSpec:
  name, schema, permission, timeout, retryable
```

- 未注册工具拒绝
- 按阶段/角色白名单
- 结构化错误
- 不要为了“看起来像 Agent”硬加 MCP

---

## 6. 生产收口（独立于演示）

按 [15 上线前清单](15-prelaunch-checklist.md)：

- A1 `SECRET_KEY` 换掉
- A2 生产 `.env`
- A3 建 Postgres 用户/库
- A4 收口 `postgres` 超级用户
- A5 端口只绑 127.0.0.1
- A6 env 权限 640
- A7 备份 timer + 异地
- A8 checkpoint 清理 timer
- A9 独立限额 Key
- A10 `LANGGRAPH_STRICT_MSGPACK=true`

域名/ICP 是外部依赖，不要把演示阻塞在它上面；演示可用临时入口/本地录屏。

---

## 7. 建议顺序（3 天）

| 天 | 工作 | 产出 |
| --- | --- | --- |
| Day 1 | 8 场景 Evals + 报告 | `evals/reports/latest.md` |
| Day 2 | Run/Trace API + Console | 可点击的 Trace/Verification 页面 |
| Day 3 | token usage + budget + audit 最小版 | 成本/审计证据 |
| 之后 | 生产 A 类 + 演示排练 | 上线/面试 |

---

## 8. 不要再加的功能

- RAG / 长期记忆
- 多 Agent
- MCP 多 Server
- 自由聊天
- 复杂前端设计

现在缺的是**证据**，不是功能。

---

## 9. 面试一句话

> 我不是在堆功能，而是把一条计划修复链路做成了可恢复、可验证、可审计的控制平面：LangGraph 管暂停/恢复，AgentRun 管生命周期，ContextBuilder 管预算，独立 verifier 用数据库事实验收，失败自动回滚，下一步补任务级 Evals 和 Console Trace。
