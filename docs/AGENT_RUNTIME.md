# Agent 当前运行契约

以下来自当前源码，不以历史迁移报告的“完成”状态为依据。

## 语义理解与执行

`POST /api/v1/agent/chat → coach_agent.run → semantic_runtime.run → business_snapshot / semantic_planner → validate_tasks → execute_plan → 领域服务 → 重读验证 → AgentRun + AgentMessage`。

coach_agent._run 仅调用 semantic_runtime，没有旧 intent_router fallback。semantic_planner 将业务快照、历史上下文和可用能力菜单提供给结构化模型调用；它输出 BoundPlan / BoundTask，不直接写库。模型不可用、对象或参数不明确时应返回失败/澄清，而不是让文本规则猜测执行。

binding_contract.py 分开表示对象 resolution 和 parameters_resolved。task_executor.validate_tasks 校验能力白名单、真实 ID、目标类型、归属、参数 Schema、依赖顺序；补齐缺失参数必须重新绑定，不能仅设置“已澄清”跳过验证。快照 revision 与当前业务状态不一致返回 stale。

## 能力与边界

注册入口为 capabilities.py:capability。本地注册表17项；另有 commerce_capabilities、workout_capabilities、day_plan_capabilities，以及 initial_plan_runtime 的8个首份/长期替换计划生命周期能力，共49项可派发定义。实际明细在 [能力清单](audit/capabilities.json)，按源码导出；本轮可调用菜单仍按真实上下文筛选。

| execution_boundary | 入口 | 约束 |
|---|---|---|
| local_database | task_executor.execute_tasks | 局部训练/计划食物、实际进食登记/纠错、档案、记忆、查询、补偿；重读和 scope 比较 |
| commerce | commerce_runtime.execute_mixed | 外部查询、报价与订单独立生命周期；凭证、门店、库存、价格、幂等及未知结果核实 |
| workout_lifecycle | workout_runtime.execute_mixed | 真实训练会话、组数事实和完成摘要，不靠回复伪造完成 |
| initial_plan | initial_plan_runtime.execute | 首份及已有长期计划替换均复用 coach.create_draft/confirm_draft；预览不改档案；档案指纹及草案etag保护；生成、修改、确认、取消分阶段 |
| day_revision | day_plan_runtime.execute_revision | 整日草案按阶段预览、编辑、确认应用；每轮只执行一个生命周期任务 |

新能力必须同时接入注册、执行边界、确认及验证策略；增加提示词或页面按钮不算完成迁移。

首份计划在目标契约已完成语义提取后，直接使用 PlanningAgent 的确定性 skill 流程生成骨架，不再让第二个模型重复解释同一原话。首份计划复用 PlanningAgent/skill 生成骨架，聊天另经 initial_requirements 独立提取结构化目标，不从提议工具参数倒推原意。契约保存正式动作数、目录选择、时长语义、天数、目标、食材/过敏限制；typed delta合并保留未提及条件，部分修改保留未授权更改的动作/餐次，实际分钟数分配含热身并重读验证。生产聊天首份草案跳过旧文本显式编辑与确定性文本重试；结构化目录选择仍经过器械/伤病守卫。无法表达的明确要求列为未完成，不能静默忽略。

无契约的独立草案 API 保留旧兼容提取，仅作用于脱离数据库的输入；已带契约草案的反馈复用新契约链，模型不可用时拒绝绕过。两者生成/反馈都不提前保存 Profile。首份反馈保留档案指纹，失败生成保留上一份ready预览；失败修改的未满足条件存于原待办，不能确认旧预览冒充完成新要求。

## 确认、连续修改和幂等

CoachPendingPlan 保存绑定任务、snapshot_id、问题及 version。结构化确认使用原 pending_id/version；自然语言控制器只能管理当前保存计划。新增目标、修改参数、选择候选和授权执行是不同操作；无关新请求不携带已放弃局部任务的上下文，编辑原任务或延续整日草案时才保留对应工作流；只授权保留任务子集不能重放其余旧任务。整日/商务草案编辑应引用原草案及尚未满足的要求，不能重置上下文。

档案、记忆、补偿和外部提交等按注册策略确认。确认前校验拥有者、当前对象和版本；热身移除、推断绑定或缺参数另有校验。草案预览不是已经应用，批准预览也不等于批准后续实际提交。自然语言confirm还需独立复核具体操作授权；控制器与复核结论取交集，暂缓/只预览不因限定范围变成执行。结构化按钮确认仍校验原任务和版本。新请求的局部任务ID由运行时重新分配；未授权旧写任务不会携入只读请求。

