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
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

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
- 5 个 pytest

服务器部署：

```bash
cd planharness
cp .env.example .env
# 编辑 .env：POSTGRES_PASSWORD / SECRET_KEY / DEEPSEEK_API_KEY
docker compose up -d --build
```

> 没有 AppID 时小程序用模拟器；H5 由 Caddy 提供。

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

1. 今天先落表结构、seed、`/health`、`/auth/demo`、`/me/profile`。
2. 明天做真实 LLM 计划 + 模板兜底 + validator。
3. 后天做 PlanPatch + 安全门。
4. 按 [06 全量 P0 冲刺](docs/06-full-scope-sprint.md) 的三轨排期推进；每天 22:00 对照 Gate。
