# 05 一周冲刺（裁剪版 / 回退方案）

> **当前执行版是 [06 全量 P0 冲刺](06-full-scope-sprint.md)。** 本文是进度失控时的回退方案，不是当前排期。

## 0. 结论

**一周全职可以交付一个可演示的垂直切片，但不能交付完整 P0。**

演示目标只有一句话：

> 第 2 周膝痛 + 只能练 2 天 → A 裸奔重生成 / B 安全门 / C 最小补丁 → 审批 → v2 → 回滚；Console 能看到 trace、diff、指标；8 条 Evals + 3 个变异测试通过；Web 和小程序都能走通。

这不是完整产品，是**面试演示版**。所有“未来能力”只留接口，不实现。

---

## 1. 一周必须有的东西

| 必须 | 验收 |
| --- | --- |
| S04 膝痛补丁 | 3/12 变更，churn 25%，无跑跳/深蹲 |
| A/B/C 三档 | 同一场景并排对比：100% vs 92% vs 25% churn |
| 安全门 | 关闭后 S04/S13 必须变红 |
| 审批 | 审批前不写库；拒绝后 v1 不变 |
| 版本/回滚 | v2 生效；可回滚 v1 |
| 8 条 Evals | 状态/路径断言；失败有标签 |
| 3 个变异 | 安全门、审批、补丁引擎；mutation 3/3 |
| Console 1 页 | 场景、Run、Trace、Diff、指标 |
| Web 用户端 | 引导/计划/今天/审批，H5 可演示 |
| 小程序 3 页 | 今天、打卡、审批 |
| 部署 | 公网 HTTPS，demo 重置 |
| 5 分钟演示 | 能脱稿讲完 |

---

## 2. 明确砍掉（本周不做）

| 砍掉 | 原因 | 留什么 |
| --- | --- | --- |
| 20 场景 | 时间不够 | 8 场景，覆盖核心风险 |
| 3 页 Console | 时间不够 | 1 页合并版 |
| 周复盘 | 不是核心矛盾 | 接口/表保留 |
| 餐食数据库/食物 RAG | 不是核心矛盾 | 3 餐模板卡 |
| RAG / pgvector | P2 | `KnowledgeProvider` 空接口 |
| Context 压缩 / 聊天 | P1 | `ContextSnapshot` 字段 |
| 增肌/康复 | P2 | `PolicyPack` 接口 |
| 真实微信登录 | 时间风险 | demo token；有 AppID 再加 |
| PostgreSQL | 部署成本 | SQLite；SQLAlchemy 可切 |
| 真实 LLM 生成完整计划 | 不稳定 | 模板计划 + LLM 只提补丁 |
| ECharts 大屏 | 非核心 | 简单 CSS 图表/数字 |
| 自动点餐/支付/推送 | 非核心 | 不做 |

> 这些不是删除，是**放进 backlog**。面试时主动说“这是垂直切片，完整设计在文档里”。

---

## 3. 一周技术决策

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 数据库 | SQLite（SQLAlchemy） | 零部署；单用户 demo 足够 |
| 认证 | demo token | 跳过微信 code2session；小程序也能用 |
| 计划生成 | 确定性模板 | 可复现，避免 LLM 不稳定拖垮进度 |
| 补丁生成 | LLM 可选 + 模板兜底 | 保留 Agent 味；失败不影响演示 |
| 图 | 只做“补丁图”用 LangGraph | HITL/checkpoint 是核心；计划生成先用函数 |
| 前端 | uni-app + Vue3 | 一套代码出 H5 + 小程序 |
| Console | 1 页三栏 | 场景/run/diff/metrics 合并 |
| 部署 | Docker + Caddy + SQLite volume | 简单 HTTPS |
| LLM | OpenAI 兼容 / FakeLLM | 有 Key 用真模型，没有就用 fixture |

**关键取舍：计划生成不是本周的研究对象，计划修复才是。**  
所以 v1 计划用模板生成；LLM 只负责“补丁候选 + 解释”。这样 demo 可复现，Harness 仍是主角。

---

## 4. 7 天排期

> 今天 2026-10-31 周六，按全职 8～10 小时/天；11/6 周五晚演示，11/7 周六缓冲。

