# PlanHarness

> **面向计划类 Agent 的 Harness 工程，以 4 周减脂计划为测试床。**

状态：**全量 P0 一周冲刺进行中（2026-10-31 起，全职）。** 版本 v0.3 / 2026-10-31。旧的 ClaimGuard 审批项目仍保留在仓库根目录，两者不要混用。

---

## 一句话

用户第 2 周说“膝盖疼，这周只能练 2 天”。普通 Agent 会重新生成整份计划，可能塞回跑步/深蹲；PlanHarness 只做**最小安全补丁**，经过**确定性安全门**和**人工审批**后提交新版本，并可回滚。

## 这不是什么

- 不是“又一个生成健身计划的 Agent”
- 不是多 Agent 编排
- 不是 GraphRAG
- 不是医疗诊断/治疗工具
- P0 不做自由聊天、拍照识食、穿戴设备、自动点餐、支付

## 三个对比模式

| 模式 | 生成方式 | 安全门 | 审批 | 证明什么 |
| --- | --- | --- | --- | --- |
| A 裸奔 | 全量重新生成 | 无 | 无 | 计划漂移、越权写库、风险动作放行 |
| B 只加安全门 | 全量重新生成 | 有 | 有 | 安全了，但计划仍被大改 |
| C 补丁模式 | 最小补丁 | 有 | 有 | 安全 + 低 churn + 可回滚 |

## 三个展示面

| 面 | 作用 |
| --- | --- |
| Web Console | 面试主展示：场景、Trace、Diff、审批、指标。1 周版合并为 1 页 |
| Web 用户端 | 引导、计划、今天、打卡、进展、审批 |
| 微信小程序 | 今天、打卡、审批 3 页，证明同一后端多端可用 |

## Backend quickstart

```bash
cd planharness/backend
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

接口文档：<http://127.0.0.1:8000/docs>（已内置 Swagger UI 静态资源，**不依赖外网 CDN**，
断网也能打开；默认的 CDN 方案在国内会白屏，见 `backend/app/static/swagger/README.md`）

验证：

```bash
curl http://127.0.0.1:8000/api/v1/health
curl -X POST http://127.0.0.1:8000/api/v1/auth/demo -H 'Content-Type: application/json' -d '{}'
```

当前已实现：

- `GET /api/v1/health`
- `POST /api/v1/auth/demo`（demo JWT）
- `POST /api/v1/auth/wechat`（无 AppID，返回 501）
- `GET /api/v1/me/profile`
- `GET /api/v1/plans/current`（seed 的 v1，12 个 action）
- `POST /api/v1/plans/generate`（模板生成 + 热量/蛋白质计算 + validator，生成 v2）
- `PUT /api/v1/me/profile`（目标 / 体型 / 器械 / 过敏原 / 饮食偏好，直接喂给生成与安全门）
- `POST /api/v1/plans/drafts`（**生成计划草案**：模型调用 healthy-fitness-coach skill → 训练 + 食谱 + 摄入目标）
- `POST /api/v1/plans/drafts/{id}/feedback|confirm|reject`（再改改 ≤3 轮 / 确认落版本 / 不要了）
- `GET /api/v1/patch-presets`（预设改动入口：休息 / 受伤停练 / 换部位 / 加强度 / 调饮食 / 没时间）
- `POST /api/v1/plans/{plan_id}/patch-requests`（**一句话改计划**：模型定向修改 → 草案 / 追问 / 被安全门拦下）
- `POST /api/v1/patch-requests/{id}/feedback|approve|reject`（再改改 ≤3 轮 / 确认应用 / 拒绝）
- `POST /api/v1/plans/{id}/undo`（撤销上一次提交，仍生成新版本）
- `POST/GET /api/v1/workout-logs`、`GET /api/v1/stats/calories`（训练记录 → 热量，只算消耗）
- `POST /api/v1/plans/{id}/patches`（膝痛补丁，3/12，churn 25%，返回 `validation.explanation`）
- `POST /api/v1/patches/{id}/approve|reject`（审批前不写库；验证失败→409 并自动回滚）
- `POST /api/v1/plans/{id}/rollback`（回滚生成新版本）
- `POST/GET /api/v1/checkins`（打卡 upsert / 列表）
- `GET /api/v1/patches?status=`（补丁历史，审批页据此判断有没有待审批）
- `GET /api/v1/runs?status=`（最近的 Agent 运行列表，Console 用）
- `POST /api/v1/compare/knee-patch`（A/B/C 三模式对比，返回真实指标）
- `GET /api/v1/audit/{plan_id}`（审计哈希链：每条事件带 prev_hash/hash）
- `GET /api/v1/audit/{plan_id}/verify`（重算整条链，返回 valid / broken_at）
- `GET /api/v1/runs/{id}`（trace 时间线 + patch diff + 上下文快照 + 验证结论）

服务器部署（当前实际方案）：

- API：宿主机 systemd + venv，监听 `127.0.0.1:9200`
- Postgres：已有 `pg` 容器，映射 `127.0.0.1:5432`
- Nginx：对外 80/443，反代到 9200
- **不使用** `docker-compose.example.yml`（会重复起 Postgres/Caddy）

见 [deploy/README.md](deploy/README.md)。

## Frontend quickstart

```bash
cd planharness/frontend
npm install --registry=https://registry.npmmirror.com
npm run dev:h5
```

- H5：`http://localhost:5173`，Vite 把 `/api` 代理到 `127.0.0.1:8000`
- 构建：`npm run build:h5`，产物在 `frontend/dist/`
- 小程序：`npm run dev:mp-weixin`（需要 AppID；当前用开发者工具模拟器）
- 小程序请求域名必须是绝对地址：`VITE_API_BASE=https://www.agentworkout.online/api/v1`

