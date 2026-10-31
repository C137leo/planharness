# 03 领域模型与协议

## 1. 设计原则

1. **数据库是事实源，LangGraph checkpoint 只负责暂停/恢复。**
2. **LLM 只提议，代码做校验和提交。**
3. **计划版本只增不改**；旧版本 `superseded`，回滚生成新版本。
4. **所有写操作必须经过审批 token**；工具权限由后端强制。
5. **安全门不依赖 RAG/LLM**；删掉知识库仍能拦截。
6. **每次运行记录 Trace 和 ContextSnapshot**，保证可回放、可归因。

---

## 2. 数据模型

### 2.1 核心业务表

#### `users`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | uuid PK | 用户 |
| openid | varchar unique nullable | 微信 |
| unionid | varchar nullable | 预留 |
| nickname | varchar | |
| avatar_url | text | |
| role | enum(user, admin) | Console 权限 |
| created_at / updated_at | timestamptz | |

#### `profiles`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| user_id | uuid PK/FK | |
| sex | enum(male, female, other) | |
| birth_year | int | |
| height_cm | numeric(5,1) | |
| weight_kg | numeric(5,1) | 当前体重 |
| activity_level | enum(sedentary, light, moderate, high) | |
| training_experience | enum(beginner, intermediate, advanced) | |
| equipment | jsonb | 器械列表 |
| weekly_days | int | 可练天数 |
| session_minutes | int | 单次时长 |
| diet_pattern | enum(omnivore, vegetarian, vegan) | |
| allergies | jsonb | 过敏原 |
| dislikes | jsonb | 不喜欢的食物 |
| timezone | varchar | 默认 Asia/Shanghai |

#### `goals`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | uuid PK | |
| user_id | uuid FK | |
| type | enum(fat_loss, maintenance, muscle_gain) | P0 只用 fat_loss |
| target_weight_kg | numeric nullable | |
| target_rate_kg_per_week | numeric nullable | |
| target_date | date nullable | |
| priority | int | |
| status | enum(proposed, active, achieved, abandoned, superseded) | 同一用户最多一个 active |
| source | enum(user, inferred) | |
| confirmed_at | timestamptz nullable | |
| created_at | timestamptz | |

#### `constraints`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | uuid PK | |
| user_id | uuid FK | |
| type | enum(injury, allergy, schedule, equipment, food, medical) | |
| code | varchar | 如 knee_pain, peanut_allergy |
| value | jsonb | 具体值 |
| severity | enum(hard, soft) | 硬约束一票否决 |
| source | enum(user, clinician, system) | |
| status | enum(active, expired, conflicted) | |
| effective_from | timestamptz | |
| effective_to | timestamptz nullable | |
| evidence_text | text | 用户原话/记录 |
| created_at | timestamptz | |

约束不做物理删除；过期改 `status/effective_to`，保留审计。

#### `plans`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | uuid PK | |
| user_id | uuid FK | |
| goal_id | uuid FK | |
| status | enum(draft, active, archived) | |
| current_version | int | |
| created_at | timestamptz | |

#### `plan_versions`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | uuid PK | |
| plan_id | uuid FK | |
| version | int | 1,2,3... |
| parent_version | int nullable | |
| created_from_patch_id | uuid nullable | |
| status | enum(draft, active, superseded, rolled_back) | |
| calorie_target | numeric | |
| protein_target_g | numeric | |
| step_target | int | |
| summary | text | |
| created_at / activated_at | timestamptz | |

唯一约束：`(plan_id, version)`。

#### `plan_actions`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | uuid PK | |
| plan_version_id | uuid FK | |
| day_index | int 1-7 | |
| slot | enum(warmup, strength, cardio, mobility, meal_breakfast, meal_lunch, meal_dinner) | |
| seq | int | 同槽位顺序 |
| slot_key | varchar | `day:slot:seq`，跨版本稳定 |
| action_type | enum(exercise, meal) | |
| action_ref | varchar | exercise_id / meal_id |
| sets / reps / duration_min / intensity | 数值/枚举 | 按类型可空 |
| portion_g | numeric nullable | 餐食份量 |
| evidence_ids | jsonb | P2 RAG 引用 |
| metadata | jsonb | |

唯一约束：`(plan_version_id, slot_key)`。

