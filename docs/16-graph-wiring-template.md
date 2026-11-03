# 16 让 API 真正走 LangGraph：照着填的模板

> 目标：不改业务规则，只把 API 从「直接调用 domain 函数」切换成「graph.invoke / Command(resume)」。
>
> 当前阻塞：图测试用 `thread-1`，补丁 id 是函数内部生成的 UUID，API 无法把两者对上。

---

## 0. 完成标准

做完后必须满足：

```text
POST /plans/{id}/patches
  → graph 跑到 interrupt
  → 返回 patch_id
  → plan 仍是 v1

POST /patches/{id}/approve
  → Command(resume=approve)
  → commit 节点生成 v2

POST /patches/{id}/reject
  → Command(resume=reject)
  → plan 仍是 v1
```

并且：

- `thread_id == patch_id`
- 现有 50 个测试不退化
- 增加 API 级 graph 测试

---

## 1. 给 create_knee_patch 增加 patch_id

文件：`app/domain/patches.py`

### 1.1 签名

```python
def create_knee_patch(
    db: Session,
    user: User,
    plan_id: str | None = None,
    *,
    patch_id: str | None = None,   # 新增
    available_days: int = 2,
    idempotency_key: str | None = None,
    explainer: Callable[[list[dict], list[dict], dict], str] | None = None,
    token_budget: int = 2000,
) -> PlanPatch:
```

### 1.2 创建 PlanPatch 时使用它

```python
from app.models import new_id  # 如果还没导入

patch = PlanPatch(
    id=patch_id or new_id(),   # 关键
    plan_id=plan.id,
    ...
)
```

### 1.3 为什么

图要用：

```text
thread_id = patch_id
```

如果 patch id 在节点内部才生成，API 在 `graph.invoke` 之前不知道 thread_id，后面无法 resume。

---

## 2. 让 graph 接收 patch_id

文件：`app/agent/state.py`

```python
class PatchState(TypedDict, total=False):
    user_id: str
    plan_id: str
    available_days: int
    patch_id: str       # 新增：由 API 预生成
    run_id: str         # 可选：后面做 AgentRun 生命周期
    explanation: str
    diff: dict
    decision: str
    comment: str
    version: int
    status: str
```

文件：`app/agent/patch_graph.py`

```python
def prepare(state: PatchState, config) -> dict:
    with session_factory() as db:
        user = db.get(User, state["user_id"])
        if user is None:
            raise ValueError(...)

        # API 会传；测试可以退回 thread_id
        patch_id = state.get("patch_id") or config["configurable"]["thread_id"]

        patch = create_knee_patch(
            db,
            user,
            state["plan_id"],
            patch_id=patch_id,
            available_days=state.get("available_days", 2),
            explainer=explainer,
        )
        return {
            "patch_id": patch.id,
            "status": patch.status,
            "explanation": (patch.validation or {}).get("explanation", ""),
            "diff": patch.diff or {},
        }
```

注意：节点函数可以接收第二个参数 `config`，LangGraph 会把运行配置传进来。

---

## 3. 路由：创建补丁走 graph

文件：`app/api/routes.py`

顶部加：

```python
from uuid import uuid4
from langgraph.types import Command
```

### 3.1 配置 helper

```python
def _graph_config(patch_id: str) -> dict:
    return {
        "configurable": {"thread_id": patch_id},
        "recursion_limit": 20,
    }
```

### 3.2 创建补丁

把原来的：

```python
patch = create_knee_patch(...)
return ok(request, _patch_payload(patch), ...)
```

改成：

```python
patch_id = str(uuid4())
config = _graph_config(patch_id)

try:
    out = request.app.state.patch_graph.invoke(
        {
            "user_id": user.id,
            "plan_id": plan_id,
            "available_days": payload.available_days,
            "patch_id": patch_id,
        },
        config,
    )
except PatchValidationError as exc:
    raise HTTPException(422, detail=exc.errors) from exc

# 图应该在 prepare 后停在 interrupt
if "__interrupt__" not in out:
    raise HTTPException(500, detail="graph did not reach approval interrupt")

patch = _owned_patch(db, user, patch_id)
return ok(request, _patch_payload(patch), message="pending_approval")
```

效果：

- 创建补丁时真正执行 LangGraph；
- `prepare` 写草稿；
- `human_approval` 的 `interrupt` 让图暂停；
- `plan_versions` 仍然是 v1。

