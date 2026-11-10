# 38 统一健身教练 Agent：入口、记忆与训练执行

> 这次升级把「健身计划生成工具」接成一个持续工作的个人教练：
> 用户只表达目标或问题，服务端判断**改哪一层**，再把结果落到真实数据上。

---

## 一、一个入口，四种上下文

所有页面共用：

```http
POST /api/v1/agent/chat
{ "text": "...", "page": "today|plan|profile|workout", "date": "YYYY-MM-DD", "session_id": "..." }
```

`page` 只用于 UI 展示和少数默认提示，**不是**业务路由。意图、作用范围、要调用的能力，
全部由 `backend/app/agent/coach_agent.py` 根据文本 + 记忆 + 当前训练会话判断。

| 用户说 | 决策 | 实际调用的既有能力 |
| --- | --- | --- |
| 我想减脂，每周 3 次，每次 30 分钟 | `create_plan` | `domain.coach.create_draft`（skill 计算 + 本地安全门） |
| 今天只有 20 分钟，不去健身房 | `adjust_today` | `domain.day_plan.apply_constraints` → 当天 `PlanAdjustment` + `TemporaryContext` |
| 以后把训练安排在午休 | `update_preference` | `domain.memory` 写 long_term；下次生成自动带上 |
| 这周只能练两天 | `adjust_plan` | `domain.requests` 最小补丁草案 → 用户确认 → 新版本 |
| 器械被占用 / 这个动作我不会 | `adjust_workout` / `exercise_guidance` | `domain.workout.replace_action` / `update_guidance` |
| 今天有点累，但还是完成了 | `log_feedback` | `domain.workout.finish_session` → `WorkoutLog` + short_term |
| 你记得我什么 / 忘掉午休那条 | `query_memory` / `edit_memory` | `domain.memory` |
| 帮我规划今天 | `today_brief` | 原有 `agent.today_agent` 工具链（知识 / 餐食 / 麦当劳） |

没有 API Key、模型失败或用户只是说了一句规则能覆盖的话时，全部走确定性路径；
有模型时只影响「计划草案的解释文案」，不会改变安全门、版本提交和训练记录。

---

## 二、三层记忆

| 层 | 表 | 例子 | 生命周期 | 影响 |
| --- | --- | --- | --- | --- |
| 长期 | `memories(kind=long_term)` | 目标、午休训练、家里有哑铃、不喜欢深蹲 | 持久，可编辑/删除 | 后续所有计划生成与今日安排 |
| 短期 | `memories(kind=short_term)` | 最近 2 次只完成 30%、难度 4/5 | 最近几次训练 | 下一次计划降负荷、回复提示 |
| 临时 | `temporary_contexts` | 今天 20 分钟、今天不去健身房、器械被占用 | 当天（`day` 唯一） | 只改今天的有效计划 / 当前训练 |

写入规则（`domain/memory.py::extract_candidates`）：

- 出现「以后 / 通常 / 一直 / 长期 / 每次 / 每周」→ 可作为长期候选；
- 出现「今天 / 这次 / 现在 / 临时 / 只剩」→ 只写临时上下文，**绝不写长期**；
- 目标和明确的每周天数/单次时长属于稳定信息，可直接写长期；
- 同一 `(user_id, kind, category, key)` 覆盖更新，避免自相矛盾的记忆；
- 不确定是否长期时，合并回复里会说明「只改今天」，不猜测用户长期偏好。

「我的」页面提供 `GET/PUT/DELETE /api/v1/agent/memories`，可以查看、编辑、删除。

### 连续未完成如何真的改变计划

`WorkoutSession.finish` 会写 `completion_pct / difficulty / fatigue`，
`recent_training_state` 从最近几次 finished 会话计算 `consecutive_incomplete`。
`coach.create_draft` 在草案生成后调用 `apply_training_state_adjustments`：

- 连续 ≥2 次完成率 < 60% → 每个动作组数 -1；
- 每场动作 >2 个时去掉最后一个，保留最短可执行版本；
- 在 `payload.adaptive`、`revisions`、`summary` 中留下可解释的痕迹。

---

## 三、今日有效计划：临时调整不碰长期版本

```
PlanVersion（长期，append-only）
        +
PlanAdjustment（当天 active；TemporaryContext 只放环境事实）
        ↓
GET /api/v1/agent/day → 今日有效动作 / 临时调整说明 / 训练会话 / 完成情况
```

`domain/day_plan.py` 只做确定性变换：