当前页面（TabBar：今天 / 计划 / 进展 / 我的）：

- 今天：今天练什么（动作/组次/肌群）、今天吃什么（食材克数/热量）、推荐摄入、六个改动预设 + 「自己说一句」
- 计划：整周明细、今日餐食、待确认改动、撤销上一次改动、换一份计划
- 进展：近 7 天消耗、体重、训练记录（附热量公式与口径）
- 我的：目标 / 体型 / 器械 / 过敏原 / 饮食偏好可编辑，保存后直接生成计划
- 打卡：体重/睡眠/饥饿/酸痛/执行率 + 记一条训练（实时算 kcal）
- Console（H5）：场景回放、Trace、Diff、上下文、成本、审计链、A/B/C 对比
- 待确认补丁：旧的独立审批页保留，作为抽屉之外的兜底入口

「改计划」是底部抽屉而不是独立 tab：审批是动作的一部分，不该让用户再去找页面。
交互上有一条硬规则：**审批动作绝不自动跳转**（曾因自动切 tab + 按钮同位置导致连点两次直接提交）。

> Uni-app 当前安装包提示 Node >=22；本机 Node 20 也能 build，但建议升级到 Node 22。

## 评测（Evals）

不是"我写了测试"，而是**任务级回归 + 指标 + 失败归因 + 变异测试**：

| 产物 | 内容 |
| --- | --- |
| [evals/reports/latest.md](evals/reports/latest.md) | 11 个场景 × 10 个指标的实测报告（含与上次的趋势对比） |
| [evals/reports/mutations.md](evals/reports/mutations.md) | 变异测试：弄坏 6 个护栏，评测必须全抓住 |
| [evals/reports/latest.json](evals/reports/latest.json) | 机器可读版，给 CI / 趋势图用 |

Console 里可以一键跑 A/B/C 对比（数字来自实测），点开任意一个 run，能看到：**trace 时间线 → diff → 上下文 → 成本 → 审计链（可一键验证）→ 独立验证**。
审计链断了会直接显示 `断裂 @ #N` 和原因。

```bash
cd backend
python -m app.evals            # 跑 11 场景 → latest.{json,md}，并追加一行历史
python -m app.evals --mutants  # 跑变异测试 → mutations.{json,md}
python -m app.evals --only E04,E07   # 只跑子集（写 partial-*，不覆盖 latest）
```

指标：`safety_violation_rate` / `verification_pass_rate` / `approval_coverage` /
`plan_churn` / `context_dropped_required` / `fallback_success` /
`duplicate_commit_rate` / `avg_cost_cny_per_run` / `fallback_rate` / `p95_latency_ms`。

Console 里还有「场景回放」：点 E01–E08 任一个，服务端跑一次评测并当场展示断言与成本
（用临时库 + mock 模型，不写你的数据）。

设计说明见 [04 评测与交付 §8.5](docs/04-evals-and-delivery.md)。

## 文档索引

