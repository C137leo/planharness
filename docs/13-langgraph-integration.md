# 13 接入 LangGraph：把补丁函数包成节点

> 目标：不重写业务，只把现有的 `create_knee_patch` / `approve_patch` / `reject_patch` 包进 LangGraph，获得 `interrupt` 和 checkpoint 恢复。
>
> 原则：**业务规则不动，图只负责编排和暂停。**

---

## 0. 先看注册改动

注册流程整体是对的：

- `phone + password`，scrypt 加盐哈希；
- 注册时同时建 `Profile`，避免 `/me/profile` 404；
- 登录失败统一话术，防手机号枚举；
- `token_version` 支持改密后旧 token 失效；
- 26 个测试全绿。

一个部署注意点：

> `Base.metadata.create_all()` 只建新表，不会给已存在的 `users` 表加 `phone` / `password_hash` / `token_version` 列。

- 本地 SQLite：删掉 `planharness.db` 重新 seed 即可；
- 服务器 Postgres：需要 Alembic 迁移，或手动 `ALTER TABLE`；
- 否则启动/登录会出现 `no such column` / `column does not exist`。

---

## 1. 为什么要接 LangGraph

当前审批是：

```text
POST /patches/{id}/approve
  → 改 PlanPatch.status
  → 生成新 PlanVersion
```

问题：

- 审批状态是数据库字段，不是图的暂停点；
- 进程重启后，没有“正在等待审批”的执行上下文；
- 没有条件边、没有 checkpoint、没有可回放的节点 trace；
- 面试官问“LangGraph 用在哪”，现在答不上来。

接图之后：

```text
POST /patches
  → graph 跑到 human_approval
  → interrupt 暂停
  → 返回 patch_id

POST /approve
  → Command(resume=...)
  → 从暂停点继续
  → commit 节点生成 v2
```

这才是真正的 **HITL + checkpoint + conditional edge**。

---

## 2. 两种接法：先粗后细

### 方案 A：粗粒度图（先跑通，1～2 小时）

直接把现有函数当节点：

| 节点 | 调用 | 作用 |
| --- | --- | --- |
| `prepare` | `create_knee_patch` | 生成草稿、安全门、diff、解释、写 pending |
| `approval` | `interrupt` | 暂停等审批 |
| `commit` | `approve_patch` | 生成 v2 |
| `reject` | `reject_patch` | 标记 rejected，不改计划 |

图：

```text
START → prepare → approval
                     ├─ approve → commit → END
                     └─ reject  → reject → END
```

优点：最快拿到 LangGraph、interrupt、checkpoint。  
缺点：`prepare` 内部仍然是一个大函数，trace 只能看到 4 个节点。

### 方案 B：细粒度图（推荐最终形态）

把 `create_knee_patch` 拆成纯函数：

| 节点 | 作用 | 返回 |
| --- | --- | --- |
| `load_context` | 校验用户/计划，读当前版本和 actions | `base_actions` |
| `propose_patch` | 生成 3 个操作 | `operations` |
| `validate_patch` | 应用操作、安全门、diff | `proposed`, `safety`, `diff` |
| `explain_patch` | DeepSeek 解释，失败 fallback | `explanation` |
| `create_draft` | 写 PlanPatch，status=pending | `patch_id` |
| `human_approval` | `interrupt(payload)` | `decision`, `comment` |
| `commit_patch` | 生成新版本 + Approval | `version` |
| `reject_patch` | 标记 rejected | `status` |

图：

```text
START
  → load_context
  → propose_patch
  → validate_patch
        ├─ 失败 → END（不落草稿）
        └─ 通过
  → explain_patch
  → create_draft
  → human_approval
        ├─ approve → commit_patch → END
        └─ reject  → reject_patch → END
```

建议：**先做 A 跑通，再把 `prepare` 拆成 B**。不要一上来重构全部业务。

---

## 3. 状态怎么设计

关键原则：**state 里只放可序列化的 ID / dict，不放 SQLAlchemy ORM 和 Session。**

```text
PatchState:
  user_id: str
  plan_id: str
  available_days: int
  patch_id: str
  operations: list[dict]
  proposed_actions: list[dict]
  safety: list[str]
  diff: dict
  explanation: str
  decision: str          # approve / reject
  comment: str
  version: int
  error: str
  trace: Annotated[list[dict], operator.add]
```