#### `plan_patches`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | uuid PK | |
| plan_id | uuid FK | |
| base_version | int | 乐观锁 |
| goal_id | uuid | |
| constraint_ids | jsonb | 本次依据 |
| reason_code | enum(injury, schedule, plateau, low_adherence, goal_change) | |
| reason_text | text | 给用户看 |
| operations | jsonb | 见 PlanPatch 协议 |
| validation | jsonb | 安全门/校验结果 |
| diff | jsonb | churn 等 |
| status | enum(draft, validated, pending_approval, approved, rejected, committed, superseded, expired) | |
| requires_approval | bool | P0 一律 true |
| idempotency_key | varchar unique | |
| created_by | enum(agent, user) | |
| created_at / updated_at | timestamptz | |

#### `approvals`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | uuid PK | |
| patch_id | uuid FK | |
| actor_user_id | uuid FK | |
| decision | enum(approved, rejected) | |
| comment | text | |
| token_hash | varchar | 提交时校验；不返回模型 |
| decided_at | timestamptz | |

#### `checkins`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | uuid PK | |
| user_id | uuid FK | |
| date | date | |
| weight_kg | numeric | |
| sleep_hours | numeric | |
| hunger_level | int 1-5 | |
| soreness_level | int 1-5 | |
| adherence_pct | int 0-100 | |
| note | text | |
| created_at | timestamptz | |

唯一约束：`(user_id, date)`；提交支持 Idempotency-Key。

#### `reviews`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | uuid PK | |
| user_id / plan_id | uuid | |
| week_index | int | |
| period_start / period_end | date | |
| trend | jsonb | 体重、执行率、睡眠、酸痛 |
| recommendation | jsonb | 建议调整 |
| status | enum(draft, pending_approval, accepted, rejected) | |
| created_at | timestamptz | |

#### `agent_runs`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | uuid PK | |
| user_id | uuid | |
| kind | enum(generate_plan, propose_patch, weekly_review, chat) | |
| mode | enum(naive, guarded, patch) | Console 对比 |
| scenario_id | varchar nullable | |
| status | enum(queued, running, waiting_approval, succeeded, failed) | |
| input / output | jsonb | |
| trace | jsonb | 节点 span |
| context_snapshot_id | uuid nullable | |
| tokens_in / tokens_out | int | |
| latency_ms | int | |
| error_code | varchar nullable | |
| created_at / finished_at | timestamptz | |

#### `context_snapshots`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | uuid PK | |
| run_id | uuid FK | |
| goal_id | uuid | |
| constraint_ids | jsonb | |
| sections | jsonb | policy/goal/constraints/plan/recent/summary/evidence |
| token_count | int | |
| compressed | bool | P0 false |
| compression_ratio | numeric | P1 |
| created_at | timestamptz | |

### 2.2 Harness 表

| 表 | 关键字段 |
| --- | --- |
| `eval_scenarios` | id, name, profile, events, expected_state, expected_path, faults, tags, active |
| `eval_runs` | id, config, model, git_sha, status, metrics, started_at, finished_at |
| `eval_results` | id, eval_run_id, scenario_id, passed, failure_tags, actual_state, trace_run_id |
| `mutation_runs` | id, mutant_name, config_patch, killed, failing_scenarios, mutation_score, created_at |

### 2.3 关系

```text
users 1─1 profiles
users 1─n goals
users 1─n constraints
users 1─n plans 1─n plan_versions 1─n plan_actions
plans 1─n plan_patches 1─0..1 approvals
users 1─n checkins
users 1─n agent_runs 1─0..1 context_snapshots
```

### 2.4 slot_key 与 churn

`slot_key = day_index:slot:seq`，跨版本稳定。

- `total_slots`：当前版本的 action 槽位数。
- `changed_slots`：相同 `slot_key` 下 `action_ref` 或关键参数变化的数量，加上新增/删除的槽位。
- `churn = changed_slots / total_slots`。
- `required_changes`：由策略根据新约束推导出的“必须改”的 slot 集合。
- `unnecessary_churn = |changed - required| / total_slots`。
- `target_changes`：热量/步数/蛋白质变化单独统计，不混入 action churn。

S04 示例：

- v1：12 个 action 槽位，3 个训练日。
- 膝痛 + 只能练 2 天，必须改 3 个：跑步→快走、深蹲→臀桥、删除周六训练。
- C 模式：changed=3，churn=25%；unnecessary=0。
- A 模式：changed=12，churn=100%，且可能保留高风险动作。

---