- [01 产品与 MVP](docs/01-product-and-mvp.md)：定位、范围、用户旅程、演示脚本、验收
- [02 页面线框](docs/02-ux-wireframes.md)：Console 3 页、Web 用户端、小程序 3 页
- [03 领域模型与协议](docs/03-domain-and-protocol.md)：表结构、PlanPatch、Agent 图、API、权限
- [04 评测与交付](docs/04-evals-and-delivery.md)：完整版：20 场景、三档矩阵、变异测试、3 周计划（backlog）
- [05 一周冲刺（裁剪版）](docs/05-one-week-sprint.md)：8 场景、1 页 Console、3 变异；作为进度失控时的回退方案
- [06 全量 P0 冲刺](docs/06-full-scope-sprint.md)：**当前执行版**：20 场景、3 页 Console、周复盘、餐食库、真实 LLM/微信/Postgres
- [07 环境与部署](docs/07-environment-and-deploy.md)：域名、DeepSeek、Docker、HTTPS、无 AppID 的小程序策略
- [08 AI Key 安全与网关](docs/08-ai-gateway-security.md)：用 Worker/SCF 隔离 DeepSeek Key，服务器只持可撤销 token
- [09 最简单网关操作](docs/09-simple-gateway-walkthrough.md)：Cloudflare Worker 逐步操作 + 最小代码 + 验证命令
- [10 代码地图与流程图](docs/10-code-map-and-flows.md)：**给不熟 Python 的人**：目录结构、Python↔Java 对照、架构图、膝痛补丁全流程图
- [11 进度复盘](docs/11-progress-review.md)：这两天做了什么、完整链路图、LangGraph 缺口与下一步
- [13 LangGraph 接入](docs/13-langgraph-integration.md)：状态设计、节点包装、interrupt/checkpoint、API 改法与测试
- [14 Harness 控制平面](docs/14-harness-control-plane.md)：生命周期、上下文、状态、验证、工具治理、审计、成本的差距与路线
- [16 Graph API 接入模板](docs/16-graph-wiring-template.md)：patch_id/thread_id、路由改写、resume、测试模板
- [17 Run 生命周期与 Verifier](docs/17-run-lifecycle-and-verifier.md)：AgentRun 状态机、独立验收、测试模板
- [18 剩余工作清单](docs/18-remaining-work.md)：Evals、Console Trace、token/cost、audit、生产收口与优先级
- [19 当前阶段与最终优先级](docs/19-final-priorities.md)：90 tests/8 Evals/6 变异已完成，剩余前端账号、CI、部署、演示
- [12 架构总结](docs/12-architecture.md)：**架构风格判定**（分层单体/贫血模型，不是 DDD）、分层与依赖规则、聚合边界、状态机、9 条 ADR、技术债与演进路线、面试讲稿
- [15 上线前修改清单](docs/15-prelaunch-checklist.md)：**上线前逐条过**：10 个阻断项 + 8 个建议项 + 验证/回滚；配套 `deploy/preflight.sh` 自检
- [21 skill 驱动的计划生成](docs/21-skill-driven-plan.md)：**本轮**：Node 桥接 healthy-fitness-coach、模型编排 + 本地安全门、生成草案 → 确认 → 落版本、补丁改为用户自定义
- [20 一句话改计划](docs/20-flexible-patch-flow.md)：**本轮改版**：可协商草案、新增 3 张表与 7 个接口、热量口径、TabBar 与抽屉交互、已验证/未验证
- [生产部署模板](deploy/README.md)：systemd + Nginx + 现有 Postgres 容器（当前实际方案）

## 已锁定决策

1. **P0 只做“4 周减脂 + 计划修复”**，不做增肌/康复/医疗解读。
2. 核心场景：第 2 周膝痛 + 只能练 2 天 → 最小补丁 → 安全门 → 审批 → v2 → 可回滚。
3. 只做“计划 + 打卡 + 审批 + 简单餐食建议”，不做自动点餐/买菜。
4. Console 3 页，小程序 3 页；Console 只在 Web。
5. **P1 才做上下文 Harness**（Goal/Constraint 账本、Context Pack、压缩评测）；P0 只记录 `ContextSnapshot` 口子。
6. **P2 才做 RAG/健身计划扩展**；P0 只留 `KnowledgeProvider`、`Evidence`、`PolicyPack` 接口。
7. 演示数据使用合成数据，不爬电商/不做真实医疗数据。
8. 默认后端 Python + FastAPI + LangGraph，前端 Vue3 + uni-app；Java 经验用于契约、状态机、权限、测试。
9. **1 周全量冲刺**：Postgres、真实 LLM + 模板兜底、真实微信登录 + demo 兜底、20 场景、3 页 Console、5 变异。

## 待确认

- 小程序 AppID：**暂无**。H5 主演示；小程序代码/模拟器并行；AppID 下来后接真实登录。
- 域名：`www.agentworkout.online`，备案完成；待配 DNS A 记录 + Caddy HTTPS。
- LLM：DeepSeek；Key 写服务器 `.env`，不要发聊天。
- 服务器：无 Docker，今天安装；失败走原生 systemd。
- 后端是否坚持 Java；若坚持，Spring Boot BFF + Python Agent 服务，工期增加。
- 已确认：全职 1 周（2026-10-31 ～ 2026-11-06），11/07 缓冲。

## 下一步

已完成：教练 skill 桥接（训练 + 食谱 + 摄入）、画像录入、计划草案多轮确认、
用户自定义补丁（预设 + 自由文本，模型定向修改 + 确定性校验）、训练记录与热量统计、
TabBar 四页、Console 观测面、11 场景 Evals + 6 变异体、审计链。

1. 浏览器里过一遍五页与两个抽屉（当前环境没有浏览器，只做了构建期检查）。
2. 用真实 `DEEPSEEK_API_KEY` 跑一次生成，统计模型 tool-calling 的成功率与成本。
4. `docs/15` 的服务器侧阻断项：备份定时器、Postgres 超级用户收敛、端口核验。
