# 11 进度复盘：做了什么、链路图、LangGraph 缺口

> 日期：2026-11-02  
> 结论：**核心垂直切片已经跑通，但 LangGraph 还没接。现在不是“Agent 图”，而是“确定性计划修复服务 + DeepSeek 解释器 + 审批状态机”。**

---

## 1. 一句话总结

我们做成了一条**可运行的 S04 闭环**：

> 膝痛 + 只能练 2 天 → 最小补丁（3/12，churn 25%）→ 安全门 → DeepSeek 解释 → 审批 → v2（11 个 action）→ 回滚 v3（12 个 action）。

但还没有：

- LangGraph `StateGraph`
- `interrupt` / checkpoint 恢复
- 20 场景 Evals / 三档矩阵 / 变异测试
- Console 的真实 Run/Trace
- RAG、上下文压缩、微信登录

所以你会觉得“好像没用到 LangGraph”——**确实没用**。这是当前最大的缺口，不是错觉。

---

## 2. 这两天实际改了什么

### 10/31：定位、骨架、环境

- 项目从“健身 Agent”收窄为 **PlanHarness：计划类 Agent 的 Harness 工程**。
- 定了核心矛盾：膝痛/时间变化下的**最小安全修复**，不是重新生成。
- 写了产品、页面、领域模型、PlanPatch、评测、部署等文档。
- 起了 FastAPI 后端骨架：SQLAlchemy、JWT、demo 登录、seed、health/profile/current plan。
- 配了 systemd + Nginx + Postgres 的服务器方案；确认域名/证书/备案限制。

### 11/01：核心业务闭环 + DeepSeek + 前端

- **计划生成**：热量/蛋白质计算、模板生成、validator。
- **补丁协议**：`replace_action` / `remove_action`，3 个操作，diff/churn。
- **安全门**：膝痛禁忌动作、训练天数，确定性拦截。
- **审批/版本**：审批前不写库；通过后生成 v2；拒绝保留 v1；回滚生成 v3。
- **打卡**：`POST/GET /checkins`。
- **DeepSeek**：`explain_patch`，只解释补丁，不改计划；失败自动 fallback。
- **前端**：uni-app H5 骨架，今天/打卡/审批/Console；修复 tabBar 不能 `navigateTo` 的问题。
- **测试**：20 个 pytest 全过。
- **真实验证**：DeepSeek 返回“改动/原因/风险/下一步”；v2 11 个 action；回滚 v3 12 个 action。

---

## 3. 当前架构链路

```text
┌──────────────────────────────┐
│ Web H5 / 微信小程序           │
│ uni-app + Vue3               │
│ 今天 / 打卡 / 审批 / Console  │
└──────────────┬───────────────┘
               │ HTTPS / Vite proxy
               ▼
┌──────────────────────────────┐
│ Nginx / Caddy（部署时）       │
│ 静态 H5 + /api 反代           │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ FastAPI  /api/v1              │
│ auth / profile / plans        │
│ patches / checkins            │
└──────┬───────────────┬───────┘
       │               │
       ▼               ▼
┌──────────────┐  ┌────────────────────────────┐
│ domain       │  │ llm                         │
│ planning.py  │  │ prompts.py                  │
│ patches.py   │  │ deepseek.py → DeepSeek API  │
│ 计算/校验/版本│  │ fallback.py（失败兜底）     │
└──────┬───────┘  └────────────────────────────┘
       │
       ▼
┌──────────────────────────────┐
│ SQLAlchemy                    │
│ 本地 SQLite / 服务器 Postgres │
└──────────────────────────────┘
```

**注意：** 这条链路里没有 LangGraph。  
FastAPI 直接调用 `create_knee_patch` / `approve_patch` 函数；审批靠 `PlanPatch.status` + API，不靠 `interrupt`。

---

## 4. S04 完整时序

```text
H5 今天页
  │
  ├─ POST /auth/demo
  │     └─ 返回 JWT
  │
  ├─ GET /plans/current
  │     └─ v1：12 个 action
  │
  ├─ POST /plans/{plan_id}/patches
  │     │
  │     ├─ 定位 3 个槽位
  │     │    squat → glute_bridge
  │     │    brisk_walk → easy_walk
  │     │    remove row
  │     │
  │     ├─ 安全门：禁忌动作 / 训练天数
  │     ├─ diff：changed 3 / total 12，churn 25%
  │     ├─ build_explanation_payload
  │     ├─ DeepSeekExplainer（失败 → fallback）
  │     └─ 写 PlanPatch，status = pending_approval
  │           ❗ 此时 plan_versions 不变，仍是 v1
  │
  ├─ 审批页 GET /patches/{patch_id}
  │
  ├─ POST /patches/{patch_id}/approve
  │     ├─ 重跑安全门
  │     ├─ 复制 base actions，应用 operations
  │     ├─ 创建 PlanVersion v2（11 个 action）
  │     ├─ 旧版本 superseded
  │     └─ 写 Approval
  │
  ├─ GET /plans/current
  │     └─ v2：11 个 action
  │
  └─ POST /plans/{id}/rollback {target_version: 1}
        └─ 创建 PlanVersion v3（12 个 action）
```

已实测：

