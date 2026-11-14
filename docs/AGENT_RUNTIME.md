# Agent 当前运行契约

以下来自当前源码，不以历史迁移报告的“完成”状态为依据。

## 语义理解与执行

`POST /api/v1/agent/chat → coach_agent.run → semantic_runtime.run → business_snapshot / semantic_planner → validate_tasks → execute_plan → 领域服务 → 重读验证 → AgentRun + AgentMessage`。

coach_agent._run 仅调用 semantic_runtime，没有旧 intent_router fallback。semantic_planner 将业务快照、历史上下文和可用能力菜单提供给结构化模型调用；它输出 BoundPlan / BoundTask，不直接写库。模型不可用、对象或参数不明确时应返回失败/澄清，而不是让文本规则猜测执行。

binding_contract.py 分开表示对象 resolution 和 parameters_resolved。task_executor.validate_tasks 校验能力白名单、真实 ID、目标类型、归属、参数 Schema、依赖顺序；补齐缺失参数必须重新绑定，不能仅设置“已澄清”跳过验证。快照 revision 与当前业务状态不一致返回 stale。

## 能力与边界

注册入口为 capabilities.py:capability。原本地注册表 14 项；另有 commerce_capabilities、workout_capabilities、day_plan_capabilities。可派发定义的实际明细在 [能力清单](audit/capabilities.json)，按源码重新导出。模型收到的是按上下文和配置筛选后的菜单，不能将可派发总数当成每轮已开放数量。

| execution_boundary | 入口 | 约束 |
|---|---|---|
| local_database | task_executor.execute_tasks | 局部训练/食物、档案、记忆、查询、补偿；重读和 scope 比较 |
| commerce | commerce_runtime.execute_mixed | 外部查询、报价与订单独立生命周期；凭证、门店、库存、价格、幂等及未知结果核实 |
| workout_lifecycle | workout_runtime.execute_mixed | 真实训练会话、组数事实和完成摘要，不靠回复伪造完成 |
| day_revision | day_plan_runtime.execute_revision | 整日草案按阶段预览、编辑、确认应用；每轮只执行一个生命周期任务 |

新能力必须同时接入注册、执行边界、确认及验证策略；增加提示词或页面按钮不算完成迁移。

## 确认、连续修改和幂等

CoachPendingPlan 保存绑定任务、snapshot_id、问题及 version。结构化确认使用原 pending_id/version；自然语言控制器只能管理当前保存计划。新增目标、修改参数、选择候选和授权执行是不同操作；无关新请求不携带已放弃局部任务的上下文，编辑原任务或延续整日草案时才保留对应工作流；只授权保留任务子集不能重放其余旧任务。整日/商务草案编辑应引用原草案及尚未满足的要求，不能重置上下文。

档案、记忆、补偿和外部提交等按注册策略确认。确认前校验拥有者、当前对象和版本；热身移除、推断绑定或缺参数另有校验。草案预览不是已经应用，批准预览也不等于批准后续实际提交。

可选 request_id 在执行前保存声明；相同键同请求只重放已完成结果，冲突或执行中/待核实返回 409，不盲重试。商务 ExternalOperation 另外声明外部操作；未知结果先查询对账。补偿是有范围/版本条件的后续操作，不宣称可以回滚已产生的外部订单。

## 商务和训练的事实边界

commerce_availability.py 重新核实附近门店及距离；最近门店关闭时，确认替换为营业门店后继续选餐；范围内无营业门店返回业务解释。库存、报价、取餐方式和下单开关均由实际服务校验。

commerce_history.py 区分 active（有效未付）、paid、inactive、unknown；过期/取消不是有效待付。状态未知不能冒充已付款，历史时间本身不足以证明订单失效，需沿用订单生命周期规则。取餐默认/偏好、位置、选餐与确认界面不改变支付授权边界。

workout_records.py、workout_replan.py 和 day_revision.py 分离已记录事实与新目标。整日修改要核验具体动作、份量及显式目标约束；不能仅因工具返回 success 就声称所有时间、动作数量和饮食要求都满足。

## Console 观测与真实限制

Console 复用原 api.agentChat，没有第二个 Agent 或直接写业务库接口。RuntimeConversation 只保存空会话元数据；消息/运行从原表恢复。runtime_console.py 投影限制为本人数据，快照核对用户/线程，执行日志只按当前结果中准确 execution_id 关联。

AgentRun.id 是 Turn/Trace ID；事件 ID 为 run_id + 原 trace 数组序号。历史未采集的时间为 null，不用 run 时间补造。structured_call 记录脱敏业务 data 和解析输出，不包含全部系统提示、输出 Schema 或 HTTP 报文，也不保存自由推理。

runtime_observation.py 记录 execute_plan **批次边界**的真实开始/完成/失败及耗时；不是每个工具独立计时。观测故障隔离，不改变原执行。新旧 stage/node trace 均可读取。任务状态、实际结果、已关联 before/after 与回复分开展示，缺少日志只意味着没有该证据。

目前全局 goal_verified 为 null、transaction_rollback 为 not_recorded；商务外部操作未全部逐项关联。当前没有 SSE；主要 trace 在 run 完成后落库，中断前内存事件不能恢复。不得称为完整生产级事件体系或全局独立目标验证已完成。

投影遮盖敏感字段、常见 Bearer/sk-形式与手机号；这不是任意自由文本秘密识别器。后续仍需逐工具调用 ID/提交回滚证据、外部对账关联、独立整体目标验证、中断恢复、大数据分页、保留周期和 PostgreSQL/真机验证。测试证据适用范围见 [审计报告](CODEBASE_AUDIT_REPORT.md)。

## 历史路径

旧规则路由保留给 legacy 测试和 CLI；独立 LangGraph 补丁、today Agent、skill 计划生成、知识 API 继续保留。不得将其重新接入生产聊天，也不得仅凭“旧”删除。原始评测轨迹与 holdout 来源仍保留，历史通过率不代表当前模型准确率。
