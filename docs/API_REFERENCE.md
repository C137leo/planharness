# API 参考与路由索引

路由来自 backend/app/api/*.py 的装饰器和请求模型，挂载关系在 main.py。完整方法、路径、处理器、行号、签名与本地请求模型字段见 [机器可读清单](audit/api-routes.json)。这是源码扫描索引，不替代部署实例 `/openapi.json` 的完整 JSON Schema；Pydantic 组合模型的约束应以源码和实例 OpenAPI 为准。

## 公共约定

业务路由前缀 `/api/v1`。成功包装由 routes.py:ok 生成：`{code: 0, message, data, trace_id}`。HTTP 错误遵循 FastAPI 错误响应，不能只检查成功包装的 code。认证由既有 get_current_user 依赖验证 Bearer 令牌；登录/注册和健康检查等公共接口以实际依赖为准。

### 聊天结果恢复

`GET /api/v1/agent/requests/{request_id}`只查询当前登录用户的请求声明，不执行或重发任务。返回`{request_id,status,thread,result}`：running时result=null，succeeded时为精确原AgentChatResult；超过5分钟仍无最终结果时只读投影为needs_reconciliation，不能盲重试。未知/其他用户请求返回404。前端超时/刷新后按账号范围内保存的原request_id查询恢复，不保存请求正文或凭证。

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
| POST | `/api/v1/auth/register` | [routes.py:1479](../backend/app/api/routes.py#L1479) |
| POST | `/api/v1/auth/login` | [routes.py:1493](../backend/app/api/routes.py#L1493) |
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
| GET | `/api/v1/agent/requests/{request_id}` | [coach_routes.py:301](../backend/app/api/coach_routes.py#L301) |
| GET | `/api/v1/agent/messages` | [coach_routes.py:321](../backend/app/api/coach_routes.py#L321) |
| GET | `/api/v1/agent/conversations` | [coach_routes.py:332](../backend/app/api/coach_routes.py#L332) |
| DELETE | `/api/v1/agent/conversations/{thread}` | [coach_routes.py:342](../backend/app/api/coach_routes.py#L342) |
| GET | `/api/v1/agent/day` | [coach_routes.py:358](../backend/app/api/coach_routes.py#L358) |
| GET | `/api/v1/agent/workout-replans/{authorization_id}` | [coach_routes.py:370](../backend/app/api/coach_routes.py#L370) |
| GET | `/api/v1/agent/day-revisions/{revision_id}` | [coach_routes.py:380](../backend/app/api/coach_routes.py#L380) |
| POST | `/api/v1/agent/day/reset` | [coach_routes.py:393](../backend/app/api/coach_routes.py#L393) |
| GET | `/api/v1/agent/adjustments` | [coach_routes.py:406](../backend/app/api/coach_routes.py#L406) |
| POST | `/api/v1/agent/adjustments/{adjustment_id}/restore` | [coach_routes.py:430](../backend/app/api/coach_routes.py#L430) |
| GET | `/api/v1/agent/progress-insights` | [coach_routes.py:448](../backend/app/api/coach_routes.py#L448) |
| GET | `/api/v1/agent/memory/status` | [coach_routes.py:459](../backend/app/api/coach_routes.py#L459) |
| GET | `/api/v1/agent/memories` | [coach_routes.py:485](../backend/app/api/coach_routes.py#L485) |
| POST | `/api/v1/agent/memories` | [coach_routes.py:511](../backend/app/api/coach_routes.py#L511) |
| PUT | `/api/v1/agent/memories/{memory_id}` | [coach_routes.py:539](../backend/app/api/coach_routes.py#L539) |
| DELETE | `/api/v1/agent/memories/{memory_id}` | [coach_routes.py:559](../backend/app/api/coach_routes.py#L559) |
| POST | `/api/v1/workout-sessions` | [coach_routes.py:577](../backend/app/api/coach_routes.py#L577) |
| GET | `/api/v1/workout-sessions/active` | [coach_routes.py:595](../backend/app/api/coach_routes.py#L595) |
| GET | `/api/v1/workout-sessions/{session_id}` | [coach_routes.py:612](../backend/app/api/coach_routes.py#L612) |
| POST | `/api/v1/workout-sessions/{session_id}/sets` | [coach_routes.py:625](../backend/app/api/coach_routes.py#L625) |
| POST | `/api/v1/workout-sessions/{session_id}/skip` | [coach_routes.py:645](../backend/app/api/coach_routes.py#L645) |
| POST | `/api/v1/workout-sessions/{session_id}/replace` | [coach_routes.py:660](../backend/app/api/coach_routes.py#L660) |
| POST | `/api/v1/workout-sessions/{session_id}/pause` | [coach_routes.py:683](../backend/app/api/coach_routes.py#L683) |
| POST | `/api/v1/workout-sessions/{session_id}/finish` | [coach_routes.py:697](../backend/app/api/coach_routes.py#L697) |
| POST | `/api/v1/workout-sessions/{session_id}/feedback` | [coach_routes.py:726](../backend/app/api/coach_routes.py#L726) |
| GET | `/api/v1/agent/runtime/conversations` | [coach_routes.py:738](../backend/app/api/coach_routes.py#L738) |
| GET | `/api/v1/agent/runtime/conversations/{thread}` | [coach_routes.py:744](../backend/app/api/coach_routes.py#L744) |
| GET | `/api/v1/agent/runtime/turns/{turn_id}` | [coach_routes.py:753](../backend/app/api/coach_routes.py#L753) |
| POST | `/api/v1/agent/runtime/conversations` | [coach_routes.py:760](../backend/app/api/coach_routes.py#L760) |
| GET | `/api/v1/knowledge/search` | [knowledge_routes.py:23](../backend/app/api/knowledge_routes.py#L23) |
| POST | `/api/v1/knowledge/ask` | [knowledge_routes.py:48](../backend/app/api/knowledge_routes.py#L48) |
| GET | `/api/v1/knowledge/rag` | [knowledge_routes.py:57](../backend/app/api/knowledge_routes.py#L57) |
| GET | `/api/v1/health` | [routes.py:99](../backend/app/api/routes.py#L99) |
| POST | `/api/v1/auth/demo` | [routes.py:128](../backend/app/api/routes.py#L128) |
| POST | `/api/v1/auth/wechat` | [routes.py:152](../backend/app/api/routes.py#L152) |
| GET | `/api/v1/me/profile` | [routes.py:164](../backend/app/api/routes.py#L164) |
| PUT | `/api/v1/me/profile` | [routes.py:248](../backend/app/api/routes.py#L248) |
| POST | `/api/v1/plans/drafts` | [routes.py:298](../backend/app/api/routes.py#L298) |
| GET | `/api/v1/plans/drafts/{draft_id}` | [routes.py:316](../backend/app/api/routes.py#L316) |
| POST | `/api/v1/plans/drafts/{draft_id}/feedback` | [routes.py:330](../backend/app/api/routes.py#L330) |
| POST | `/api/v1/plans/drafts/{draft_id}/confirm` | [routes.py:349](../backend/app/api/routes.py#L349) |
| POST | `/api/v1/plans/drafts/{draft_id}/reject` | [routes.py:374](../backend/app/api/routes.py#L374) |
| GET | `/api/v1/patch-presets` | [routes.py:392](../backend/app/api/routes.py#L392) |
| GET | `/api/v1/plans/current` | [routes.py:420](../backend/app/api/routes.py#L420) |
| GET | `/api/v1/plans/{plan_id}/versions` | [routes.py:443](../backend/app/api/routes.py#L443) |
| POST | `/api/v1/plans/generate` | [routes.py:516](../backend/app/api/routes.py#L516) |
| POST | `/api/v1/plans/{plan_id}/patches` | [routes.py:552](../backend/app/api/routes.py#L552) |
| GET | `/api/v1/audit/{stream_id}` | [routes.py:619](../backend/app/api/routes.py#L619) |
| GET | `/api/v1/audit/{stream_id}/verify` | [routes.py:659](../backend/app/api/routes.py#L659) |
| POST | `/api/v1/compare/knee-patch` | [routes.py:684](../backend/app/api/routes.py#L684) |
| GET | `/api/v1/evals/scenarios` | [routes.py:700](../backend/app/api/routes.py#L700) |
| POST | `/api/v1/evals/scenarios/{scenario_id}/run` | [routes.py:717](../backend/app/api/routes.py#L717) |
| POST | `/api/v1/plans/{plan_id}/patch-requests` | [routes.py:764](../backend/app/api/routes.py#L764) |
| GET | `/api/v1/patch-requests/{request_id}` | [routes.py:792](../backend/app/api/routes.py#L792) |
| POST | `/api/v1/patch-requests/{request_id}/feedback` | [routes.py:806](../backend/app/api/routes.py#L806) |
| POST | `/api/v1/patch-requests/{request_id}/approve` | [routes.py:825](../backend/app/api/routes.py#L825) |
| POST | `/api/v1/patch-requests/{request_id}/reject` | [routes.py:909](../backend/app/api/routes.py#L909) |
| POST | `/api/v1/plans/{plan_id}/undo` | [routes.py:936](../backend/app/api/routes.py#L936) |
| GET | `/api/v1/patches` | [routes.py:964](../backend/app/api/routes.py#L964) |
| GET | `/api/v1/patches/{patch_id}` | [routes.py:1002](../backend/app/api/routes.py#L1002) |
| POST | `/api/v1/patches/{patch_id}/approve` | [routes.py:1013](../backend/app/api/routes.py#L1013) |
| POST | `/api/v1/patches/{patch_id}/reject` | [routes.py:1084](../backend/app/api/routes.py#L1084) |
| GET | `/api/v1/runs` | [routes.py:1158](../backend/app/api/routes.py#L1158) |
| GET | `/api/v1/runs/{run_id}` | [routes.py:1180](../backend/app/api/routes.py#L1180) |
| POST | `/api/v1/plans/{plan_id}/retarget` | [routes.py:1242](../backend/app/api/routes.py#L1242) |
| POST | `/api/v1/plans/{plan_id}/rollback` | [routes.py:1262](../backend/app/api/routes.py#L1262) |
| POST | `/api/v1/workout-logs` | [routes.py:1301](../backend/app/api/routes.py#L1301) |
| GET | `/api/v1/workout-logs` | [routes.py:1330](../backend/app/api/routes.py#L1330) |
| DELETE | `/api/v1/workout-logs/{log_id}` | [routes.py:1349](../backend/app/api/routes.py#L1349) |
| GET | `/api/v1/stats/calories` | [routes.py:1364](../backend/app/api/routes.py#L1364) |
| POST | `/api/v1/checkins` | [routes.py:1388](../backend/app/api/routes.py#L1388) |
| GET | `/api/v1/checkins` | [routes.py:1413](../backend/app/api/routes.py#L1413) |
| POST | `/api/v1/meal-logs` | [routes.py:1427](../backend/app/api/routes.py#L1427) |
| GET | `/api/v1/meal-logs` | [routes.py:1448](../backend/app/api/routes.py#L1448) |
| DELETE | `/api/v1/meal-logs/{log_id}` | [routes.py:1464](../backend/app/api/routes.py#L1464) |
| POST | `/api/v1/auth/register` | [routes.py:1479](../backend/app/api/routes.py#L1479) |
| POST | `/api/v1/auth/login` | [routes.py:1493](../backend/app/api/routes.py#L1493) |