## 3. PlanPatch 协议

### 3.1 信封

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| patch_id | 是 | |
| plan_id / base_version | 是 | 乐观锁 |
| goal_id | 是 | |
| constraint_ids | 是 | 本次依据 |
| reason_code / reason_text | 是 | injury/schedule/... |
| operations | 是 | 操作列表 |
| evidence_ids | 否 | P2 RAG |
| validation | 是 | 安全门/约束结果 |
| diff | 是 | churn 等 |
| status | 是 | 生命周期 |
| requires_approval | 是 | P0 一律 true |
| idempotency_key | 是 | 防重复 |

### 3.2 操作类型

| 操作 | 参数 | 示例 |
| --- | --- | --- |
| `replace_action` | slot_key, before_action_ref, after_action, reason | 跑步→快走 |
| `remove_action` | slot_key, reason | 删除周六训练 |
| `add_action` | day, slot, seq, action, reason | 增加臀桥 |
| `move_action` | slot_key, to_day, to_slot, reason | 周三→周四 |
| `adjust_target` | target, before, after, reason | 热量 -100 kcal |
| `replace_meal` | slot_key, before_food_ids, after_food_ids, reason | 牛奶→豆浆 |

每个操作带 `op_id`、`reason`、`before`、`after`。  
`before` 用于乐观校验；不匹配则补丁作废。

### 3.3 生命周期

```text
draft → validated → pending_approval → approved → committed
                         ↓
                     rejected / expired
committed → superseded（新版本生效）
```

### 3.4 提交不变量

1. `base_version == current_version`，否则 409 `STALE_VERSION`。
2. 安全门通过；硬约束无违规。
3. 热量/蛋白质/训练天数在校验区间。
4. 有有效 approval token，且 actor 是计划所有者。
5. `idempotency_key` 未被使用。
6. 提交事务内：旧版 `superseded`，新版 `active`，patch `committed`。
7. 审批前不允许任何计划/动作写库。

### 3.5 审批与回滚

- `approve`：后端签发 token，事务内提交；返回新版本。
- `reject`：保留旧版，记录原因。
- `rollback`：指定目标版本，生成新版本；写审计；不调用 LLM。
- token 只存 hash，不返回给模型；`commit_plan` 工具必须校验。

### 3.6 补丁示例

```text
S04：膝痛 + 只能练 2 天

原因：右膝疼痛；本周可练天数 3 → 2
操作：
  1. replace_action  day2:strength:1  跑步 → 快走
  2. replace_action  day1:strength:2  深蹲 → 臀桥
  3. remove_action   day6:strength:1  删除周六训练
保留：其余 9 个 action + 热量/蛋白质目标
diff：changed 3/12，churn 25%，unnecessary 0
状态：pending_approval → approved → v2
```

---

## 4. Agent 图

### 4.1 计划生成图

```text
START
  → load_profile
  → safety_screen
        ├─ 红灯 → stop_and_advise → END
        └─ 绿灯/黄灯
  → compute_targets（代码算热量/蛋白质）
  → retrieve_guidelines（P2 接 RAG；P0 可空）
  → draft_plan（LLM/模板）
  → validate_plan（确定性）
        ├─ 失败 → repair_plan（最多2次）→ validate_plan
        └─ 通过
  → human_confirm（interrupt）
        ├─ 修改 → validate_plan
        └─ 确认 → commit_plan → END
```

### 4.2 计划修复图

```text
START
  → load_current_plan
  → apply_constraint_event
  → propose_patch（LLM 只提候选）
  → validate_patch（安全门 + 约束 + 热量）
        ├─ 不安全 → fallback_template / 重新提案
        └─ 安全
  → compute_diff
  → human_approval（interrupt）
        ├─ 拒绝 → 保留旧版
        └─ 通过 → commit_new_version → END
```

### 4.3 图状态

只放当前运行所需：

- `user_id`
- `goal_id`
- `active_constraint_ids`
- `plan_id`
- `base_version`
- `patch_id`
- `validation`
- `diff`
- `mode`
- `trace_id`

业务数据从 DB 读，不把整份计划塞进图状态。

### 4.4 工具权限

| 类型 | 工具 | 权限 |
| --- | --- | --- |
| 读 | `get_profile` / `get_plan` / `search_knowledge` / `get_food` / `get_exercise` | 只读 |
| 写草稿 | `propose_patch` | 只写 draft，不生效 |
| 提交 | `commit_plan` | 必须 approval token，后端校验 |

