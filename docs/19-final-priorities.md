# 19 当前阶段与剩余优先级（2026-11-04）

## 0. 一句话

> 代码层已经到 L3.5：LangGraph、AgentRun、verifier/自动回滚、ContextBuilder、Evals、变异测试、审计链、成本预算、Console 真实 Trace 都已落地。  
> 剩下的主要是 **产品入口、CI、部署、演示**，不是再补 Harness 机制。

当前测试：**90 passed**（本地/临时库）。

---

## 1. 已经完成

| 能力 | 证据 |
| --- | --- |
| LangGraph interrupt/checkpoint | `app/agent/patch_graph.py` + `test_agent_graph` |
| AgentRun 生命周期 | waiting/committed/rejected/failed |
| 独立 verifier + 自动回滚 | `app/agent/verify.py` + E07 |
| 上下文预算/截断/快照 | `app/llm/context.py` + `test_context` |
| 8 场景 Evals | `evals/reports/latest.md` |
| 变异测试 6/6 | `evals/reports/mutations.md` |
| 审计哈希链 | `app/audit.py` + `/audit/{id}/verify` |
| 真实 token/cost | `ExplainResult` + `LlmBudget` |
| Console 真实 Run/Trace | `/runs` + `frontend/src/pages/console` |
| 注册/登录/JWT 撤销 | `app/auth.py` + `test_auth` |

---

## 2. 剩余工作

### P0：前端账号入口

后端已有：

```text
POST /auth/register
POST /auth/login
```

前端仍然只有 `ensureToken()` 自动 demo 登录，没有登录/注册页面。

要做：

- 新增 `pages/auth/index.vue`（登录 / 注册 / 体验账号）；
- API client 加 `register` / `login` / `clearToken`；
- 启动时没有 token 就进 auth 页；
- 保留“体验账号”按钮，面试演示不被注册流程卡住；
- 登录成功后按角色进入今天页。

验收：

```text
新手机号注册 → 自动登录 → /me/profile 200
旧手机号重复注册 → 409
错误密码 → 401，话术与不存在账号一致
体验账号 → 直接进入
```

### P0：CI 门禁

现在 Evals/变异测试是手动跑的，应该进 CI：

```text
uv sync
uv run pytest
uv run python -m app.evals
uv run python -m app.evals --mutants
```

- 任何指标不达标 / 变异未被杀死 → CI 失败；
- 上传 `evals/reports/` 为 artifact；
- 这是“Harness 闭环”的最后一块证据。

### P0：部署与迁移

代码是本机跑通的，服务器还需要：

- 拉最新代码；
- `phone/password_hash/token_version` 的新列迁移（`create_all` 不会改旧表）；
- `/etc/planharness/planharness.env` + `SECRET_KEY`；
- systemd/Nginx/端口；
- 备份 timer / checkpoint 清理 timer；
- `preflight.sh` 全绿。

注意：域名 ICP 还在审核，外部 SNI 可能仍被拦；演示可先用本机/临时入口，正式域名等通过。

### P1：演示收口

- 5 分钟演示脚本（今天 → 补丁 → 审批 → v2 → 回滚 → Console → Evals）；
- demo 数据重置（别让版本号无限增长）；
- 小程序 `build:mp-weixin` 至少模拟器能跑；没有 AppID 不做真机；
- 录一份 5 分钟 backup 视频；
- 更新 README 的“当前状态”（现在还写着“一周冲刺进行中 / Console 骨架”）。

### P1：安全与边界

- 登录/注册限流（防密码爆破）；
- `SECRET_KEY` 生产随机化；
- DeepSeek Key 低余额/网关（方案 B 已接受）；
- `/docs` 是否公网开放；
- 备份异地一份。

### P2：可做可不做

- 20 场景（现在 8 个已覆盖核心，不必为数量而数量）；
- max_steps/deadline（图是短流程，当前 recursion_limit + 预算够用）；
- ToolRegistry（当前没有真正工具调用，不要为了概念硬加）；
- RAG / 长期记忆 / 多 Agent / MCP（都不做）。

---

## 3. 建议顺序

如果目标是面试：

| 顺序 | 工作 | 时间 |
| --- | --- | --- |
| 1 | 前端登录/注册页 | 半天 |
| 2 | CI（pytest + evals + mutants） | 1 小时 |
| 3 | 演示脚本 + demo reset + 录屏 | 半天 |
| 4 | 服务器部署/迁移（域名可用后） | 1 天 |
| 5 | 限流/备份/安全收口 | 半天 |

如果目标是上线：先做部署 A 类，再做前端账号和 CI。

---

## 4. 不要再做

- 不要加 RAG；
- 不要加多 Agent；
- 不要加 MCP；
- 不要为了“看起来像平台”做工作流编辑器。

现在缺的是**演示完成度**和**上线收口**，不是功能数量。