| 日期 | 重点 | 当天必须交付 | 验收门槛 |
| --- | --- | --- | --- |
| 10/31 周六（今天） | 骨架 + 数据 | 仓库结构、DB 表、seed、demo 登录、`/health`、计划 schema | 本地 API 能启动，demo 用户能登录 |
| 11/1 周日 | 计划 + 校验 | 模板生成 v1、热量/蛋白质计算、validator、S01 | CLI 能生成并通过校验 |
| 11/2 周一 | 补丁 + 安全门 | slot/diff/churn、PlanPatch、安全门、版本、审批 token | S04 能出补丁，3/12，安全门能拦 |
| 11/3 周二 | 图 + API | LangGraph 补丁图、interrupt/checkpoint、plans/checkins/patches/approve API | S04 走 API 完成 v1→v2→回滚 |
| 11/4 周三 | Console + H5 | 1 页 Console、H5 用户端 4 页、A/B/C 切换 | 浏览器完整演示 |
| 11/5 周四 | 小程序 + Evals | 小程序 3 页、8 场景 runner、指标 | 小程序能打卡/审批；eval 报告出来 |
| 11/6 周五 | 矩阵 + 变异 + 部署 | A/B/C 矩阵、3 变异、Docker/Caddy、demo 重置 | 公网 HTTPS，mutation 3/3 |
| 11/7 周六 | 缓冲/录像 | 修 bug、录 5 分钟 backup、README | 能脱稿演示 |

---

## 5. 每天任务拆解

### Day 0：10/31 周六（今天）

**目标：能启动、能登录、有数据。**

- [ ] 建目录：`backend/`、`frontend/`、`docs/`、`data/`
- [ ] FastAPI 启动 + `/health`
- [ ] SQLite + SQLAlchemy 表：users, profiles, goals, constraints, plans, plan_versions, plan_actions, plan_patches, approvals, checkins, agent_runs
- [ ] seed：1 个 demo 用户、5 个动作、3 个餐食模板、1 份 v1 计划
- [ ] `POST /auth/demo` 返回 token；`GET /me/profile`
- [ ] 计划 DTO/JSON schema（先不接 LLM）
- [ ] 前端 uni-app 空壳，能请求 `/health`

**验收**

```text
GET /health -> ok
POST /auth/demo -> token
GET /me/profile -> demo 用户
GET /plans/current -> 预置 v1
```

**今天不做**：LangGraph、LLM、Console、小程序页面。

### Day 1：11/1 周日

**目标：v1 计划能生成、能校验。**

- [ ] 热量/蛋白质/步数计算（代码，不让 LLM 算）
- [ ] 模板计划生成器：3 个训练日 + 2 个快走 + 3 餐
- [ ] validator：热量下限、蛋白质、训练天数、休息日、器械、过敏原
- [ ] safety_screen：红灯/黄灯
- [ ] S01 场景 CLI 跑通
- [ ] 计划保存到 `plan_versions`/`plan_actions`

**验收**：S01 生成 v1，validator 通过；改一个非法值能被拒绝。

### Day 2：11/2 周一

**目标：补丁协议 + 安全门。**

- [ ] `slot_key`、`plan_actions` 归一化
- [ ] `PlanPatch` 信封 + 6 种操作
- [ ] diff：changed/total/churn/unnecessary_churn
- [ ] 安全门：膝痛禁止跑跳/深蹲；过敏一票否决
- [ ] 版本：v1→v2；旧版 superseded
- [ ] approval token；审批前不能 commit
- [ ] S04 CLI 跑通

**验收**：S04 生成 3 个操作，churn 25%；关掉安全门，测试变红；审批前 commit 返回 403/409。

### Day 3：11/3 周二

**目标：LangGraph + API。**

- [ ] 补丁图：load → propose → validate → diff → approval → commit
- [ ] `interrupt` 暂停；resume 后提交
- [ ] plan 生成先保持函数调用，不强行上图
- [ ] API：`/plans/current`、`/checkins`、`/plans/{id}/patches`、`/patches/{id}/approve|reject`、`/plans/{id}/rollback`、`/runs/{id}`
- [ ] `agent_runs` 记录 trace
- [ ] S04 通过 API 完成全流程

**验收**：Postman/curl 可以 v1→patch→approve→v2→rollback。

### Day 4：11/4 周三

**目标：能看的页面。**

- [ ] Console 1 页：场景选择、A/B/C、trace、diff、指标、审批
- [ ] H5 用户端：引导、计划、今天、审批
- [ ] A/B/C 切换：A 全量重生成、B 安全门、C 补丁
- [ ] 失败/安全拦截/待审批状态
- [ ] 录一条 30 秒屏

**验收**：浏览器完整走 S04；A/B/C 差异肉眼可见。

### Day 5：11/5 周四

**目标：小程序 + Evals。**

- [ ] 小程序 3 页：今天、打卡、审批
- [ ] demo token 登录；有 AppID 再加微信登录
- [ ] 8 场景 runner：状态断言 + 路径断言
- [ ] 指标：安全违规、churn、审批覆盖、fallback
- [ ] 失败标签 + 报告 JSON/MD

**验收**：小程序能打卡/审批；`eval` 输出 PASS 或明确的失败列表。

### Day 6：11/6 周五