---

## 4. 路由：审批 / 拒绝走 resume

### 4.1 approve

```python
@router.post("/patches/{patch_id}/approve")
def approve(...):
    patch = _owned_patch(db, user, patch_id)

    if patch.status != "pending_approval":
        raise HTTPException(409, detail=f"patch status is {patch.status}")

    try:
        request.app.state.patch_graph.invoke(
            Command(resume={"decision": "approve", "comment": payload.comment}),
            _graph_config(patch_id),
        )
    except PatchValidationError as exc:
        raise HTTPException(422, detail=exc.errors) from exc
    except Exception as exc:
        # 没有 checkpoint / thread 不存在
        raise HTTPException(409, detail=f"cannot resume graph: {exc}") from exc

    # graph 在另一个 Session 中提交，当前 Session 要丢弃缓存
    db.expire_all()
    plan = _current_plan_payload(db, user)
    return ok(request, {...}, message="committed")
```

### 4.2 reject

```python
try:
    request.app.state.patch_graph.invoke(
        Command(resume={"decision": "reject", "comment": payload.comment}),
        _graph_config(patch_id),
    )
except PatchValidationError as exc:
    raise HTTPException(422, detail=exc.errors) from exc
except Exception as exc:
    raise HTTPException(409, detail=f"cannot resume graph: {exc}") from exc

db.expire_all()
patch = _owned_patch(db, user, patch_id)
return ok(request, _patch_payload(patch), message="rejected")
```

关键：

- `thread_id` 必须等于 `patch_id`；
- 审批前先确认 patch 属于当前用户；
- `db.expire_all()` 防止读到旧版本缓存。

---

## 5. 测试模板

新建 `tests/test_api_graph.py`：

```python
def test_api_uses_graph_for_create_and_approve(client):
    headers = ...
    plan = client.get("/api/v1/plans/current", headers=headers).json()["data"]

    created = client.post(
        f"/api/v1/plans/{plan['plan_id']}/patches",
        headers=headers,
        json={"reason_code": "injury", "available_days": 2},
    )
    assert created.status_code == 200
    patch = created.json()["data"]
    assert patch["status"] == "pending_approval"

    # 审批前仍是 v1
    still = client.get("/api/v1/plans/current", headers=headers).json()["data"]
    assert still["version"] == 1

    approved = client.post(
        f"/api/v1/patches/{patch['id']}/approve",
        headers=headers,
        json={"comment": "ok"},
    )
    assert approved.status_code == 200

    after = client.get("/api/v1/plans/current", headers=headers).json()["data"]
    assert after["version"] == 2
    assert len(after["actions"]) == 11
```

再加：

```python
def test_api_reject_keeps_version(client): ...
def test_api_graph_unknown_patch_returns_404(client): ...
```

---

## 6. 做完后再做 AgentRun 生命周期

这一步先不要混进来。graph 接管之后，再加：

```text
prepare:  run.status = waiting_approval
approve:  run.status = committed
reject:   run.status = rejected
stop_reason
tokens_in/out
latency_ms
```

否则你会同时调“图、状态、token、审计”，很难定位。

---

## 7. 常见坑

| 坑 | 处理 |
| --- | --- |
| `thread_id` 和 `patch_id` 不同 | API 预生成 patch_id，两处都用它 |
| resume 报 thread not found | app 重启后 InMemorySaver 丢了；生产用 PostgresSaver |
| 审批后读到旧 v1 | `db.expire_all()` |
| graph 没走到 interrupt | 检查 `"__interrupt__" in out` |
| `prepare` 里又生成 patch id | 必须使用传入的 `patch_id` |
| 直接调 `approve_patch` | 用 `Command(resume=...)`，否则图状态没推进 |

---

## 8. 验收清单

- [ ] `create_knee_patch` 支持 `patch_id`
- [ ] `PatchState` 有 `patch_id`
- [ ] `prepare` 使用 `state["patch_id"]` 或 `config.thread_id`
- [ ] API create 走 `graph.invoke`
- [ ] API approve/reject 走 `Command(resume=...)`
- [ ] 审批前 v1、approve 后 v2、reject 后 v1
- [ ] 旧测试仍绿
- [ ] 新增 API 级 graph 测试
