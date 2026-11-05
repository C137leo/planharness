# 22 面试总结与项目架构

> 当前代码：192 passed / 1 skipped；8 个任务级 Evals；6/6 变异测试；26 条知识；6 个工具；TodayAgent 最多 6 步。
> 定位：**bounded agentic workflow（有边界的 Agent 工作流）**，不是全自主 Agent，也不是纯 workflow。

---

## 0. 一句话

> 用户说“帮我规划今天”，模型自己决定先查知识、看计划、找三餐、查恢复建议；  
> 但所有建议必须过工具白名单、引用校验、过敏原校验、安全门和确定性 verifier。  
> **模型负责提议，Harness 负责能不能做。**

---

## 1. 架构图

```text
┌──────────────────────────────────────────────────────────────┐
│ 客户端                                                       │
│  H5 / 微信小程序（uni-app）        Console（质量中心）       │
└───────────────┬──────────────────────────────┬───────────────┘
                │ HTTPS + JWT                  │
                ▼                              ▼
┌──────────────────────────────────────────────────────────────┐
│ FastAPI API 层                                               │
│ auth / profile / plans / drafts / patches / patch-requests   │
│ food / knowledge / agent / checkins / runs / audit / evals   │
└──────┬───────────────────┬───────────────────┬───────────────┘
       │                   │                   │
       ▼                   ▼                   ▼
┌───────────────┐  ┌───────────────────┐  ┌───────────────────┐
│ Agent Runtime │  │ Deterministic     │  │ Persistence       │
│               │  │ Domain            │  │                   │
│ LangGraph     │  │ planner           │  │ Postgres/SQLite   │
│ patch_graph   │  │ patches           │  │ plans/patches     │
│ TodayAgent    │  │ calories          │  │ runs/audit        │
│ PlanningAgent │  │ food catalog      │  │ context snapshots │
│ ChangeAgent   │  │ knowledge         │  │ checkpoints       │
│ checkpoints   │  │ verifier          │  │                   │
└──────┬────────┘  └─────────┬─────────┘  └───────────────────┘
       │                     │
       ▼                     ▼
┌───────────────┐  ┌───────────────────┐
│ Tool layer    │  │ LLM               │
│ coach_* tools │  │ DeepSeek          │
│ knowledge     │  │ tool calling      │
│ food options  │  │ JSON mode         │
│ plan/nutrition│  │ fallback          │
│ recovery      │  │                   │
└───────────────┘  └───────────────────┘
```

### 横切能力

```text
Safety gate / allowlist / citation check / allergen check
Approval / interrupt / checkpoint / rollback
Audit hash chain / cost budget / context budget / trace
Evals / mutation tests / fallback / max steps
```

---

## 2. 代码地图

| 层 | 关键文件 | 职责 |
| --- | --- | --- |
| API | `app/api/routes.py`、`agent_routes.py`、`food_routes.py`、`knowledge_routes.py` | REST、鉴权、异步 run |
| Agent | `app/agent/today_agent.py` | 今日规划 tool loop |
| Agent | `app/agent/planning_agent.py` | 计划生成 tool loop |
| Agent | `app/agent/change_agent.py` | 自然语言 → 补丁操作 |
| Workflow | `app/agent/patch_graph.py` | prepare → approval → commit → verify/rollback |
| 工具 | `app/agent/today_tools.py`、`app/skill/bridge.py` | 白名单工具、Node skill 桥 |
| 领域 | `app/domain/*` | 计划、补丁、热量、餐食、校验 |
| 知识 | `app/knowledge/*` | 26 条知识、bigram 检索、引用 |
| 订餐 | `app/food/*` | 3 个变体 + 平台深链 |
| 持久化 | `app/models.py`、`checkpointer.py` | 业务表 + LangGraph checkpoint |
| Harness | `app/agent/verify.py`、`app/audit.py`、`app/evals/*` | 验证、审计、评测、变异 |
| 前端 | `frontend/src/pages/*` | 今天/计划/打卡/进展/我的/Console |

---

## 3. 三条核心链路

### A. 今日规划 Agent

```text
POST /agent/today-plan
  → AgentRun(status=running)
  → 后台 TodayAgent
       model
        ├─ safety_screen
        ├─ get_today_plan
        ├─ search_knowledge
        ├─ search_food_options × 3
        └─ get_recovery_tips
       ↓ 模型自己决定顺序，最多 6 步
  → validate（引用/过敏原/链接/医疗边界）
  → AgentRun.output + trace
GET /runs/{id} → 前端轮询展示
```

关键：

- 不是代码按顺序调工具；
- 工具失败是 observation，不是 500；
- 无 Key/超步数/校验失败 → 确定性 fallback；
- 最终答案必须带 citation、3 个餐食选项、免责声明。

### B. 计划生成

```text
POST /plans/drafts
  → PlanningAgent tool loop
       coach_safety_gate
       coach_plan_program
       coach_nutrition_targets
       coach_today_session
  → 14 天/周计划草案
  → 用户反馈/确认
  → PlanVersion + MealPlan
```

### C. 改计划 + 审批 + 验证

```text
POST /plans/{id}/patch-requests
  → ChangeAgent：自然语言 → operations
  → 确定性校验/安全门
  → 草案（不写新版本）
  → 用户确认
  → LangGraph patch_graph
       prepare → interrupt → commit → verify
  → 新版本；verify 失败自动回滚
```

---

## 4. Agent 还是 Workflow？

准确说法：