- v1：12 actions
- patch：3/12，churn 0.25
- v2：11 actions，无 `goblet_squat`，有 `glute_bridge` / `easy_walk`
- v3：12 actions，原始计划恢复
- DeepSeek 解释：`改动 / 原因 / 风险 / 下一步`

---

## 5. 现在到底有哪些 Harness 能力

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 确定性安全门 | ✅ | 膝痛禁忌、训练天数，不依赖 LLM |
| 审批闸门 | ✅ | 审批前不写库；拒绝保留旧版 |
| 版本/回滚 | ✅ | 只增不改；回滚生成新版本 |
| LLM 失败兜底 | ✅ | 无 Key/超时/非法 JSON → fallback |
| 请求 trace_id | ✅ | 中间件生成，响应头返回 |
| 单元/集成测试 | ✅ | 20 个 pytest |
| LangGraph 图 | ❌ | 没有 `StateGraph` / `interrupt` / checkpoint |
| 任务级 Evals | ❌ | 20 个 pytest ≠ 20 个场景评测 |
| 三档矩阵/变异 | ❌ | 未实现 |
| Console 真实 Run/Trace | ❌ | 当前是静态骨架 |
| AgentRun 追踪 | ❌ | 表在，但没写入 |
| RAG / Context / 微信 | ❌ | P1/P2，未实现 |

---

## 6. 为什么没接 LangGraph

当时的取舍是：

> 先把“补丁、安全门、审批、版本、回滚”这些**业务不变量**跑通，再接图。

如果先上图，你会同时调试：

- LangGraph 状态传递
- interrupt/checkpoint
- 业务校验
- 数据库事务

容易两头都调不通。现在业务不变量已经稳定，接图反而简单：**把现有函数包成节点，不重写业务。**

但这确实导致当前项目更像“服务 + LLM 解释器”，不像“Agent 图”。这个缺口要在下一轮补上。

---

## 7. LangGraph 应该怎么接

目标图：

```text
START
  → load_plan
  → propose_patch（调用现有 create_knee_patch 的提案逻辑）
  → validate_patch（安全门 + diff）
        ├─ 不通过 → fallback_patch / END
        └─ 通过
  → human_approval（interrupt，等待用户）
        ├─ rejected → END
        └─ approved
  → commit_patch（生成新版本）
  → END
```

关键点：

- `thread_id = patch_id`。
- `interrupt` 前只写 `PlanPatch` 草稿，不写 `plan_versions`。
- 用户审批后 `Command(resume={"decision":"approve"})`。
- checkpoint 用 `InMemorySaver`（本地）/ `SqliteSaver`（跨进程）。
- `propose_patch` 失败走 fallback，不阻塞。
- 现有 `create_knee_patch` / `approve_patch` / `reject_patch` 不改业务，只包节点。

API 形态可以不变：

- `POST /plans/{id}/patches`：启动图，跑到 interrupt，返回 `patch_id` + `waiting_approval`。
- `POST /patches/{id}/approve`：`Command(resume=...)`，恢复图，提交 v2。
- `POST /patches/{id}/reject`：resume reject，结束。

这样才真正用上：

- `StateGraph`
- 条件边
- `interrupt`
- checkpoint / resume
- 有界重试/回退

---

## 8. 下一轮最小任务

### 任务 1：LangGraph 补丁图

- 新增 `app/agent/patch_graph.py`
- 用 `StateGraph` 包 5 个节点
- 用 `InMemorySaver` + `thread_id=patch_id`
- `interrupt` 放在审批节点
- 测试：
  1. 跑到 interrupt，plan 仍是 v1；
  2. resume approve，v2；
  3. resume reject，v1 不变；
  4. 重启进程后，SQLite checkpointer 能恢复。
- 完成标准：API 行为不变，但内部由 graph 驱动。

### 任务 2：Evals + Console

- 8 个场景先跑通：S04、S05、S15、S19、S20、红灯、过敏、正常。
- 指标：安全违规、churn、审批覆盖、fallback 成功率。
- Console 接真实 Run/Trace，不再静态。
- 再扩到 20 场景 + 三档矩阵 + 3 个变异。

### 任务 3：部署与演示

- H5 部署到 Nginx；小程序等 AppID。
- 域名备案通过后切正式域名；否则临时 HTTP/隧道演示。
- 录 5 分钟 backup。

---

## 9. 面试时怎么诚实地说

现在可以说：

> 我先做了计划修复的 Harness 垂直切片：确定性安全门、审批、版本回滚、LLM 只做解释、失败兜底、20 个测试。  
> LangGraph 编排是下一步，我会把补丁流程改成带 interrupt/checkpoint 的状态图，而不是把业务逻辑重写。

不要现在说：

> “我用 LangGraph 做了多 Agent 编排。”

因为代码里没有，面试官一搜就露馅。

---

## 10. 结论

- **做成了什么**：一条真实的“计划修复 + 安全 + 审批 + 回滚 + LLM 解释”闭环。
- **没做成什么**：LangGraph 图、任务级 Evals、Console 真实 Trace。
- **为什么迷茫**：因为项目现在更像“后端服务 + 模型调用”，还不是“Agent 图”。这是阶段性取舍，不是方向错了。
- **下一步**：把现有函数包成 LangGraph 节点，接 `interrupt + checkpoint`，这是最高优先级。
