# 23 和“工业级 Agent”的差距，以及面试怎么打

> 先说结论：**你拿“工业级”当标尺，当然会觉得差得远。**  
> 大厂面试官也不会要求一个个人项目有工业级规模；他们看的是：  
> **你有没有工程判断、失败意识、可验证性，以及能不能把边界讲清楚。**

---

## 0. 你的项目其实不弱

当前已有：

- 192 tests / 1 skipped
- 8 个任务级 Evals
- 6/6 变异测试
- LangGraph interrupt/checkpoint，Postgres 跨进程恢复
- AgentRun 生命周期、独立 verifier、失败自动回滚
- 审计哈希链、成本预算、上下文预算/快照
- TodayAgent tool loop（模型自己决定工具顺序）
- RAG 26 条知识 + 引用
- 3 个餐食变体 + 京东/淘宝/美团深链
- H5 / Console / 登录注册

这已经超过大多数“LangChain + RAG + 聊天”的候选项目。

---

## 1. 但和工业级确实差什么

| 维度 | 现在 | 工业级 | 能不能短期补 |
| --- | --- | --- | --- |
| 规模 | 单进程、2C2G | 多副本、队列、限流、容量规划 | 部分：压测 + 限流 |
| 可用性 | 有 checkpoint/rollback | SLO、告警、值班、故障演练 | 部分：指标 + 告警 |
| 可观测 | trace/audit | OTel、指标、日志、链路、看板 | 可以：OTel + 看板 |
| 安全 | JWT、白名单、审计 | 多租户、密钥管理、合规、数据保留 | 部分：文档 + 限流 |
| 数据 | 单库、无迁移体系 | 迁移、备份、恢复演练、分区 | 部分：Alembic + 备份 |
| 评测 | 8 场景 + 6 变异 | 大规模离线集、在线 eval、A/B | 部分：扩到 20 |
| 成本 | 单次预算 | 配额、账单、成本归因 | 部分：预算 + 报表 |
| 产品 | 个人 demo | 真实用户、留存、业务指标 | 短期补不了 |
| 合规 | 免责声明 | 隐私政策、数据授权、DPA | 短期补不了 |

**不可短期补的，不要假装有。**  
面试官更怕“吹成工业级”，不怕“承认是垂直切片”。

---

## 2. 你真正的短板不是功能

不是缺 RAG、不是缺多 Agent、不是缺 MCP。

是这三件事：

1. **没有 CI 门禁**：Evals/变异测试还是手动跑；
2. **没有运行时指标**：只有 trace/audit，没有 QPS、p95、错误率、token/成本趋势；
3. **没有公开可演示的入口**：域名备案/HTTPS 受限，面试时可能只能本地跑。

这三件比再加一个功能更能提升“工程感”。

---

## 3. 一周内最值得补的 5 件事

### 1）CI 门禁（1 小时）

```bash
uv run pytest
python -m app.evals
python -m app.evals --mutants
```

- 任一失败 → CI 红；
- 上传报告 artifact；
- README 放 CI badge。

### 2）运行时指标（半天）

最小版：

```text
runs_total{status}
run_latency_ms
tool_calls_total{tool,status}
tokens_total
cost_cny_total
checkpoint_resume_total
```

- 用 Prometheus client 或结构化日志；
- Console 增加“指标”页；
- 不要求 Grafana，能展示趋势即可。

### 3）压测（半天）

用 k6/vegeta 打：

```text
GET /health
GET /plans/current
POST /agent/today-plan（mock 模型）
```

产出：

```text
并发 50，p95 xxx ms，错误率 x%，瓶颈在 DB/模型/序列化
```

这是面试里非常加分的“我知道自己的上限”。

### 4）20 条 Evals + RAG eval（1 天）

- 现有 8 条扩到 20：红旗、过敏、无计划、工具超时、重复审批、上下文超预算；
- RAG：10 条问题，recall@3、citation valid、no-match refusal；
- 指标趋势图放进 Console。

### 5）部署/录屏（1 天）

- 域名备案没下来：用本机/临时入口录 5 分钟视频；
- 服务器跑 preflight；
- 备份/恢复演练一次；
- 面试时准备“如果公网不可用，我有录屏”。

---

## 4. 面试怎么重新定位

不要说：

> “我做了一个工业级 Agent。”

要说：

> “我做了一个**生产形状的垂直切片**：把高风险计划变更做成 bounded agent + deterministic harness。  
> 它不追求工业级规模，但把安全、审批、恢复、验证、审计、成本、评测这些生产问题都落到了代码和测试里。”

这句话会立刻把评价维度从“功能多不多”拉到“工程判断”。

---

## 5. 三个最值钱的故事

### 故事 1：模型能改计划，但不能直接提交

- 模型输出 operations；
- 安全门/validator 拦截；
- 人审；
- 独立 verifier 从 DB 验收；
- 失败自动回滚。

### 故事 2：进程重启，审批不丢

- LangGraph `interrupt`；
- Postgres checkpointer；
- `Command(resume=...)`；
- 跨进程测试。

### 故事 3：评测不是自嗨

- 8 场景；
- 6 个变异体：弄坏安全门/审批/验证器/预算/fallback，评测必须抓住；
- 192 tests。

---

## 6. 被问“工业级差在哪”怎么答

不要防御，给结构化回答：

> 差在规模、SLO、多租户、数据合规和真实用户验证，这些不是个人项目能补齐的。  
> 我能补齐的是工程形状：CI、指标、压测、备份、审计、成本、失败注入。  
> 如果给我 10 倍资源，我会先做队列化执行、多副本 + Postgres checkpointer、OTel + SLO、租户隔离和数据保留策略，而不是加更多 Agent 功能。

这段话会显得你懂生产，而不是只会写 demo。

---

## 7. 目标岗位怎么选

- 优先：Agent 平台、LLM 应用基础设施、AI 应用后端、效率工具；
- 次优：通用后端（强调状态机、事务、审计、幂等、测试）；
- 避免：纯算法/模型训练岗（项目不匹配）。

简历上不要写“健身 App”，写：

> “面向高风险计划变更的 Agent Harness：工具白名单、LangGraph HITL/checkpoint、独立 verifier、审计链、Evals/变异测试。”

---

## 8. 最后一句

> 你不需要把它做成工业级；你需要证明自己**知道工业级差在哪，并且已经用工程手段把可控部分做扎实**。  
> 这比“功能很多但讲不清边界”的项目强得多。
