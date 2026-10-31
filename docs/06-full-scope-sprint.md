# 06 全量 P0 一周冲刺（2026-10-31 ～ 11-06）

> 执行版：**不砍 P0 功能**。  
> 边界：P1（Context/聊天）和 P2（RAG/增肌）保留接口与文档，不在本周实现；否则 7 天物理上做不完。

## 环境状态（2026-10-31 14:50）

| 项 | 状态 | 处理 |
| --- | --- | --- |
| 域名 | `www.agentworkout.online`，备案完成 | Caddy 自动 HTTPS |
| LLM | DeepSeek | Key 写服务器 `.env`，不要提交/发聊天 |
| 服务器 | 无 Docker，可安装 | 今天装；失败走原生 systemd |
| 小程序 AppID | 没有 | H5 主演示；小程序代码/模拟器并行；真实登录等 AppID |

详见 [07 环境与部署](07-environment-and-deploy.md)。

## 0. 一句话

一周内交付 **全量 P0 演示版**：

> 20 场景 + 3 页 Console + 周复盘 + 餐食库 + 真实 LLM 计划 + 微信登录代码 + Postgres + Web/小程序 + A/B/C 矩阵 + 5 个变异。

不是“功能清单走过场”，是**每条都有可演示路径和验收断言**。

---

## 1. 本周范围

### 做（全量 P0）

| 模块 | 内容 |
| --- | --- |
| 计划 | 4 周减脂，真实 LLM 生成 + 模板兜底 |
| 周复盘 | 第 7 天趋势 + 调整建议 + 审批 |
| 餐食 | 30 个食物/餐食模板，3 餐/天，热量/蛋白质/过敏原 |
| 修复 | 膝痛、时间变化、目标变化（目标变化只做版本切换 + 确认） |
| 安全 | 红灯/黄灯、热量下限、过敏原、禁忌动作 |
| 审批 | 计划确认 + 补丁审批 + 拒绝/回滚 |
| Console | Scenarios / Run / Quality 3 页 |
| Evals | 20 场景 + 三档矩阵 + 5 变异 |
| 端 | Web H5 + 微信小程序 3 页 |
| 部署 | Postgres + Docker + Caddy + HTTPS |
| 登录 | 微信 code2session + demo 账号 |

### 本周不实现（路线图保留）

| 项 | 原因 |
| --- | --- |
| Context 压缩 / 教练聊天 | P1；本周只落 `context_snapshots` 字段 |
| RAG / pgvector / 增肌 | P2；本周只落 `KnowledgeProvider`、`PolicyPack` 接口 |
| 自动点餐/支付/穿戴 | 非 P0 |

> 这不是砍功能，是阶段边界。若你要求 P1/P2 也在本周可演示，7 天不够，需要再加一周。

---

## 2. 三轨并行

| 轨道 | 负责人 | 内容 |
| --- | --- | --- |
| A 后端/Agent | 你 | DB、domain、LLM、LangGraph、API、部署 |
| B 前端 | 我 | Console 3 页、H5 用户端、小程序 3 页、API client |
| C 评测 | 共同 | 20 场景、矩阵、变异、演示脚本 |

每天 10～12 小时；前端必须并行，否则做不完。

---

## 3. 7 天排期

| 日期 | 后端（你） | 前端（我） | 共同验收 |
| --- | --- | --- | --- |
| 10/31 周六 | Postgres、表结构、seed、demo/微信登录、health | uni-app 骨架、3 个 tab、Console 路由、mock API | API 能起，demo 用户能登录 |
| 11/1 周日 | 真实 LLM 计划 + 模板兜底、validator、餐食库、周复盘表 | 引导/计划/今天页；Console 场景列表 | v1 计划 + 3 餐 + 周复盘草稿 |
| 11/2 周一 | PlanPatch、安全门、版本/审批/回滚 | 审批页、diff 组件、Console Run 页 | S04 补丁 3/12，审批前不写库 |
| 11/3 周二 | LangGraph HITL、plans/checkins/patches API、20 fixtures | Trace/Diff 联调、小程序壳 | API 走通 v1→v2→rollback |
| 11/4 周三 | 周复盘、餐食建议、20 场景 runner | Console Quality、小程序 3 页 | 20 场景报告可跑 |
| 11/5 周四 | 三档矩阵、5 变异、Postgres 收尾 | 全端联调、图表、小程序模拟器 | mutation 5/5，H5 可用；小程序本地可跑 |
| 11/6 周五 | Docker/Caddy、demo 重置、公网验证 | 演示数据、彩排修 bug | 公网 HTTPS，5 分钟演示 |
| 11/7 周六 | 缓冲、录像、文档 | 缓冲 | 可脱稿 |

