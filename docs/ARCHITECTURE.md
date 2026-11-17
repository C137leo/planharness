# 当前系统架构

核对基线：2026-11-14 工作区源码；未提交的现有实现也属于本说明核对对象，不能把文档提交等同于所有代码已发布。

## 入口与模块职责

| 层 | 实际入口 / 模块 | 职责 |
|---|---|---|
| 前端 | frontend/src/main.ts、App.vue、pages.json | uni-app 初始化、页面注册与共享布局 |
| 业务 UI | pages/today、plan、progress、profile、workout、checkin | 展示服务端业务状态和提交用户操作 |
| 对话 UI | components/GlobalCoachWorkspace.vue、composables/coachWorkspace.ts | 共享对话、结构化确认、业务卡片 |
| 工作台 | pages/console/index.vue、components/RuntimeEvidence.vue | 原聊天入口的会话及证据视图 |
| 客户端 | frontend/src/api/client.ts | 认证请求、API 封装；开发代理见 vite.config.ts |
| 应用装配 | backend/app/main.py:create_app | DB、种子、加法迁移、checkpointer、路由、中间件与生命周期 |
| HTTP | backend/app/api/*_routes.py、routes.py | 登录、计划、训练、聊天、商务、知识、系统及运行查询 |
| 聊天 | agent/coach_agent.py、semantic_runtime.py | 请求幂等、快照、待确认计划、绑定与派发 |
| 领域层 | domain/ | 计划、营养、档案、记忆、训练事实、整日修订、外部订单 |
| 存储 | models.py、db.py、db_migrations.py | SQLAlchemy 实体、会话工厂、已有库加法补齐 |

所有后端路由模块在 main.py 中明确挂载；不能只搜索前端调用就认定接口无用。API 清单见 [API_REFERENCE.md](API_REFERENCE.md)。

## 数据流和状态来源

用户操作 → 认证 API → 读取本人实际业务状态 → 校验对象/版本/范围 → 对应领域执行 → 重读验证 → 返回结果与持久化记录 → 前端刷新。

长期计划由 Plan / PlanVersion 保存，当天临时安排通过 PlanAdjustment 及整日修订服务叠加得到 effective_day；训练记录属于 WorkoutSession / WorkoutLog / WorkoutRecordRevision 等事实实体。重新制定目标和已有完成事实是不同维度，不能拿旧记录充作新目标完成。档案和记忆通过 profile_sync、memory_lifecycle 保持规范来源与生命周期。

首份计划骨架继续来自PlanningAgent/skill，聊天通过独立的initial_requirements目标契约执行并核对数量、目录选择、时长与食材，部分修改保留其他选择。MealLog是实际进食；聊天登记/纠错与页面共用该实体，先确认再写，不能拿MealPlan冒充已吃记录。请求最终结果与消息提交同事务保存，前端按账号保留请求编号供跨刷新只读核实。

部位与饮食偏好由模型解释为带生效日期的 `planning_preferences` 契约，执行层依据动作的目标/负荷部位和真实食材身份核对效果。约束及基线随长期计划 program 保存；`effective_day` 和当前计划 API 共享日期投影，短期要求过期后恢复基线。模型语义解释与目录工程标注的职责及验证边界见 [AGENT_RUNTIME.md](AGENT_RUNTIME.md)。

商务报价、确认和外部调用有单独 CheckoutDraft / ExternalOperation 等状态；不能把外部提交放进本地事务后声称可一起回滚。运行观测复用 AgentRun、AgentMessage、CoachBusinessSnapshot、CoachTaskExecution；RuntimeConversation 只存空会话元数据，不另存一套消息。

## 同时存在的执行路径

生产聊天只走语义绑定运行器，详见 [AGENT_RUNTIME.md](AGENT_RUNTIME.md)。其他路径仍有独立用途：

- routes.py / agent_routes.py 的计划生成、补丁审批和 today Agent；main.py 仍组装 patch_graph 和 checkpointer。
- domain/planner.py、planning_agent.py 等生成与适配逻辑通过 skill/bridge.py 调用 Node 确定性引擎；不可用时由原服务决定保守降级。这不是聊天模型理解失败后的规则意图替代。
- knowledge_routes.py 提供自建知识问答及外部语料检索；retriever.py 的 BM25 与 rag.py 的语料对比并存。存在独立工具不意味着它已注册进聊天能力菜单。
- legacy_coach_agent.py / intent_router.py / legacy_task_executor.py 等保留给 legacy 测试、兼容链与 eval_intent.py；本次未移除或迁移。

## 启动与部署边界

create_app 会建不存在的表、执行加法 schema 补齐、种子与记忆回填。部分迁移异常记录后继续启动，因此“进程启动成功”不足以证明 schema 正确。生产发布应先备份并核验迁移日志及健康检查。

开发 checkpointer 可用内存；生产 PostgreSQL checkpointer 不可用时不静默降级。业务数据库与 Graph checkpoint 配置需分别核对。后台记忆维护由生命周期任务运行；测试环境应禁用或隔离。

systemd / Nginx / PostgreSQL 模板见 [部署说明](../deploy/README.md)，Docker 为另一部署入口，不代表已在线上采用。当前没有通过本次审计验证生产、多进程或微信真机运行。
