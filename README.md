# Coach Agent / PlanHarness

面向训练、饮食和日常执行的教练应用。用户通过统一对话调整安排、记录训练、管理档案与记忆；计划与订单修改按对应能力的确认、版本和验证规则执行。

前端使用 Vue 3 + TypeScript + uni-app（H5 / 微信小程序）；后端使用 Python 3.12+、FastAPI、SQLAlchemy。开发可用 SQLite，部署模板使用 PostgreSQL。模型通过结构化输出理解目标，服务端绑定真实业务对象并执行能力；前端不负责意图识别。

## 当前功能与边界

- 今天、计划、进展、档案、打卡与训练执行页面共享同一教练入口。
- 聊天主链为 `coach_agent → semantic_runtime → semantic_planner / capability registry → 领域执行边界`。旧规则意图路由不进入生产聊天。
- 支持训练/食物局部修改、档案与记忆、训练会话生命周期、整日草案预览/应用和商务点餐。具体可用能力取决于上下文、凭证、配置及注册表，不能只按页面文案判断。
- Agent 运行工作台复用聊天接口，展示真实会话、任务、事件、快照与已关联执行日志。全局目标验证、逐工具事务证据、实时事件流尚未完整实现。
- 独立计划生成、LangGraph 补丁审批、知识检索与旧评测接口仍存在；它们不是聊天的语义 fallback，也不应因“不在聊天中”而删除。
- 商务能力依赖官方 MCP、凭证和下单开关。外部未知结果需核实；不能靠重复请求解决，也不会自动完成支付。

## 快速启动

后端（仓库根目录）：

```bash
cd backend
uv sync --group dev
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

前端（另一个终端，Node >= 22.12）：

```bash
cd frontend
npm ci
npm run dev:h5
```

前端默认 5173，开发代理指向 8000；健康检查 `/api/v1/health`，本地 Swagger `/docs`。环境模板见 [.env.example](.env.example)。启动后端会初始化所配置的数据库、种子数据及加法迁移；测试请使用临时数据库，勿指向生产库。

## 文档入口

- [当前架构](docs/ARCHITECTURE.md)
- [Agent 运行、确认与验证边界](docs/AGENT_RUNTIME.md)
- [主要 API 与完整路由清单](docs/API_REFERENCE.md)
- [开发、测试与维护](docs/DEVELOPMENT.md)
- [部署操作](deploy/README.md)
- [审计结果和删除依据](docs/CODEBASE_AUDIT_REPORT.md)、[保留待确认项](docs/UNUSED_CODE_REVIEW.md)

编号文档、学习笔记、产品评审和历史评测只作各自用途的参考，不代表当前架构或本次验证结果。维护规则见 [AGENTS.md](AGENTS.md)。