可选 request_id 在执行前保存声明；相同键同请求只重放已完成结果，冲突或执行中/待核实返回 409，不盲重试。GET /agent/requests/{request_id}按当前用户只读查询声明与最终结果；前端聊天请求超时后仅查询同一声明，不重发POST。模型调用共享120秒预算，前端结果恢复查询有180秒窗口；超出窗口仍标为待核实，不能称失败或自动重试。商务 ExternalOperation 另外声明外部操作；未知结果先查询对账。补偿是有范围/版本条件的后续操作，不宣称可以回滚已产生的外部订单。

finish将精确最终结果、消息与本地提交同事务保存到请求声明，响应中断不能把已完成声明覆盖为未知。超过5分钟仍running的声明在查询时只读投影为needs_reconciliation，不自动重发，也不推断后台已停止。前端按账号持久化请求编号/线程/时间，刷新后打开会话可GET核对；不缓存正文或凭证，不是可重放命令队列。

整日草案修订失败时，待确认版本递增并保存 unresolved_edits；模型控制阶段失败、尚未取得结构化要求时也保存 unresolved_goal_input。确认入口和按钮均阻止应用旧稿，直到成功生成满足新要求的预览或取消。明确正式动作数独立保存为 formal_actions，并对草案目标条目与持久化约束作确定性比较。已有整日预览/重排授权上下文不得自动降级为对实际训练或计划餐食的局部写入。

具体本地修改若以 mode=propose 返回带参数任务，会保存为待确认预览，而非仅保留口头说明；明确确认后才写实际记录。

## 业务事实和同步

普通日期快照包含Checkin、独立WorkoutLog和MealLog；无WorkoutSession不代表没有手工运动。Profile快照保留goal_note，区分目标原话与结构化目标。档案更新和UI表单共用profile_sync，同步canonical镜像后做数据库重读验证。备注明确kind与retention_days；短期有效期按用户时区自然日计算，过期条目不向模型开放；旧数据不依据文本自动推测期限或删除。history.undo只面向支持的资源，Profile恢复通过同一同步服务，训练事实仍不能撤销为未发生。

档案与镜像同步在同一事务内，同步失败不能返回保存成功。训练时段及展示标签成对更新/清空，也成对补偿恢复。聊天 memory.archive 只处理独立备注；canonical 镜像必须先通过 profile.update 预览修改对应档案字段，执行器拒绝单独归档。独立记忆管理 API 保留原有同步清空档案的兼容行为。

前端以发送时的token判断迟到结果/401是否仍属于当前账号；旧账号的响应不展示，旧错误不退出新账号。同账号切换对话也不把旧线程结果追加到新线程。恢复仅核对已有声明，不自动续跑中断执行器或外部未知副作用。

nutrition.record_meal绑定day、nutrition.correct_meal绑定本人meal_log，均先确认后写实际进食。事实与计划餐食分离；营养值必须提供或明确核对估算，不能将未知当0。局部事务重读整个业务状态，越界副作用回滚，差异日志记录meal_logs，不能用普通history.undo抹掉实际记录。

reply_evidence独立核查规划器自由回答/问题中的无依据外食过敏安全承诺，拒绝时用固定证据边界说明；复核不可用时暂缓发布候选结论。它与原规划器仍使用模型服务，不等于独立模型准确率已验收，也不是通用医疗文本审核或全部最终字段的整体目标验证。

## 商务和训练的事实边界

commerce_availability.py 重新核实附近门店及距离；最近门店关闭时，确认替换为营业门店后继续选餐；范围内无营业门店返回业务解释。库存、报价、取餐方式和下单开关均由实际服务校验。

显式选定真实营业门店不再重复询问选店；推断替换门店仍需确认。纯报价不创建订单。reply_evidence 对未完成目标先做语义类型分类，只凭未过期报价、实际购物车、总价及取餐方式等证据移除已完成的报价目标，不能据此宣称下单、支付或过敏核实完成；审阅失败保留未完成项。