---

## 4. 每日 Gate

### Day 0（今天）

必须跑通：

```text
GET /health -> ok
POST /auth/demo -> token
GET /me/profile -> demo 用户
GET /plans/current -> 预置 v1
```

不跑通，不睡觉。

### Day 1

- 真实 LLM 生成 v1；失败自动模板兜底。
- validator 拦住非法热量/过敏原/器械。
- 3 餐/天入库。
- 周复盘接口返回草稿。

### Day 2

- S04 补丁：3/12，churn 25%。
- 关安全门，S04 测试变红。
- 审批前 commit 返回 403/409。
- 拒绝审批，v1 不变。

### Day 3

- API 全流程：v1 → patch → approve → v2 → rollback。
- `interrupt` 能暂停/恢复。
- 20 个 scenario fixture 全部加载。

### Day 4

- 20 场景跑完，失败有 tag + trace。
- 小程序 3 页能打卡/审批。

### Day 5

- A/B/C 矩阵出表。
- 5 个变异全部被杀死。
- 微信登录成功（AppID 可用时）。

### Day 6

- 公网 HTTPS。
- demo 重置。
- 5 分钟演示 2 遍不超时。

---

## 5. 全量 P0 验收

- [ ] 20 场景报告，状态/路径断言齐全。
- [ ] A/B/C 三档：100% / ~92% / 25% churn。
- [ ] 安全违规 0（B/C）。
- [ ] 审批覆盖 100%。
- [ ] 拒绝审批不产生 v2；回滚成功。
- [ ] 5 变异 5/5。
- [ ] 周复盘能出建议并审批。
- [ ] 餐食 3 餐/天，热量/蛋白质/过敏原正确。
- [ ] 真实 LLM 计划 + 模板兜底。
- [ ] 微信登录代码 + demo 登录；无 AppID 时以 H5 演示，AppID 下来后 0.5 天接入。
- [ ] Web H5 走通 S04；小程序在开发者工具模拟器走通 S04。
- [ ] 公网可访问，demo 可重置。

---

## 6. 今天（10/31）任务

### 你：后端

1. Docker 起 Postgres；`.env`。
2. 表结构：users/profiles/goals/constraints/plans/plan_versions/plan_actions/plan_patches/approvals/checkins/reviews/agent_runs。
3. seed：demo 用户 + 1 份 v1 计划 + 3 餐。
4. `/health`、`/auth/demo`、`/auth/wechat`（先 code2session 骨架）、`/me/profile`、`/plans/current`。
5. 启动 README。

### 我：前端

1. uni-app + Vue3 骨架。
2. TabBar：今天 / 打卡 / 审批。
3. Console 路由壳。
4. mock API + 类型。
5. 明天接引导/计划页。

### 需要你发我

- 小程序 AppID / AppSecret（没有就先用 demo token，代码路径保留）。
- LLM provider/base_url/key/model（没有就 FakeLLM + fixture）。
- 服务器域名/HTTPS 是否就绪。

---

## 7. 进度落后时

不砍功能，但可以**调整顺序**：

1. 先保 S04 + A/B/C + 20 场景 + 5 变异；
2. 周复盘/餐食可以最后接 UI；
3. 微信登录若被 AppID/备案卡住，代码先写，演示用 demo token；
4. RAG/Context 不在本周，别提前做。

**每天 22:00 对照 Gate；不过就第二天补，不把问题留到周五。**