1. 按画像 + 当天条件过滤器械（不去健身房 / 办公室 / 器械被占用）；
2. 不适部位用 `safety_rules` 过滤并换同肌群、低风险动作；
3. 按可用分钟数裁剪动作和组数（保留热身与复合动作优先）；
4. 写 `PlanAdjustment`（旧 `TemporaryContext.plan_override` 自动迁移），**不修改 PlanVersion**；
5. 计划版本变了，旧 override 自动失效，避免展示与底稿不一致。

`POST /api/v1/agent/day/reset` 可恢复原计划。

---

## 四、训练执行

表 `workout_sessions` 保存执行过程；`workout_logs` 保存练完的事实。

```http
POST /api/v1/workout-sessions                 开始 / 恢复
GET  /api/v1/workout-sessions/active          今天是否有进行中的训练
POST /api/v1/workout-sessions/{id}/sets       完成一组
POST /api/v1/workout-sessions/{id}/skip       跳过当前动作
POST /api/v1/workout-sessions/{id}/replace    替换当前动作（可带器械/疲劳/剩余时间）
POST /api/v1/workout-sessions/{id}/pause      暂停 / 继续
POST /api/v1/workout-sessions/{id}/finish     完成 + 难度/疲劳/时长反馈
```

硬规则：

- 换动作 / 缩短时间 / 太累，只改「当前及之后」，`completed_sets` 与已完成动作原样保留；
- `finish` 才写 `WorkoutLog`，一条动作一条记录，热量按原有 MET 口径复算；
- 用户说「完成了」而没逐组点时，记为 `user_reported`，不伪造系统观察到的组数；
- 每次提交都会更新短期记忆，并追加用户级审计事件（`workout_started/adjusted/finished`）。

前端 `pages/workout/index.vue` 展示当前动作、第几组、休息倒计时、剩余动作和训练内 Agent 入口。

---

## 五、Agent 决策流水线

```
用户输入
 → 安全筛查（红旗症状直接阻断）
 → 读取上下文（画像 / 计划 / 今日有效动作 / 会话 / 长期+短期记忆 / 临时上下文）
 → 意图理解（规则优先，字段与 LLM 契约一致）
 → 作用范围（none / today / workout / long_term）
 → 调用业务能力（建草案 / 当天 override / 补丁草案 / 训练调整 / 反馈 / 记忆）
 → 数据写入（append-only 版本；临时表；训练会话；训练记录）
 → 结果展示（回复 + changes + actions + 结构化结果）
 → 记忆更新（只写通过长期信号验证的候选）
```

计划级改动仍然保留原有 HITL：

- 生成计划 → `plan_drafts` 草案；
- 结构变更（这周只能练两天 / 停练 / 加强度）→ `patch_requests` 草案 + 安全门；
- 用户确认后才生成新 `PlanVersion`，可撤销 / 回滚 / 审计。

---

## 六、验收测试

`backend/tests/test_coach_agent.py` 覆盖：

1. 初次建立计划（聊天 → 草案 → 确认 → 新版本）
2. 临时缩短到 20 分钟，不改计划版本、不改长期偏好
3. 「以后安排在午休」写入长期记忆，并进入下一份计划的 meta
4. 连续两次未完成 → 下一份计划 `adaptive.load_reduced=true`，组数下降
5. 训练中器械被占用：前两个动作完成记录保留，只替换当前
6. 「这个动作我不会」：知识库指导写进当前训练任务
7. 「完成了」：写真实 WorkoutLog + 短期记忆
8. 记忆查看 / 编辑 / 删除
9. 今天 / 计划 / 我的 三个页面调用同一个 `/agent/chat`
10. 「这周只能练两天」走补丁草案 + 确认，确认前版本不变

运行：

```bash
cd backend
.venv/bin/python -m pytest -q
```

---

## 七、已知边界

- **意图识别是规则优先**：覆盖当前 10 类任务和常见中文说法；非常口语化的长句可能落到 `clarify`，
  会给出三个快捷方向而不是乱猜。字段契约与 LLM 输出一致，后续可替换为模型分类器。
- **没有迁移工具**：本次只新增表（`memories` / `temporary_contexts` / `agent_messages` /
  `workout_sessions`），`Base.metadata.create_all` 会补建；已有表字段未改，无需 Alembic。
- **休息计时在客户端**：服务端只给建议秒数；关掉页面会丢失当前倒计时，但组数进度已落库。
- **未接可穿戴设备 / 心率**：难度和疲劳来自用户自述，不假装有传感器数据。
- **长期记忆与画像各存一份**：画像供 skill 直接计算，记忆供 Agent 解释与跨能力复用；
  冲突时以用户最近一次明确表达为准，并在回复里说明。