commerce_history.py 区分 active（有效未付）、paid、inactive、unknown；过期/取消不是有效待付。状态未知不能冒充已付款，历史时间本身不足以证明订单失效，需沿用订单生命周期规则。取餐默认/偏好、位置、选餐与确认界面不改变支付授权边界。纯订单状态查询不重新附带购买确认；报价同时提供用户时区expires_at_label。营养数据和偏好过滤不构成外食过敏安全保证。

workout_records.py、workout_replan.py 和 day_revision.py 分离已记录事实与新目标。整日修改要核验具体动作、份量及显式目标约束；不能仅因工具返回 success 就声称所有时间、动作数量和饮食要求都满足。

训练记录回复列出真实逐组次数、重量和整场实际时长，缺失值不以计划补齐。前端当前会话优先使用重读结果，必要时只读刷新；历史附件仍是历史快照。进展接口传递 actual_duration_known，未知动作时长显示未记录，消耗汇总说明仅包括已知时长记录。

## Console 观测与真实限制

Console 复用原 api.agentChat，没有第二个 Agent 或直接写业务库接口。RuntimeConversation 只保存空会话元数据；消息/运行从原表恢复。runtime_console.py 投影限制为本人数据，快照核对用户/线程，执行日志只按当前结果中准确 execution_id 关联。

AgentRun.id 是 Turn/Trace ID；事件 ID 为 run_id + 原 trace 数组序号。历史未采集的时间为 null，不用 run 时间补造。structured_call 记录脱敏业务 data 和解析输出，不包含全部系统提示、输出 Schema 或 HTTP 报文，也不保存自由推理。

runtime_observation.py 记录 execute_plan **批次边界**的真实开始/完成/失败及耗时；不是每个工具独立计时。观测故障隔离，不改变原执行。新旧 stage/node trace 均可读取。任务状态、实际结果、已关联 before/after 与回复分开展示，缺少日志只意味着没有该证据。

目前全局 goal_verified 为 null、transaction_rollback 为 not_recorded；商务外部操作未全部逐项关联。当前没有 SSE；主要 trace 在 run 完成后落库，中断前内存事件不能恢复。不得称为完整生产级事件体系或全局独立目标验证已完成。

投影遮盖敏感字段、常见 Bearer/sk-形式与手机号；这不是任意自由文本秘密识别器。后续仍需逐工具调用 ID/提交回滚证据、外部对账关联、独立整体目标验证、中断恢复、大数据分页、保留周期和 PostgreSQL/真机验证。测试证据适用范围见 [审计报告](CODEBASE_AUDIT_REPORT.md)。

## 历史路径

旧规则路由保留给 legacy 测试和 CLI；独立 LangGraph 补丁、today Agent、skill 计划生成、知识 API 继续保留。不得将其重新接入生产聊天，也不得仅凭“旧”删除。原始评测轨迹与 holdout 来源仍保留，历史通过率不代表当前模型准确率。

## 长期食谱、完整重建与停止请求

用户明确新增过敏原用 profile.add_allergies，只做长期并集，不自动删除原有项；普通 profile.update 和档案表单的过敏变更同样触发 meal_safety。冲突食材从真实目录替换，保持已有餐次，长期模板以新版本保存，后续未完成餐次的覆盖同步修复，已发生进食/训练事实不改写。海鲜限制包含鱼和甲壳类；所有计划食物写入口使用结构化标签检查，不能把原始中文直接与英文标签相交。未知或目录外成分不能据此承诺医学安全。普通历史撤销不能删除长期过敏限制。

已有长期计划可以 create/revise/confirm/cancel_replacement_draft；预览不写实际安排，确认前核对原计划base_version、档案指纹和草案etag。创建新版本不删除实际日志。首次/替换预览默认三餐，用户明确选择动作时不为增加差异而替换；默认训练日按周分散，器械过滤后重复的动作组合在安全范围内改用同类动作。

POST /agent/requests/{request_id}/cancel 仅停止尚未进入执行阶段的本用户请求。模型网络请求使用独立可取消连接；取消后不执行后续工具。执行阶段用同一请求声明行锁设标记，已进入执行阶段返回too_late，不能声称停止等于回滚已提交修改或远程订单。已完成的取消结果仍按原request_id保存和重放。前端停止按钮、IME composition生命周期和Enter发送分开处理。

MCP只接收当前用户请求头凭证，空值不使用服务端默认Token或演示口令；前端Token和订单缓存按账号隔离，不迁移无法确认归属的旧全局凭证。