Agent 不能直接写 `plan_versions` / `plan_actions`。

### 4.5 降级

- LLM 非法 JSON：重试最多 2 次；仍失败用安全模板补丁。
- 安全门拦截：不提交，返回原因和替代建议。
- 约束冲突：暂停，问用户，不自动选。
- 工具超时：部分结果 + 明确披露；不编造。

---

## 5. API 契约

统一前缀 `/api/v1`，响应包 `{ code, message, data, trace_id }`。

### 5.1 用户侧

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/auth/wechat` | 小程序 code 换 token |
| POST | `/auth/demo` | Web 体验账号 |
| GET/PUT | `/me/profile` | 档案 |
| GET/POST | `/me/goals` | 目标版本 |
| GET/POST/PATCH | `/me/constraints` | 约束账本 |
| POST | `/plans/generate` | 生成计划，返回 run_id |
| GET | `/plans/current` | 当前计划 + 版本 + actions |
| GET | `/plans/{id}/versions` | 版本历史 |
| POST | `/plans/{id}/rollback` | 回滚 |
| POST | `/checkins` | 打卡 |
| POST | `/plans/{id}/patches` | 生成补丁 |
| GET | `/patches/{id}` | 补丁 + diff |
| POST | `/patches/{id}/approve` | 审批并提交 |
| POST | `/patches/{id}/reject` | 拒绝 |
| POST | `/plans/{id}/reviews` | 周复盘 |
| GET | `/runs/{id}` | 运行状态/trace 摘要 |

### 5.2 Console

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/console/scenarios` | 场景列表 |
| POST | `/console/scenarios/{id}/run` | 跑 A/B/C |
| GET | `/console/runs/{id}` | 完整 trace |
| GET | `/console/quality/latest` | 最新报告 |
| POST | `/console/quality/matrix` | 跑三档矩阵 |
| POST | `/console/quality/mutations` | 跑变异测试 |

### 5.3 错误码

- `VALIDATION_ERROR`
- `SAFETY_BLOCK`
- `APPROVAL_REQUIRED`
- `STALE_VERSION`（409）
- `CONSTRAINT_CONFLICT`
- `TOOL_TIMEOUT`
- `RATE_LIMITED`

### 5.4 并发与幂等

- 生成/补丁/审批都支持 `Idempotency-Key`。
- 补丁提交带 `base_version`；不匹配返回 409。
- 审批与提交在同一事务；重复提交返回已有版本，不重复写。

---

## 6. Context 口子（P1）

P0 不实现压缩，但必须落：

- `goals`：目标版本化。
- `constraints`：约束账本。
- `context_snapshots`：每次运行模型看到的上下文。
- `ContextBuilder`：按层拼 Context Pack。

P1 的 Context Pack：

```text
[Policy / 安全规则]        ← 永不压缩
[Goal Card]               ← 永不压缩
[Active Constraints]      ← 永不压缩，逐字
[Current Plan Summary]    ← 结构化摘要
[Recent Turns]            ← 最近 N 轮原文
[Older Summary]           ← 可压缩
[Retrieved Evidence]      ← top-k，带引用
```

丢弃顺序：闲聊 → 旧工具输出 → 旧摘要；绝不丢目标/安全约束。

---

## 7. 扩展端口（P2）

| 端口 | 作用 | P0 |
| --- | --- | --- |
| `KnowledgeProvider` | `retrieve(query, filters) -> Evidence[]` | 空实现/小内置库 |
| `PlanGenerator` | `generate` / `patch` | 模板 + LLM |
| `PolicyPack` | 减脂规则；以后增肌/康复 | `FatLossPolicy` |

`Evidence` 字段：`evidence_id, source, title, snippet, score, retrieved_at`。  
计划/补丁预留 `evidence_ids`，P2 接 RAG 时不用改协议。

---

## 8. 种子数据

- 20 个动作：深蹲、臀桥、俯卧撑、划船、快走、跑步、开合跳等，含禁忌/器械/替代。
- 40 个食物/餐食模板：热量、蛋白质、过敏原。
- 5 个演示画像：正常、素食、乳糖不耐、膝痛、时间紧。
- 1 个 demo 用户：4 周计划 + 3 周打卡历史，方便演示平台期/周复盘。
- 所有数据为合成/公开通用知识，页面标注“演示数据”。