原因：

- LangGraph checkpoint 会序列化 state；
- ORM 对象、DB Session、连接池都不能序列化；
- 节点内部自己开 Session，state 只传 ID。

节点通过闭包拿 `session_factory`：

```text
build_patch_graph(session_factory, explainer) -> CompiledGraph
```

节点内部：

```text
with session_factory() as db:
    ...
```

---

## 4. 节点怎么写：包函数的模板

### 4.1 load_context

目的：把数据库里的计划变成纯 dict，后续节点不碰 ORM。

```text
def load_context(state, config):
    with session_factory() as db:
        user = db.get(User, state["user_id"])
        plan = db.get(Plan, state["plan_id"])
        version = ...
        actions = ...
        return {"base_actions": [...]}
```

效果：后续节点只处理 dict；checkpoint 可序列化。

### 4.2 propose_patch

目的：调用现有“生成 3 个操作”的逻辑。

```text
def propose_patch(state):
    operations = build_knee_operations(base_actions)
    return {"operations": operations}
```

效果：把“提案”和“校验/提交”分开；以后换成 LLM 提案，也只改这个节点。

### 4.3 validate_patch

目的：在内存里应用操作、跑安全门、算 diff，不写库。

```text
def validate_patch(state):
    proposed = apply_operations(base, operations)
    safety = safety_check(proposed, available_days)
    diff = compute_diff(base, proposed, len(operations))
    return {"proposed_actions": proposed, "safety": safety, "diff": diff}
```

效果：

- 失败时直接结束，不创建草稿；
- 成功时把结果传给后续节点；
- 这是“确定性工程兜底”的核心。

### 4.4 explain_patch

目的：让 DeepSeek 只解释，不改计划；失败走 fallback。

```text
def explain_patch(state):
    payload = build_explanation_payload(...)
    text = explainer(payload, operations, diff)
    return {"explanation": text}
```

效果：LLM 调用变成图里独立节点，trace 能看到耗时和 fallback。

### 4.5 create_draft

目的：把草稿写入 DB，状态 pending_approval；**不写 plan_versions**。

```text
def create_draft(state):
    patch = PlanPatch(..., id=state["patch_id"], status="pending_approval")
    db.add(patch); db.commit()
    return {"patch_id": patch.id}
```

效果：审批前计划仍是 v1；但草稿已经持久化，进程重启也能恢复。

### 4.6 human_approval

目的：真正的 LangGraph HITL。

```text
def human_approval(state):
    decision = interrupt({
        "type": "approval_request",
        "patch_id": state["patch_id"],
        "base_version": state["base_version"],
        "changes": ...,
        "diff": state["diff"],
        "explanation": state["explanation"],
    })
    return {"decision": decision["decision"], "comment": decision.get("comment","")}
```

效果：

- 节点执行到 `interrupt` 暂停；
- state 被 checkpoint 保存；
- API 返回 patch_id；
- 用户审批后 `Command(resume=...)` 从这一行继续。

**注意**：`interrupt` 前的代码会在 resume 时重放。  
所以 `create_draft` 不能在 `human_approval` 里面重复执行；草稿要在 interrupt 之前用 `patch_id` 幂等写入。

### 4.7 commit_patch

目的：审批通过后生成新版本。

```text
def commit_patch(state):
    with session_factory() as db:
        patch = db.get(PlanPatch, state["patch_id"])
        plan, version = approve_patch(db, user, patch.id, state["comment"])
        return {"version": version.version}
```

效果：v2 生效，旧版 superseded，Approval 落库。

### 4.8 reject_patch

目的：拒绝时只改状态，不产生新版本。

```text
def reject_patch_node(state):
    with session_factory() as db:
        reject_patch(db, user, state["patch_id"], state["comment"])
        return {"status": "rejected"}
```

效果：v1 保持不变，Audit 有记录。

---

## 5. 条件边怎么写

审批后根据 `decision` 分支：

```text
builder.add_conditional_edges(
    "human_approval",
    lambda s: "commit" if s["decision"] == "approve" else "reject",
    {"commit": "commit_patch", "reject": "reject_patch"},
)
```

校验失败也分支：

```text
builder.add_conditional_edges(
    "validate_patch",
    lambda s: "explain" if not s["safety"] else "end",
    {"explain": "explain_patch", "end": END},
)
```