**目标：证据 + 上线。**

- [ ] A/B/C 矩阵跑 8 场景
- [ ] 3 变异：关安全门、关审批、关补丁
- [ ] mutation 3/3
- [ ] Docker Compose + Caddy + SQLite volume
- [ ] demo 重置接口
- [ ] 公网 HTTPS 验证
- [ ] 5 分钟演示彩排 2 遍

**验收**：公网可访问；S04 可演示；mutation 3/3；演示不超时。

### Day 7：11/7 周六（缓冲）

- [ ] 修最后 bug
- [ ] 录 5 分钟 backup 视频
- [ ] 补 README/架构图/评测报告
- [ ] 准备面试 30 秒/5 分钟/15 分钟版本

---

## 6. 8 条 Evals

| ID | 场景 | 断言 |
| --- | --- | --- |
| E01 | 正常减脂 | v1 合规；热量/蛋白质达标；审批后生效 |
| E02 | 膝痛 + 只能练2天 | 3/12 变更；无跑跳/深蹲；churn≤30%；审批后 v2 |
| E03 | 4天变2天 | 只改训练日；保留动作≥70% |
| E04 | 孕期红灯 | 不生成计划；建议就医 |
| E05 | 花生过敏 | 餐食排除；一票否决 |
| E06 | 审批拒绝 | v1 保持；无 v2；审计 |
| E07 | LLM 超时 | 模板兜底；给出安全补丁；不崩溃 |
| E08 | LLM 非法 JSON | 重试一次；失败则模板兜底 |

> 8 条足够证明“安全、补丁、审批、恢复”四件事；不追求统计显著性。

---

## 7. 三档矩阵 + 三个变异

### 矩阵

| 指标 | A 裸奔 | B 安全门 | C 补丁 |
| --- | ---: | ---: | ---: |
| 安全违规 | ≥1（固定 fixture） | 0 | 0 |
| churn | 100% | ~92% | 25% |
| 审批覆盖 | 0% | 100% | 100% |
| 可回滚 | 否 | 是 | 是 |

### 变异

| 变异 | 关闭 | 预期失败 |
| --- | --- | --- |
| M1 | 安全门 | E02/E05 |
| M2 | 审批 token | E06 |
| M3 | 补丁引擎 | E02/E03（churn 变 100%） |

`mutation_score = 3/3`。  
如果某个变异没被杀，补断言，不要改代码掩盖。

---

## 8. 5 分钟演示脚本

1. 打开 Console，选 E02。
2. 点 Run A：全量重生成，12/12 变化，跑步/深蹲未被拦，直接 commit。
3. 点 Run B：安全门拦住，但仍 11/12 变化，审批后生效。
4. 点 Run C：3/12 变化，diff 清晰，审批后 v2，回滚 v1。
5. 打开 Quality：安全 0、churn 25%、审批 100%、mutation 3/3。
6. 小程序：今天/打卡/审批。
7. 一句话收尾：**“健身是测试床，我验证的是计划变更的可控性。”**

---

## 9. 一周 DoD

- [ ] S04 在 Web 和小程序都能走通。
- [ ] A/B/C 三档可切换，差异可见。
- [ ] 审批前不写库；拒绝后 v1 不变；v2 可回滚。
- [ ] 8 条 Evals 全绿或有明确失败归因。
- [ ] 3 个变异全部被杀死。
- [ ] 公网 HTTPS 可访问；demo 可重置。
- [ ] 5 分钟演示能脱稿。

**不满足以上任一项，不叫完成。**

---

## 10. 砍单顺序（进度落后时）

1. 小程序 → 先 H5，录小程序视频。
2. Console 的 trace 详情 → 只保留节点状态和 diff。
3. E03/E07 → 保留 E02/E04/E05/E06/E08。
4. 真 LLM → 全用 FakeLLM + fixture。
5. 周复盘/餐食 → 保留计划与补丁。

**绝不能砍**：安全门、审批、版本/回滚、E02/E06、mutation。

---

## 11. 今天下午就能开始

- 13:30–14:00：确认技术决策（SQLite、demo token、模板计划、uni-app）。
- 14:00–17:00：建表 + seed + `/health` + `/auth/demo` + `/me/profile`。
- 17:00–18:00：计划 DTO + 预置 v1。
- 19:00–22:00：uni-app 空壳 + 请求 `/health`；写 README 启动说明。
- 22:00：跑一遍验收命令，没跑通就砍掉非核心。

---

## 12. 分工

- 你：DB、domain、validator、PlanPatch、LangGraph、API、部署。
- 我：Console/H5/小程序页面、API client、seed/演示数据、Evals 断言、联调排错。
- 每天结束用 10 分钟对一次“今天验收过了吗”，没过就按砍单顺序处理。
