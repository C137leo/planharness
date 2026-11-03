# 17 下一阶段模板：AgentRun 生命周期 + 独立 Verifier

> 前置：LangGraph 已经接管 API，53 个测试通过。
>
> 这一阶段只做两件事：
> 1. 让 AgentRun 真实反映「等待审批 / 已提交 / 已拒绝」；
> 2. 提交后跑独立 verifier，用环境事实验收，而不是模型自称完成。

---

## 0. 为什么先做这两个

现在 graph 能跑，但观测层还不完整：

- `create_knee_patch` 在审批前就把 `AgentRun.status` 写成 `succeeded`；
- approve/reject 不更新 run；
- Console 无法区分“等待审批”和“已完成”；
- 没有提交后的独立验收。

先补这两个，才能让 Console、评测、审计有真实数据。

---

## 1. AgentRun 生命周期

### 目标状态机

```text
running
  → waiting_approval     # prepare 写草稿后
      ├─ committed       # approve 提交 v2 后
      └─ rejected        # reject 后
```

失败：

```text
running → failed
waiting_approval → failed（verification 失败）
```

### 1.1 PatchState 加 run_id

`app/agent/state.py`：

```python
class PatchState(TypedDict, total=False):
    ...
    patch_id: str
    run_id: str       # 新增
```

### 1.2 create_knee_patch 返回 run_id

`app/domain/patches.py`：

当前：

```python
run.status = "succeeded"
```

改成：

```python
run.status = "waiting_approval"
```

创建 patch 后：

```python
patch = PlanPatch(..., validation={
    ...,
    "run_id": run.id,
})
db.add(patch)
db.flush()

run.output = {
    "patch_id": patch.id,
    "diff": diff,
    "stop_reason": "waiting_approval",
}
db.commit()
```

> 这样 `patch.validation.run_id` 就能把补丁和运行记录串起来，不用加新列。

### 1.3 prepare 把 run_id 带进 state

`app/agent/patch_graph.py`：

```python
return {
    "patch_id": patch.id,
    "run_id": validation.get("run_id", ""),
    "status": patch.status,
    ...
}
```

### 1.4 commit 更新 run

```python
with session_factory() as db:
    user = db.get(User, state["user_id"])
    _, version = approve_patch(...)

    verification = verify_patch_commit(
        db, plan=..., version=version, patch_id=state["patch_id"]
    )

    run = db.get(AgentRun, state.get("run_id"))
    if run is not None:
        run.status = "committed" if verification.passed else "failed"
        run.error_code = None if verification.passed else "VERIFICATION_FAILED"
        run.output = {
            **(run.output or {}),
            "version": version.version,
            "verification": verification.to_dict(),
            "stop_reason": "completed" if verification.passed else "verification_failed",
        }
        run.finished_at = utcnow()
        db.commit()

    return {"version": version.version, "status": "committed", "verification": verification.to_dict()}
```

### 1.5 reject 更新 run

```python
run = db.get(AgentRun, state.get("run_id"))
if run is not None:
    run.status = "rejected"
    run.output = {**(run.output or {}), "stop_reason": "approval_rejected"}
    run.finished_at = utcnow()
    db.commit()
```

### 1.6 路由返回 verification（可选）

`app/api/routes.py` approve 里：

```python
out = graph.invoke(Command(resume=...), config)
verification = out.get("verification")
return {
    "patch_id": patch_id,
    "committed_version": ...,
    "verification": verification,
    "plan": plan.model_dump(),
}
```

前端可以忽略，Console 可以展示。

---

## 2. 独立 Verifier

### 2.1 新增文件

`app/domain/verification.py`：

```python
@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    checks: list[dict]
    errors: list[str]

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": self.checks, "errors": self.errors}
```

### 2.2 函数

```python
def verify_patch_commit(
    db: Session,
    *,
    plan: Plan,
    version: PlanVersion,
    patch_id: str,
) -> VerificationResult:
    ...
```

检查项：

| check | 规则 |
| --- | --- |
| version_is_current | `plan.current_version == version.version` |
| version_active | `version.status == "active"` |
| action_count | 8 ≤ actions ≤ 20 |
| no_forbidden_knee | 不出现 `goblet_squat/squat/run/jump/lunge` |
| calorie_range | 1200 ≤ calorie_target ≤ 4000 |
| protein_range | 50 ≤ protein_target_g ≤ 250 |
| approval_exists | 有 `Approval(patch_id, decision="approved")` |

实现要点：

- 只读 DB，不调 LLM；
- 每一步都记 `{"name":..., "passed":..., "detail":...}`；
- 任何一项失败，`passed=False`；
- 失败不自动回滚，先记录；自动补偿是下一步。

### 2.3 为什么独立

- 不用 `patch.validation` 里模型/代码自己写的 `passed`；
- 从 **最终数据库状态** 重新算；
- 以后可以放到独立进程/容器里跑。

---

## 3. 测试模板

### 3.1 run 生命周期

```python
def test_run_waits_then_committed(client):
    # create patch via API
    patch = ...
    run_id = patch["validation"]["run_id"]

    with session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "waiting_approval"

    # approve
    ...
    with session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "committed"
        assert run.output["verification"]["passed"] is True
```

### 3.2 reject

```python
assert run.status == "rejected"
assert run.output["stop_reason"] == "approval_rejected"
```

### 3.3 verifier 抓非法动作

```python
def test_verifier_catches_forbidden_action(...):
    version = ...
    action = ...  # 把某个 action 改成 run
    action.action_ref = "run"
    db.commit()

    result = verify_patch_commit(...)
    assert result.passed is False
    assert any(c["name"] == "no_forbidden_knee" and not c["passed"] for c in result.checks)
```

### 3.4 verifier 要求审批记录

```python
def test_verifier_requires_approval(...):
    # 删除 approval 后验证失败
```

---

## 4. 真实 token usage（下一小步，不要混）

现在 `DeepSeekExplainer.explain` 返回 `str`，拿不到 usage。

建议下一小步改成：

```python
@dataclass
class ExplainResult:
    text: str
    tokens_in: int
    tokens_out: int
```

然后：

- `call_deepseek` 返回 `(content, usage)`；
- `explain` 返回 `ExplainResult`；
- `create_knee_patch` 写 `run.tokens_in/out`；
- fallback 的 usage 为 0。

这一步会改 explainer 接口，等 lifecycle/verifier 稳定后再做。

---

## 5. 验收清单

- [ ] run 创建后是 `waiting_approval`
- [ ] approve 后 run 是 `committed`
- [ ] reject 后 run 是 `rejected`
- [ ] run.output 有 `verification`
- [ ] verifier 能从最终 DB 状态抓出非法动作
- [ ] verifier 要求 approval 存在
- [ ] 旧 53 个测试仍绿
- [ ] 新增 4 个测试

---

## 6. 一句话

> 图负责“怎么执行”，AgentRun 负责“执行到哪”，verifier 负责“是否真的完成”。  
> 这三个齐了，才能把“模型说完成了”换成“环境事实说完成了”。
