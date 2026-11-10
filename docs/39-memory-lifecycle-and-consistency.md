# 39 记忆生命周期、临时上下文 TTL 与计划展示一致性

> 本次升级解决四个遗留问题：短期记忆无生命周期、临时上下文无限累积、
> 今天页/计划页展示口径不同、Profile 与 Agent Memory 双写冲突。

---

## 一、当前数据结构（修改后）

### Profile（结构化事实的唯一来源）

新增：

| 字段 | 用途 |
| --- | --- |
| `preferred_time_window` | 长期训练时间，如 `12:00-13:00` |
| `preferred_time_label` | 时间展示名，如「午休」 |
| `preferred_location` | 长期训练地点（home / gym / office / outdoor） |
| `likes` | 喜欢的动作 |

原有 `goal / training_experience / equipment / weekly_days / session_minutes / dislikes`
继续作为结构化事实来源。

### Memory（Agent 语义记忆）

| 字段 | 说明 |
| --- | --- |
| `kind` | `long_term` / `short_term` |
| `category` | goal / schedule / location / equipment / like / dislike / experience / training_state / session / behavior / note |
| `confidence` | 这条记忆是否真实（0~1） |
| `importance` | 对用户的重要程度（0~1） |
| `last_confirmed_at` | 最近一次确认/复述时间 |
| `expires_at` | 仅 short_term 默认有值；long_term 默认 NULL |
| `status` | `active` / `archived` / `expired` / `superseded` |
| `structured.canonical` | 是否为 Profile 的 canonical 镜像 |

新增 `memory_revisions` 表：每次改值/归档/过期/取代记录旧值与新值。

### TemporaryContext（只对当天有效）

新增/使用：

- `effective_date`：按用户时区的本地日期；
- `status`：`active` / `completed` / `reset` / `expired`；
- `completed_at`：用户结束当天训练后标记；
- `expires_at`：本地当天结束对应的 UTC 时间；
- 旧的 `day` 字段保留用于兼容。

### PlanAdjustment（当天有效计划调整，新增）

| 字段 | 说明 |
| --- | --- |
| `effective_date` | 调整生效的本地日期 |
| `plan_id` / `base_version` | 基于哪份计划/版本，版本变化后自动失效 |
| `status` | `active` / `superseded` / `reset` / `expired` |
| `reason` / `summary` / `changes` | 可追溯的调整原因与结果 |
| `actions` / `constraints` | 当天实际执行动作与约束 |
| `expires_at` | 当天结束 |

数据关系：

```text
PlanVersion（长期，append-only）
        +
PlanAdjustment（某天，active）
        ↓
DailyPlan（运行时有效视图，GET /agent/day）
```

`TemporaryContext` 只存环境事实（今天只有 20 分钟、器械被占用），
不再与计划调整混用。

---

## 二、生命周期策略

| 类型 | 默认保留 | 到期处理 | 真实历史 |
| --- | --- | --- | --- |
| 长期记忆 | 永久 | 不自动删除；用户修改或删除 | — |
| 普通短期记忆 | 30 天 | `status=expired`，保留行与 revision | WorkoutLog / WorkoutSession 不动 |
| 训练状态短期记忆 | 30 天 | 先提炼 `behavior`，再 `expired` | 训练历史不动 |
| 临时上下文 | 14 天 | `status=expired`，不物理删除 | PlanAdjustment 历史保留 |

### 读取时过滤

- `list_memories()` 默认只返回 `status=active` 且 `expires_at > now` 的记录；
- `load_temporary()` 默认只返回 active 且未过期的上下文；
- 不依赖定时任务先跑，定时任务只是日常维护。

### 提炼规则

- 最近 90 天训练 → `long_term / behavior / training_pattern`：
  平均完成率、难度、疲劳、连续未完成；
- 临时上下文重复出现 → `behavior`：
  - 多次缩短时间（≥3 次，中位 ≤35 分钟）
  - 多次改地点（≥3 次）
  - 多次器械冲突（≥3 次）
  - 多次不适（≥2 次）
- **提炼出的行为规律不会自动改 Profile 长期偏好**，只会作为解释/建议上下文。

### 清理任务

- `app/domain/memory_lifecycle.py::run_lifecycle_cleanup`
- 后台任务：`app/main.py` 的 `planharness-memory-maintenance`
  - 默认每 24 小时执行一次；可通过 `MEMORY_CLEANUP_INTERVAL_SECONDS` 配置
  - 关闭：`MEMORY_CLEANUP_ENABLED=0`
- 手动补跑：`backend/scripts/cleanup_memory.py`
- 日志：
  - Python logger `planharness.memory`
  - 审计链 `system:memory` 的 `memory_lifecycle_run` 事件
  - `GET /api/v1/agent/memory/status` 返回最近一次清理汇总

### 时区

新增 `app/domain/timeutil.py`：

- `effective_date` 和 `expires_at` 按用户 `Profile.timezone` 计算；
- 路由/Agent 的「今天」默认使用用户时区；
- 服务器 UTC 时区不会改变用户的「今天」。

---

## 三、计划展示口径

### 今天页