> **外层是 workflow，内层是 agent。**

| 层 | 谁决定 | 代码 |
| --- | --- | --- |
| 今日规划 | 模型决定调哪些工具、什么顺序 | `today_agent.py` |
| 计划生成 | 模型决定工具调用顺序 | `planning_agent.py` |
| 自然语言改计划 | 模型决定 operations | `change_agent.py` |
| 提交/审批/验证 | 代码固定流程 | `patch_graph.py`、`verify.py` |

为什么提交不交给模型：

- 高风险，必须可审批、可回滚；
- 模型会幻觉，代码不能；
- 生产 Agent 不是“越自主越好”，而是“边界内自主”。

面试不要把它包装成“全自主 Agent”，  
包装成 **bounded agent + deterministic harness** 更可信。

---

## 5. Harness 能力矩阵

| 能力 | 实现 |
| --- | --- |
| 安全门 | 禁忌动作、过敏原、红旗症状、医疗边界 |
| 工具治理 | 白名单、schema、错误 observation、max steps |
| 上下文预算 | priority/order、截断、ContextSnapshot |
| 生命周期 | AgentRun waiting/committed/rejected/failed |
| 人工审批 | LangGraph interrupt + checkpoint |
| 独立验证 | `verify_commit` 从 DB 重算，不信任模型 |
| 回滚 | 验证失败自动生成回滚版本 |
| 审计 | hash chain + verify |
| 成本 | token/cost budget + stop_reason |
| 评测 | 8 场景 + 6 变异 + 192 tests |
| 可观测 | `/runs/{id}` trace、context、verification |

---

## 6. 面试讲法

### 30 秒

> 我做的是一个健身计划 Agent + Harness。用户可以用一句话让模型重新规划今天：模型自己决定调知识库、今日计划、三餐外卖、恢复建议这些工具；但所有工具都是只读白名单，最终答案要过引用、过敏原、链接和医疗边界校验。计划变更则走 LangGraph 的 interrupt/checkpoint，人确认后才提交，提交后独立 verifier 从数据库验收，失败自动回滚。

### 2 分钟

1. **问题**：健身计划不是生成一次就结束，用户会改目标、受伤、没时间；纯 RAG 问答和固定 workflow 都不够。
2. **Agent**：模型在有限工具集里自己决定调什么；今日规划、计划生成、改计划分别有 tool loop/LLM planner。
3. **Harness**：安全门、工具白名单、上下文预算、审批、独立 verifier、回滚、审计、成本、Evals。
4. **证据**：192 tests、8 场景全过、6/6 变异、Postgres 跨进程恢复、审计链可验证。
5. **边界**：不是全自主，不做医疗诊断，不代下单，平台只给深链。

### 15 分钟深挖

- 为什么不是纯 ReAct：高风险提交需要确定性审批/验证；
- 为什么不是纯 workflow：用户输入开放，工具顺序不能写死；
- 怎么防止模型乱来：只读工具、白名单、schema、引用校验、validator、人审；
- 怎么证明有效：Evals + 变异测试 + 审计 + trace；
- 怎么恢复：LangGraph checkpoint + Postgres；
- 怎么控制成本：context budget + token/cost budget；
- 怎么失败：fallback 确定性编排，不 500；
- 怎么扩展：MCP/affiliate/RAG 向量，但不改变控制面。

---

## 7. 高频追问

### Q1：这算 Agent 吗？

算 bounded agentic workflow。模型决定工具和顺序；提交/验证是确定性 workflow。  
不是全自主 Agent，这是有意的安全设计。

### Q2：为什么不用 LangChain AgentExecutor？

审批、checkpoint、回滚、审计、独立 verifier 需要显式控制；LangGraph 更适合。

### Q3：模型乱调工具怎么办？

工具白名单 + JSON schema + max steps + 工具错误 observation + 最终校验；越权工具直接拒绝。

### Q4：模型编造知识怎么办？

RAG 只返回真实 chunk；最终 citation 必须存在；无结果就拒绝回答；校验不过降级。

### Q5：为什么不用多 Agent？

当前瓶颈是计划变更的安全和可验证性，不是角色分工；多 Agent 会增加协调失败。

### Q6：为什么订餐不自动下单？

没有稳定官方消费者 API；浏览器自动化有登录/反爬/支付/合规风险。只做 3 个推荐 + 平台深链，用户自己下单。

### Q7：评测怎么证明不是自嗨？

8 个任务级场景 + 6 个变异测试：故意弄坏安全门、审批、验证器、预算、fallback，评测必须抓住。

### Q8：最大风险是什么？

健康安全与错误建议。用红旗拦截、免责声明、确定性安全门、人审、独立 verifier 控制；不做诊断。

---

## 8. 局限（主动说）

- RAG 目前是 26 条 + 关键词/bigram，不是大规模向量检索；
- 订餐是搜索深链，没有实时价格/库存；
- 8 个 Evals 不是统计显著性；
- 小程序无 AppID，真机未验证；
- 域名备案未通过，公网 HTTPS 受限；
- 没有自动打卡/14 天日历（当前范围外）；
- 不是多 Agent，也不打算为了概念加。

---

## 9. 下一步

1. 前端登录/注册页收口；
2. CI 跑 pytest + evals + mutants；
3. 部署/迁移 + 备份；
4. 5 分钟演示脚本 + 录屏；
5. 知识库扩到 50 条、加 retrieval eval；
6. 有联盟 API 再接真实价格。