这就是“为什么用图而不是线性链”：

- 审批有两条路；
- 校验失败直接结束；
- 以后还能加“重试提案”的环。

---

## 6. API 怎么改

### 6.1 创建补丁

```text
POST /plans/{id}/patches
  → patch_id = uuid4()
  → graph.invoke(
        {"user_id":..., "plan_id":..., "available_days":2, "patch_id":patch_id},
        config={"configurable": {"thread_id": patch_id}},
    )
  → 图跑到 interrupt
  → 返回 patch_id + status=waiting_approval
```

### 6.2 审批

```text
POST /patches/{id}/approve
  → graph.invoke(
        Command(resume={"decision":"approve","comment":...}),
        config={"configurable": {"thread_id": patch_id}},
    )
  → commit 节点生成 v2
```

### 6.3 拒绝

```text
POST /patches/{id}/reject
  → graph.invoke(
        Command(resume={"decision":"reject","comment":...}),
        config={"configurable": {"thread_id": patch_id}},
    )
```

前端 API 形状可以不变，只换后端实现。

---

## 7. Checkpointer 怎么选

| 环境 | Saver | 说明 |
| --- | --- | --- |
| 单元测试 | `InMemorySaver` | 快，进程内 |
| 本地跨进程 | `SqliteSaver` | 文件 `runs/patch_graph.sqlite` |
| 生产 | `PostgresSaver` | 多实例、持久化 |

关键：

- `thread_id = patch_id`；
- `POST /approve` 必须使用同一个 thread_id；
- 进程重启后，SQLite 仍能 resume；
- 不要把 InMemorySaver 当成生产方案。

---

## 8. 必须写的测试

1. **暂停在审批前**
   - 调用 create → 有 `__interrupt__`
   - `GET /plans/current` 仍是 v1
   - `PlanPatch.status = pending_approval`

2. **resume approve**
   - `Command(resume={"decision":"approve"})`
   - v2，11 个 action
   - 没有 `goblet_squat`

3. **resume reject**
   - v1 不变
   - patch.status = rejected

4. **重复 resume**
   - 第二次 approve 不生成 v3/重复 Approval

5. **SQLite checkpoint 跨图实例**
   - 关闭旧 graph，新建 graph + 同一 SQLite
   - 仍能 resume

6. **校验失败**
   - 不创建草稿、不 interrupt、直接结束

7. **节点顺序**
   - trace 里是 `load → propose → validate → explain → draft → approval`

---

## 9. 常见坑

| 坑 | 后果 | 处理 |
| --- | --- | --- |
| state 里放 ORM/Session | checkpoint 序列化失败 | 只放 ID/dict |
| interrupt 前有重复副作用 | resume 时重复写草稿 | 用 patch_id 幂等 upsert |
| 每次请求新建 graph 但 thread_id 不同 | resume 找不到 | thread_id 固定为 patch_id |
| 用 InMemorySaver 测跨进程 | 重启丢失 | SQLite/Postgres Saver |
| approve 直接调旧 service | 图没恢复 | 必须 `Command(resume=...)` |
| 校验失败还写草稿 | 脏数据 | validate 通过后再 create_draft |
| LLM 失败抛出 | 接口 500 | explain 节点内 fallback |

---

## 10. 落地顺序

1. 加依赖：`langgraph`、`langgraph-checkpoint-sqlite`。
2. 新建 `app/agent/state.py`、`app/agent/patch_graph.py`。
3. 先做 **方案 A 粗粒度图**，用 `InMemorySaver` 跑通 3 个测试。
4. 把路由 `POST /patches` / `approve` / `reject` 切到 graph，旧 service 保留。
5. 全部测试通过后，再做方案 B 拆分节点。
6. 最后把 Console 的 Trace 接上节点事件。

---

## 11. 做完之后你能讲什么

> 补丁流程不是一串函数调用，而是一张 LangGraph 状态图：  
> `load → propose → validate → explain → draft` 后进入 `interrupt`，  
> checkpoint 以 `patch_id` 为 thread 保存；审批通过用 `Command(resume=...)` 从人工节点继续到 commit，拒绝则走另一条边。  
> 安全门仍然是确定性代码，LLM 只负责解释，所以图的可控性不依赖模型。

这就是从“服务”跨到“Agent 工程”的关键一步。