- 展示 `GET /agent/day` 返回的 **DailyPlan**（PlanVersion + active PlanAdjustment）；
- 有临时调整时显示「Agent 已按今天的情况调整」+ 恢复入口；
- 明确说明：只改今天，不改长期计划版本。

### 计划页

- 日历仍展示长期 `PlanVersion` 周期；
- 通过 `GET /agent/adjustments?start=&end=` 获取某天的临时调整；
- 有调整的日期显示「调」标记；
- 选中日期显示：
  - 当天实际执行动作（来自 `/agent/day`）
  - 调整原因、changes、历史记录
  - 「恢复原计划」「恢复这条」
- 长期动作列表标注为「长期计划 · 原版本」，避免误以为临时调整改写了版本。

### 接口

```http
GET  /api/v1/agent/day?date=YYYY-MM-DD
GET  /api/v1/agent/adjustments?start=YYYY-MM-DD&end=YYYY-MM-DD
POST /api/v1/agent/adjustments/{id}/restore
POST /api/v1/agent/day/reset
```

---

## 四、Profile 与 Memory 单一事实来源

### 职责

| 层 | 负责 |
| --- | --- |
| Profile | 目标、经验、时间、地点、器械、喜恶等结构化事实 |
| Workout History | 真实训练记录、完成率、时长、难度、未完成原因 |
| Agent Memory | 经历、偏好原因、长期行为总结、跨能力语义背景 |
| Temporary Context | 今天的时间/地点/器械/身体状态 |

### 写入规则

- 明确长期偏好（「以后」「通常」「一直」或明确的喜恶/目标）：
  1. 更新 Profile；
  2. 同步 `canonical` Memory 镜像；
  3. 写 `memory_revisions`。
- 临时表达（「今天」「今晚」）：
  - 只写 TemporaryContext；
  - 不改 Profile，也不写长期 Memory。
- 模糊冲突（既没有长期信号也没有临时信号，且与 Profile 不一致）：
  - 不写入；
  - 返回确认问题：「这是以后都改，还是只改今天？」。
- 计划版本变化后：
  - active PlanAdjustment 若 `base_version` 不匹配，自动标 `superseded`。

### 示例

| 旧 Profile | 新表达 | 结果 |
| --- | --- | --- |
| 午休 | 「以后晚上训练」 | Profile 改晚上，Memory 同步，留变更记录 |
| 午休 | 「今天晚上有空」 | 只写 TemporaryContext，Profile 不变 |
| 午休 | 「训练时间改到早上」 | 不写入，先询问范围 |
| 深蹲不喜欢 | 「以后不做深蹲」 | Profile.dislikes 改，Memory 同步 |

`load_memory_context()` 现在 **Profile-first**：即使旧 Memory 仍有旧值，
Agent 也以 Profile 为准，避免两套事实互相覆盖。

---

## 五、测试

新增：

- `backend/tests/test_memory_lifecycle.py`（13 个场景）
- `backend/tests/test_db_migration.py`（旧库补列）

覆盖：

1. 30 天前普通短期记忆 → expired，WorkoutLog 不丢
2. 训练状态先提炼 behavior 再过期，WorkoutSession 不丢
3. 过期临时上下文不参与读取/Agent 推理
4. 今天缩短训练不改 PlanVersion、不改长期 Profile
5. 明确修改长期时间偏好 → Profile 与 Memory 同步
6. 只说「今天晚上有空」→ 只写临时上下文，Profile 不变
7. 计划页可看到调整标识、原因、历史并恢复
8. 新旧偏好冲突时先确认范围
9. 多用户记忆严格隔离
10. 用户时区决定 effective_date
11. 档案表单修改同步 canonical Memory
12. 编辑 canonical Memory 反向同步 Profile
13. 清理写审计日志，`/agent/memory/status` 可查

运行：

```bash
cd backend
.venv/bin/python -m pytest -o addopts='' -q
```

---

## 六、迁移

项目仍不引入 Alembic。启动时：

1. `Base.metadata.create_all` 建新表（`memory_revisions` / `plan_adjustments`）；
2. `app/db_migrations.py::ensure_additive_schema` 对已有表补列；
3. `backfill_lifecycle_data` 回填：
   - `temporary_contexts.effective_date = day`、默认 status；
   - 旧 `TemporaryContext.plan_override` → `PlanAdjustment`；
   - 旧短期 Memory 的 expires_at = updated_at + 30 天；
   - `last_confirmed_at` 回填。

生产账号无 DDL 权限时，需要 DBA 预执行等价 ALTER/INDEX。

---

## 七、边界

- 临时上下文与 PlanAdjustment 都只保留最近 14 天可见/可查；更早记录仍在库中为 expired，也可通过数据库/审计追溯。
- 行为规律来自历史统计，不等于用户长期偏好；不会自动改写 Profile。
- 清理任务不处理 `WorkoutLog/WorkoutSession`，真实训练历史永久保留（除非业务另定归档策略）。
- 删除 canonical Memory 会同步清空对应 Profile 字段，避免每日 reconcile 把已删除的镜像重新写回来；
  非 canonical 的经历/行为总结删除后只影响 Agent 语义上下文。
