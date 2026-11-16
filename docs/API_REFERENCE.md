# API 参考与路由索引

路由来自 backend/app/api/*.py 的装饰器和请求模型，挂载关系在 main.py。完整方法、路径、处理器、行号、签名与本地请求模型字段见 [机器可读清单](audit/api-routes.json)。这是源码扫描索引，不替代部署实例 `/openapi.json` 的完整 JSON Schema；Pydantic 组合模型的约束应以源码和实例 OpenAPI 为准。

## 公共约定

业务路由前缀 `/api/v1`。成功包装由 routes.py:ok 生成：`{code: 0, message, data, trace_id}`。HTTP 错误遵循 FastAPI 错误响应，不能只检查成功包装的 code。认证由既有 get_current_user 依赖验证 Bearer 令牌；登录/注册和健康检查等公共接口以实际依赖为准。

### 聊天结果恢复

`GET /api/v1/agent/requests/{request_id}`只查询当前登录用户的请求声明，不执行或重发任务。返回`{request_id,status,thread,result}`：running时result=null，succeeded时为精确原AgentChatResult；超过5分钟仍无最终结果时只读投影为needs_reconciliation，不能盲重试。未知/其他用户请求返回404。前端超时/刷新后按账号范围内保存的原request_id查询恢复，不保存请求正文或凭证。

`POST /api/v1/agent/requests/{request_id}/cancel`只取消当前用户尚未执行的请求：返回cancel_requested、completed、too_late或needs_reconciliation。取消不代表撤销已产生的副作用；最终取消结果仍由原请求保存。

## 统一聊天

`POST /api/v1/agent/chat`，ChatIn 字段：

| 字段 | 类型/含义 |
|---|---|
| text | 必填，1–1000 字符 |
| request_id | 可选，1–128 字符；同键不同请求或未完成声明返回 409 |
| page | 默认 today，最长24字符，只作上下文 |
| date | 可选 YYYY-MM-DD；缺省按用户时区 |
| session_id / thread | 可选训练会话 / 对话标识；thread 最长32字符 |
| context | 页面只读业务上下文，不能凭此直接写库 |
| confirmation | PendingControl，必须携带当前 pending_id/version |
| checkout_action / commerce_action | binding_contract.py 定义的结构化商务操作 |

```json
{"text":"查看今天安排","page":"today","thread":"coach","request_id":"client-unique-id"}
```

返回 data 包含 reply、run_id、overall_status、task_plan/task_results，按实际任务可附 pending_confirmation、day_revision、checkout、order 等。字段可缺省；只有服务端结果或已验证记录能证明完成，不能从 reply 或HTTP200推断成功。原结构化确认至少包含 action、pending_id、version；候选/保留任务使用 selections、keep_task_ids 等服务端保存范围，完整约束见 binding_contract.py。

## 工作台与订单历史

- GET /agent/runtime/conversations：本人会话及空会话，当前最多50项。
- POST /agent/runtime/conversations：创建本人空会话元数据。
- GET /agent/runtime/conversations/{thread}?before=&limit=：本人消息及 Turn 引用，默认50、最多100；游标锚点必须属于同用户/同会话。
- GET /agent/runtime/turns/{turn_id}：本人事件、任务、快照、执行日志与诊断；缺失证据为 null / not_recorded。
- GET /agent/commerce/orders?group=active|paid|inactive|unknown&cursor=&limit=：订单分组，默认20、最多50。
- POST /agent/commerce/orders/sync：刷新近期订单；GET /agent/commerce/orders/{record_id} 与 POST .../{record_id}/refresh 读取/核实本人订单。
- GET/PUT /agent/commerce/preferences：位置等偏好。商务 API 使用认证及既有 MCP 凭证/账户隔离，不能把客户端展示的 ID 当授权。

以上简写路径均带 `/api/v1` 前缀。读取和外部刷新具有不同副作用，不能把 sync/refresh 当作纯本地查询。

## 全部已注册路由

旧计划、补丁、评测、系统与麦当劳接口仍挂载，是否由当前页面使用不影响其兼容责任。本清单不承诺所有路由具备相同权限；审阅时检查每个处理器依赖。

| 方法 | 路径 | 源码 |
|---|---|---|
| GET | `/api/v1/mcd/status` | [mcd_routes.py:90](../backend/app/api/mcd_routes.py#L90) |
| GET | `/api/v1/mcd/stores` | [mcd_routes.py:103](../backend/app/api/mcd_routes.py#L103) |
| GET | `/api/v1/mcd/menu` | [mcd_routes.py:118](../backend/app/api/mcd_routes.py#L118) |
| POST | `/api/v1/mcd/quote` | [mcd_routes.py:138](../backend/app/api/mcd_routes.py#L138) |
| POST | `/api/v1/mcd/orders` | [mcd_routes.py:154](../backend/app/api/mcd_routes.py#L154) |
| GET | `/api/v1/mcd/orders/{order_id}` | [mcd_routes.py:185](../backend/app/api/mcd_routes.py#L185) |
| POST | `/api/v1/mcd/orders/{order_id}/cancel` | [mcd_routes.py:201](../backend/app/api/mcd_routes.py#L201) |
| GET | `/api/v1/system/status` | [system_routes.py:36](../backend/app/api/system_routes.py#L36) |
| GET | `/api/v1/admin/llm-switch` | [system_routes.py:46](../backend/app/api/system_routes.py#L46) |
| PUT | `/api/v1/admin/llm-switch` | [system_routes.py:59](../backend/app/api/system_routes.py#L59) |
| GET | `/api/v1/auth/challenge` | [auth_routes.py:78](../backend/app/api/auth_routes.py#L78) |
| POST | `/api/v1/auth/register` | [routes.py:1488](../backend/app/api/routes.py#L1488) |
| POST | `/api/v1/auth/login` | [routes.py:1502](../backend/app/api/routes.py#L1502) |
| POST | `/api/v1/_debug/client-error` | [auth_routes.py:223](../backend/app/api/auth_routes.py#L223) |
| GET | `/api/v1/auth/me` | [auth_routes.py:248](../backend/app/api/auth_routes.py#L248) |
| POST | `/api/v1/agent/today-plan` | [agent_routes.py:135](../backend/app/api/agent_routes.py#L135) |
| GET | `/api/v1/agent/today-plan` | [agent_routes.py:154](../backend/app/api/agent_routes.py#L154) |
| POST | `/api/v1/agent/today-plan/{run_id}/apply` | [agent_routes.py:172](../backend/app/api/agent_routes.py#L172) |
| POST | `/api/v1/agent/today-plan/{run_id}/feedback` | [agent_routes.py:200](../backend/app/api/agent_routes.py#L200) |
| GET | `/api/v1/food/options` | [food_routes.py:30](../backend/app/api/food_routes.py#L30) |
| GET | `/api/v1/agent/commerce/status` | [coach_routes.py:202](../backend/app/api/coach_routes.py#L202) |
| GET | `/api/v1/agent/commerce/orders` | [coach_routes.py:216](../backend/app/api/coach_routes.py#L216) |
| POST | `/api/v1/agent/commerce/orders/sync` | [coach_routes.py:225](../backend/app/api/coach_routes.py#L225) |
| GET | `/api/v1/agent/commerce/orders/{record_id}` | [coach_routes.py:232](../backend/app/api/coach_routes.py#L232) |
| POST | `/api/v1/agent/commerce/orders/{record_id}/refresh` | [coach_routes.py:242](../backend/app/api/coach_routes.py#L242) |
| GET | `/api/v1/agent/commerce/preferences` | [coach_routes.py:259](../backend/app/api/coach_routes.py#L259) |
| PUT | `/api/v1/agent/commerce/preferences` | [coach_routes.py:265](../backend/app/api/coach_routes.py#L265) |
| POST | `/api/v1/agent/chat` | [coach_routes.py:274](../backend/app/api/coach_routes.py#L274) |
| POST | `/api/v1/agent/requests/{request_id}/cancel` | [coach_routes.py:301](../backend/app/api/coach_routes.py#L301) |
| GET | `/api/v1/agent/requests/{request_id}` | [coach_routes.py:318](../backend/app/api/coach_routes.py#L318) |
| GET | `/api/v1/agent/messages` | [coach_routes.py:338](../backend/app/api/coach_routes.py#L338) |
| GET | `/api/v1/agent/conversations` | [coach_routes.py:349](../backend/app/api/coach_routes.py#L349) |
| DELETE | `/api/v1/agent/conversations/{thread}` | [coach_routes.py:359](../backend/app/api/coach_routes.py#L359) |
| GET | `/api/v1/agent/day` | [coach_routes.py:375](../backend/app/api/coach_routes.py#L375) |
| GET | `/api/v1/agent/workout-replans/{authorization_id}` | [coach_routes.py:387](../backend/app/api/coach_routes.py#L387) |
| GET | `/api/v1/agent/day-revisions/{revision_id}` | [coach_routes.py:397](../backend/app/api/coach_routes.py#L397) |
| POST | `/api/v1/agent/day/reset` | [coach_routes.py:410](../backend/app/api/coach_routes.py#L410) |
| GET | `/api/v1/agent/adjustments` | [coach_routes.py:423](../backend/app/api/coach_routes.py#L423) |
| POST | `/api/v1/agent/adjustments/{adjustment_id}/restore` | [coach_routes.py:447](../backend/app/api/coach_routes.py#L447) |
| GET | `/api/v1/agent/progress-insights` | [coach_routes.py:465](../backend/app/api/coach_routes.py#L465) |
| GET | `/api/v1/agent/memory/status` | [coach_routes.py:476](../backend/app/api/coach_routes.py#L476) |
| GET | `/api/v1/agent/memories` | [coach_routes.py:502](../backend/app/api/coach_routes.py#L502) |
| POST | `/api/v1/agent/memories` | [coach_routes.py:528](../backend/app/api/coach_routes.py#L528) |
| PUT | `/api/v1/agent/memories/{memory_id}` | [coach_routes.py:556](../backend/app/api/coach_routes.py#L556) |
| DELETE | `/api/v1/agent/memories/{memory_id}` | [coach_routes.py:576](../backend/app/api/coach_routes.py#L576) |
| POST | `/api/v1/workout-sessions` | [coach_routes.py:594](../backend/app/api/coach_routes.py#L594) |
| GET | `/api/v1/workout-sessions/active` | [coach_routes.py:612](../backend/app/api/coach_routes.py#L612) |
| GET | `/api/v1/workout-sessions/{session_id}` | [coach_routes.py:629](../backend/app/api/coach_routes.py#L629) |
| POST | `/api/v1/workout-sessions/{session_id}/sets` | [coach_routes.py:642](../backend/app/api/coach_routes.py#L642) |
| POST | `/api/v1/workout-sessions/{session_id}/skip` | [coach_routes.py:662](../backend/app/api/coach_routes.py#L662) |
| POST | `/api/v1/workout-sessions/{session_id}/replace` | [coach_routes.py:677](../backend/app/api/coach_routes.py#L677) |
| POST | `/api/v1/workout-sessions/{session_id}/pause` | [coach_routes.py:700](../backend/app/api/coach_routes.py#L700) |
| POST | `/api/v1/workout-sessions/{session_id}/finish` | [coach_routes.py:714](../backend/app/api/coach_routes.py#L714) |
| POST | `/api/v1/workout-sessions/{session_id}/feedback` | [coach_routes.py:743](../backend/app/api/coach_routes.py#L743) |
| GET | `/api/v1/agent/runtime/conversations` | [coach_routes.py:755](../backend/app/api/coach_routes.py#L755) |
| GET | `/api/v1/agent/runtime/conversations/{thread}` | [coach_routes.py:761](../backend/app/api/coach_routes.py#L761) |
| GET | `/api/v1/agent/runtime/turns/{turn_id}` | [coach_routes.py:770](../backend/app/api/coach_routes.py#L770) |
| POST | `/api/v1/agent/runtime/conversations` | [coach_routes.py:777](../backend/app/api/coach_routes.py#L777) |
| GET | `/api/v1/knowledge/search` | [knowledge_routes.py:23](../backend/app/api/knowledge_routes.py#L23) |
| POST | `/api/v1/knowledge/ask` | [knowledge_routes.py:48](../backend/app/api/knowledge_routes.py#L48) |
| GET | `/api/v1/knowledge/rag` | [knowledge_routes.py:57](../backend/app/api/knowledge_routes.py#L57) |
| GET | `/api/v1/health` | [routes.py:99](../backend/app/api/routes.py#L99) |
| GET | `/api/v1/auth/options` | 公开登录选项，返回 `demo_login_enabled`，读取失败时前端隐藏 demo |
| POST | `/api/v1/auth/demo` | 默认关闭（404）；`DEMO_LOGIN_ENABLED=true` 时仅接受 `{}`，只登录固定演示账号，指定 `user_id` 返回 422 |
| POST | `/api/v1/auth/wechat` | [routes.py:158](../backend/app/api/routes.py#L158) |
| GET | `/api/v1/me/profile` | [routes.py:170](../backend/app/api/routes.py#L170) |
| PUT | `/api/v1/me/profile` | [routes.py:254](../backend/app/api/routes.py#L254) |
| POST | `/api/v1/plans/drafts` | [routes.py:307](../backend/app/api/routes.py#L307) |
| GET | `/api/v1/plans/drafts/{draft_id}` | [routes.py:325](../backend/app/api/routes.py#L325) |
| POST | `/api/v1/plans/drafts/{draft_id}/feedback` | [routes.py:339](../backend/app/api/routes.py#L339) |
| POST | `/api/v1/plans/drafts/{draft_id}/confirm` | [routes.py:358](../backend/app/api/routes.py#L358) |
| POST | `/api/v1/plans/drafts/{draft_id}/reject` | [routes.py:383](../backend/app/api/routes.py#L383) |
| GET | `/api/v1/patch-presets` | [routes.py:401](../backend/app/api/routes.py#L401) |
| GET | `/api/v1/plans/current` | [routes.py:429](../backend/app/api/routes.py#L429) |
| GET | `/api/v1/plans/{plan_id}/versions` | [routes.py:452](../backend/app/api/routes.py#L452) |
| POST | `/api/v1/plans/generate` | [routes.py:525](../backend/app/api/routes.py#L525) |
| POST | `/api/v1/plans/{plan_id}/patches` | [routes.py:561](../backend/app/api/routes.py#L561) |
| GET | `/api/v1/audit/{stream_id}` | [routes.py:628](../backend/app/api/routes.py#L628) |
| GET | `/api/v1/audit/{stream_id}/verify` | [routes.py:668](../backend/app/api/routes.py#L668) |
| POST | `/api/v1/compare/knee-patch` | [routes.py:693](../backend/app/api/routes.py#L693) |
| GET | `/api/v1/evals/scenarios` | [routes.py:709](../backend/app/api/routes.py#L709) |
| POST | `/api/v1/evals/scenarios/{scenario_id}/run` | [routes.py:726](../backend/app/api/routes.py#L726) |
| POST | `/api/v1/plans/{plan_id}/patch-requests` | [routes.py:773](../backend/app/api/routes.py#L773) |
| GET | `/api/v1/patch-requests/{request_id}` | [routes.py:801](../backend/app/api/routes.py#L801) |
| POST | `/api/v1/patch-requests/{request_id}/feedback` | [routes.py:815](../backend/app/api/routes.py#L815) |
| POST | `/api/v1/patch-requests/{request_id}/approve` | [routes.py:834](../backend/app/api/routes.py#L834) |
| POST | `/api/v1/patch-requests/{request_id}/reject` | [routes.py:918](../backend/app/api/routes.py#L918) |
| POST | `/api/v1/plans/{plan_id}/undo` | [routes.py:945](../backend/app/api/routes.py#L945) |
| GET | `/api/v1/patches` | [routes.py:973](../backend/app/api/routes.py#L973) |
| GET | `/api/v1/patches/{patch_id}` | [routes.py:1011](../backend/app/api/routes.py#L1011) |
| POST | `/api/v1/patches/{patch_id}/approve` | [routes.py:1022](../backend/app/api/routes.py#L1022) |
| POST | `/api/v1/patches/{patch_id}/reject` | [routes.py:1093](../backend/app/api/routes.py#L1093) |
| GET | `/api/v1/runs` | [routes.py:1167](../backend/app/api/routes.py#L1167) |
| GET | `/api/v1/runs/{run_id}` | [routes.py:1189](../backend/app/api/routes.py#L1189) |
| POST | `/api/v1/plans/{plan_id}/retarget` | [routes.py:1251](../backend/app/api/routes.py#L1251) |
| POST | `/api/v1/plans/{plan_id}/rollback` | [routes.py:1271](../backend/app/api/routes.py#L1271) |
| POST | `/api/v1/workout-logs` | [routes.py:1310](../backend/app/api/routes.py#L1310) |
| GET | `/api/v1/workout-logs` | [routes.py:1339](../backend/app/api/routes.py#L1339) |
| DELETE | `/api/v1/workout-logs/{log_id}` | [routes.py:1358](../backend/app/api/routes.py#L1358) |
| GET | `/api/v1/stats/calories` | [routes.py:1373](../backend/app/api/routes.py#L1373) |
| POST | `/api/v1/checkins` | [routes.py:1397](../backend/app/api/routes.py#L1397) |
| GET | `/api/v1/checkins` | [routes.py:1422](../backend/app/api/routes.py#L1422) |
| POST | `/api/v1/meal-logs` | [routes.py:1436](../backend/app/api/routes.py#L1436) |
| GET | `/api/v1/meal-logs` | [routes.py:1457](../backend/app/api/routes.py#L1457) |
| DELETE | `/api/v1/meal-logs/{log_id}` | [routes.py:1473](../backend/app/api/routes.py#L1473) |
| POST | `/api/v1/auth/register` | [routes.py:1488](../backend/app/api/routes.py#L1488) |
| POST | `/api/v1/auth/login` | [routes.py:1502](../backend/app/api/routes.py#L1502) |
