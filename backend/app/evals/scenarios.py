"""8 个任务级评测场景（E01–E08）。

每个场景都是「从 HTTP 入口进、一路走到数据库事实」的完整链路，
不做 mock 掉核心逻辑的事情 —— 否则评测就退化成单元测试了。

场景约定：
- 每个场景用独立临时 SQLite 库（互不污染）；
- 返回 `ScenarioResult`，既有 `checks`（人看）也有 `sample`（指标聚合用）；
- 断言失败不抛异常，由 runner 统一收集，报告里能看到「哪一条断言红了」。
"""

from __future__ import annotations

import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.domain.patches import FORBIDDEN_KNEE_REFS
from app.main import create_app
from app.skill.bridge import get_bridge
from app.models import AgentRun, Plan, PlanPatch, PlanVersion


# ---------------------------------------------------------------- 数据结构


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Sample:
    """指标聚合用的原始观测。"""

    actions_checked: int = 0
    safety_violations: int = 0
    churn: float | None = None
    # 只统计「自然发生」的验证；注入失败属于演练，不算模型质量
    verifications: list[bool] = field(default_factory=list)
    injected_failures: int = 0
    patches_created: int = 0
    approval_decisions: int = 0
    # 真正产生新版本的提交次数 / 其中有审批记录的次数
    commits: int = 0
    commits_with_approval: int = 0
    fallbacks_used: int = 0
    fallbacks_total: int = 0
    duplicate_attempts: int = 0
    duplicate_commits: int = 0
    required_dropped: int = 0
    # 成本相关（来自 AgentRun 的真实 usage）
    cost_cny: float | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    llm_fallback: bool | None = None


@dataclass
class ScenarioResult:
    id: str
    title: str
    checks: list[Check]
    sample: Sample
    latency_ms: float = 0.0
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.ok for c in self.checks)

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    description: str
    run: Callable[[], ScenarioResult]


SCENARIOS: list[Scenario] = []


def scenario(sid: str, title: str, description: str):
    def deco(fn):
        SCENARIOS.append(Scenario(sid, title, description, fn))
        return fn

    return deco


# ---------------------------------------------------------------- 工具函数


MOCK_USAGE = {"prompt_tokens": 812, "completion_tokens": 96}


def _mock_explainer(usage: dict | None = None):
    """确定性的假模型：返回固定文案 + 固定 usage。

    评测必须离线可复现：不连网、不花钱，但**用量字段是真实的形状**，
    这样成本指标才有意义。
    """
    import httpx
    from app.llm.deepseek import DeepSeekExplainer

    payload = usage or MOCK_USAGE

    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"summary":"改了 3 处","why":"膝痛","risk":"注意疼痛","next":"确认后执行"}'}}
                ],
                "usage": payload,
            },
        )

    return DeepSeekExplainer(
        api_key="eval-mock",
        base_url="https://mock.local/v1",
        model="deepseek-flash",
        timeout_s=1,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).explain


def _install_mock_llm(app, explainer=None) -> None:
    """把图里的 explainer 换成 mock（默认）或指定实现。"""
    from app.agent.patch_graph import build_patch_graph

    app.state.patch_graph = build_patch_graph(
        app.state.session_factory,
        explainer=explainer or _mock_explainer(),
        checkpointer=app.state.checkpointer,
        token_budget=app.state.settings.context_token_budget,
    )


def _env(**overrides):
    """建一个指向临时库的 app + TestClient（调用方负责 with）。

    默认装一个 mock LLM：评测要离线、确定、可复现，同时保留真实 usage 形状。
    """
    tmpdir = tempfile.mkdtemp(prefix="ph-eval-")
    settings = Settings(
        env="dev",
        database_url=f"sqlite:///{tmpdir}/eval.db",
        secret_key="eval-secret-0123456789abcdef0123456789",
        cors_origins="http://localhost:5173",
        deepseek_api_key="",  # 真实 Key 不参与评测
        **overrides,
    )
    app = create_app(settings)
    _install_mock_llm(app)
    return TestClient(app), app


def _latest_run(app) -> AgentRun | None:
    with app.state.session_factory() as session:
        return session.scalar(select(AgentRun).order_by(AgentRun.created_at.desc()))


def _record_cost(sample: Sample, app) -> None:
    """把最近一次 run 的用量/降级信息记进 sample。"""
    run = _latest_run(app)
    if run is None:
        return
    output = run.output or {}
    sample.tokens_in += run.tokens_in or 0
    sample.tokens_out += run.tokens_out or 0
    sample.cost_cny = (sample.cost_cny or 0.0) + float(output.get("cost_cny") or 0.0)
    sample.llm_fallback = bool(output.get("explanation_fallback"))


def _auth(client: TestClient) -> dict[str, str]:
    resp = client.post("/api/v1/auth/demo", json={})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


def _plan(client: TestClient, headers) -> dict:
    resp = client.get("/api/v1/plans/current", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _create_patch(client: TestClient, headers, **payload) -> dict:
    plan = _plan(client, headers)
    body = {"reason_code": "injury", "available_days": 2, **payload}
    resp = client.post(f"/api/v1/plans/{plan['plan_id']}/patches", headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _forbidden_in(actions: list[dict]) -> list[str]:
    return [
        a["action_ref"]
        for a in actions
        if a.get("action_type") == "exercise" and a.get("action_ref") in FORBIDDEN_KNEE_REFS
    ]


def _expect(checks: list[Check], name: str, ok: bool, detail: str = "") -> None:
    checks.append(Check(name, bool(ok), detail))


@contextmanager
def _patched(target, attr: str, value):
    """临时替换模块属性，退出时恢复（避免场景之间互相污染）。"""
    old = getattr(target, attr)
    setattr(target, attr, value)
    try:
        yield
    finally:
        setattr(target, attr, old)


# ---------------------------------------------------------------- E01–E08


@scenario("E01", "正常计划", "新用户拿到 seed 的 v1 计划：12 个动作、热量达标、无禁忌动作")
def e01_normal_plan() -> ScenarioResult:
    checks: list[Check] = []
    sample = Sample()
    client, app = _env()
    with client:
        headers = _auth(client)
        plan = _plan(client, headers)

        _expect(checks, "版本为 v1", plan["version"] == 1, f"version={plan['version']}")
        _expect(checks, "12 个动作槽位", len(plan["actions"]) == 12, f"n={len(plan['actions'])}")
        _expect(checks, "热量目标 1800", plan["calorie_target"] == 1800, str(plan["calorie_target"]))

        # 正常用户没有膝痛约束，不能用膝痛禁忌集来判；这里查结构与基本约束
        missing_slot = [a for a in plan["actions"] if not a.get("slot_key")]
        days = {a["day_index"] for a in plan["actions"] if a.get("slot") == "strength"}
        _expect(checks, "每个动作都有 slot_key", not missing_slot, f"缺失={len(missing_slot)}")
        _expect(checks, "力量训练日 ≥ 2", len(days) >= 2, f"天数={len(days)}")
        _expect(checks, "含餐食槽位", any(a["action_type"] == "meal" for a in plan["actions"]), "")

    return ScenarioResult("E01", "正常计划", checks, sample)


@scenario("E02", "膝痛补丁", "第 2 周膝痛 + 只能练 2 天 → 只改 3 个槽位，churn=25%，审批前不写库")
def e02_knee_patch() -> ScenarioResult:
    checks: list[Check] = []
    sample = Sample()
    client, app = _env()
    with client:
        headers = _auth(client)
        plan = _plan(client, headers)
        patch = _create_patch(client, headers)
        sample.patches_created = 1
        sample.churn = patch["diff"]["churn"]

        _expect(checks, "状态 pending_approval", patch["status"] == "pending_approval", patch["status"])
        _expect(checks, "改动 3 个槽位", patch["diff"]["changed_slots"] == 3, str(patch["diff"]["changed_slots"]))
        _expect(checks, "churn 25%", abs(patch["diff"]["churn"] - 0.25) < 1e-9, str(patch["diff"]["churn"]))
        _expect(checks, "补丁操作数 3", len(patch["operations"]) == 3, str(len(patch["operations"])))

        forbidden = [
            op.get("after_action", {}).get("action_ref")
            for op in patch["operations"]
            if op["op"] == "replace_action"
        ]
        violations = [r for r in forbidden if r in FORBIDDEN_KNEE_REFS]
        sample.actions_checked += len(patch["operations"])
        sample.safety_violations += len(violations)
        _expect(checks, "替换目标不含禁忌动作", not violations, f"命中={violations}")

        still = _plan(client, headers)
        _expect(checks, "审批前计划仍是 v1", still["version"] == 1, f"version={still['version']}")

        _record_cost(sample, app)

        # 负向用例：available_days=1 < 力量日 2 → 安全门必须拦住（这条让 M6 变异体可被杀）
        blocked = client.post(
            f"/api/v1/plans/{plan['plan_id']}/patches",
            headers=headers,
            json={"reason_code": "injury", "available_days": 1},
        )
        _expect(
            checks,
            "安全门拦截非法可用天数",
            blocked.status_code == 422,
            f"HTTP {blocked.status_code}",
        )

    return ScenarioResult("E02", "膝痛补丁", checks, sample)


@scenario("E03", "审批拒绝", "人工驳回 → 补丁 rejected、计划留在 v1、run 记为 rejected")
def e03_reject() -> ScenarioResult:
    checks: list[Check] = []
    sample = Sample()
    client, app = _env()
    with client:
        headers = _auth(client)
        patch = _create_patch(client, headers)
        sample.patches_created = 1

        resp = client.post(
            f"/api/v1/patches/{patch['id']}/reject", headers=headers, json={"comment": "不同意"}
        )
        _expect(checks, "驳回返回 200", resp.status_code == 200, str(resp.status_code))
        sample.approval_decisions = 1

        data = resp.json()["data"]
        _expect(checks, "补丁状态 rejected", data["status"] == "rejected", data["status"])
        _expect(checks, "计划仍为 v1", _plan(client, headers)["version"] == 1, "v1")

        with app.state.session_factory() as db:
            run = db.scalar(select(AgentRun).order_by(AgentRun.created_at.desc()))
            _expect(
                checks,
                "AgentRun 记录 rejected",
                run is not None and run.status == "rejected",
                getattr(run, "status", None),
            )

    return ScenarioResult("E03", "审批拒绝", checks, sample)


@scenario("E04", "审批通过", "审批 → 提交 v2（11 个动作）、独立验证通过、无禁忌动作")
def e04_approve() -> ScenarioResult:
    checks: list[Check] = []
    sample = Sample()
    client, app = _env()
    with client:
        headers = _auth(client)
        patch = _create_patch(client, headers)
        sample.patches_created = 1
        sample.churn = patch["diff"]["churn"]

        resp = client.post(
            f"/api/v1/patches/{patch['id']}/approve", headers=headers, json={"comment": "ok"}
        )
        _expect(checks, "审批返回 200", resp.status_code == 200, str(resp.status_code))
        if resp.status_code != 200:
            return ScenarioResult("E04", "审批通过", checks, sample)

        data = resp.json()["data"]
        sample.approval_decisions = 1
        sample.commits += 1
        sample.commits_with_approval += 1
        verification = data.get("verification") or {}
        sample.verifications.append(bool(verification.get("passed")))

        _expect(checks, "提交版本为 v2", data["committed_version"] == 2, str(data["committed_version"]))
        _expect(checks, "独立验证通过", verification.get("passed") is True, str(verification.get("passed")))
        _expect(checks, "计划变 11 个动作", len(data["plan"]["actions"]) == 11, str(len(data["plan"]["actions"])))

        forbidden = _forbidden_in(data["plan"]["actions"])
        sample.actions_checked = len(data["plan"]["actions"])
        sample.safety_violations = len(forbidden)
        _expect(checks, "提交后无禁忌动作", not forbidden, f"命中={forbidden}")

        # 真实 usage 必须落库（E04 用的是 mock 模型，但走的是同一条代码路径）
        run = _latest_run(app)
        _expect(checks, "run 记录了真实 token", bool(run and run.tokens_in > 0), str(getattr(run, "tokens_in", 0)))
        sample.tokens_in += run.tokens_in or 0
        sample.tokens_out += run.tokens_out or 0
        sample.cost_cny = float((run.output or {}).get("cost_cny") or 0.0)
        sample.llm_fallback = bool((run.output or {}).get("explanation_fallback"))
        _expect(checks, "成本已记账", sample.cost_cny > 0, f"¥{sample.cost_cny}")

    return ScenarioResult("E04", "审批通过", checks, sample)


@scenario("E05", "上下文预算不足", "预算小到装不下 required → 降级为模板文案，但计划照常生成、required 一条不丢")
def e05_context_budget() -> ScenarioResult:
    checks: list[Check] = []
    sample = Sample()
    client, app = _env(context_token_budget=10)  # 远小于 system prompt 的 token 数
    with client:
        headers = _auth(client)
        patch = _create_patch(client, headers)
        sample.patches_created = 1

        context = patch["validation"]["context"]
        _expect(checks, "记录上下文错误", bool(context.get("error")), str(context.get("error")))
        _expect(checks, "required 未被丢弃", context.get("dropped") == ["context"], str(context.get("dropped")))
        sample.required_dropped = 0 if context.get("dropped") == ["context"] else 1

        explanation = patch["validation"].get("explanation", "")
        sample.fallbacks_total += 1
        sample.fallbacks_used += 1 if explanation else 0
        _record_cost(sample, app)
        _expect(checks, "解释降级为非空文本", bool(explanation), explanation[:40])

        _expect(checks, "计划仍为 v1", _plan(client, headers)["version"] == 1, "v1")

    return ScenarioResult("E05", "上下文预算不足", checks, sample)


@scenario("E06", "LLM 超时 / 无 Key", "模型调用抛异常时自动降级为模板文案，接口不报错")
def e06_llm_unavailable() -> ScenarioResult:
    checks: list[Check] = []
    sample = Sample()
    client, app = _env()
    with client:
        headers = _auth(client)

        # 注意：路由现在把补丁创建交给图，图里持有 explainer。
        # 所以必须在「图」这一层注入超时，替换路由里的类是不生效的。
        from app.agent.patch_graph import build_patch_graph

        def _timeout_explainer(messages, operations, diff):
            raise TimeoutError("simulated deepseek timeout")

        app.state.patch_graph = build_patch_graph(
            app.state.session_factory,
            explainer=_timeout_explainer,
            checkpointer=app.state.checkpointer,
            token_budget=app.state.settings.context_token_budget,
        )

        patch = _create_patch(client, headers)
        sample.patches_created = 1
        sample.fallbacks_total += 1

        explanation = patch["validation"].get("explanation", "")
        sample.fallbacks_used += 1 if explanation else 0

        _expect(checks, "补丁仍创建成功", patch["status"] == "pending_approval", patch["status"])
        _expect(checks, "解释降级为非空文本", bool(explanation), explanation[:40] or "（空）")
        _expect(checks, "降级文案提到本次调整", "本次计划调整" in explanation, explanation[:40] or "（空）")
        _record_cost(sample, app)

    return ScenarioResult("E06", "LLM 超时 / 无 Key", checks, sample)


@scenario("E07", "非法补丁 → verifier fail → 自动回滚", "验证器判失败后，计划自动退回提交前内容（12 个动作）")
def e07_verification_failure_rollback() -> ScenarioResult:
    checks: list[Check] = []
    sample = Sample()
    client, app = _env()
    with client:
        headers = _auth(client)
        before = _plan(client, headers)          # 记录提交前的内容，回滚后要一模一样
        patch = _create_patch(client, headers)
        sample.patches_created = 1

        import app.agent.patch_graph as graph_module
        from app.domain.patches import approve_patch as real_approve
        from app.models import Approval

        def _bad_commit(db, user, patch_id, comment=""):
            """模拟 bug：版本提交了，但审批记录没写进去（事实不完整）。"""
            plan_obj, version_obj = real_approve(db, user, patch_id, comment)
            db.query(Approval).filter(Approval.patch_id == patch_id).delete()
            db.commit()
            return plan_obj, version_obj

        # 关键：这里不替换验证器，而是制造一个「真的有问题」的提交，
        # 让验证器自己去发现 —— 这样 M3（验证器恒通过）才会被杀掉。
        with _patched(graph_module, "approve_patch", _bad_commit):
            resp = client.post(
                f"/api/v1/patches/{patch['id']}/approve", headers=headers, json={"comment": "ok"}
            )

        _expect(checks, "审批返回 409", resp.status_code == 409, str(resp.status_code))
        if resp.status_code == 409:
            detail = resp.json()["detail"]
            _expect(checks, "标记已回滚", detail.get("rolled_back") is True, str(detail.get("rolled_back")))
            _expect(
                checks,
                "回滚版本号存在",
                detail.get("rollback_version") == 3,
                str(detail.get("rollback_version")),
            )

        after = _plan(client, headers)
        sample.injected_failures += 1
        sample.commits += 1                      # 提交确实发生过（随后被回滚）
        sample.commits_with_approval += 1

        _expect(checks, "回滚后 12 个动作", len(after["actions"]) == 12, str(len(after["actions"])))
        same_content = [
            (a["slot_key"], a["action_ref"]) for a in after["actions"]
        ] == [(a["slot_key"], a["action_ref"]) for a in before["actions"]]
        _expect(checks, "回滚内容与提交前一致", same_content, "逐槽位比对 slot_key/action_ref")

        _record_cost(sample, app)
        with app.state.session_factory() as db:
            run = db.scalar(select(AgentRun).order_by(AgentRun.created_at.desc()))
            _expect(
                checks,
                "run 标记 VERIFICATION_FAILED",
                run is not None and run.error_code == "VERIFICATION_FAILED",
                getattr(run, "error_code", None),
            )

    return ScenarioResult("E07", "非法补丁 → 自动回滚", checks, sample)


@scenario("E08", "重复审批", "对已提交的补丁再次审批 → 409，且不会多造一个版本")
def e08_duplicate_approve() -> ScenarioResult:
    checks: list[Check] = []
    sample = Sample()
    client, app = _env()
    with client:
        headers = _auth(client)
        patch = _create_patch(client, headers)
        sample.patches_created = 1

        first = client.post(
            f"/api/v1/patches/{patch['id']}/approve", headers=headers, json={"comment": "ok"}
        )
        _expect(checks, "首次审批 200", first.status_code == 200, str(first.status_code))
        sample.approval_decisions = 1
        sample.commits += 1
        sample.commits_with_approval += 1

        second = client.post(
            f"/api/v1/patches/{patch['id']}/approve", headers=headers, json={"comment": "ok again"}
        )
        sample.duplicate_attempts = 1
        _expect(checks, "重复审批 409", second.status_code == 409, str(second.status_code))

        with app.state.session_factory() as db:
            versions = db.scalars(select(PlanVersion)).all()
            sample.duplicate_commits = max(0, len(versions) - 2)  # v1 + v2 是正常的
            _expect(checks, "版本数仍为 2", len(versions) == 2, f"n={len(versions)}")
            plan = db.scalar(select(Plan))
            _expect(checks, "计划停在 v2", plan.current_version == 2, str(plan.current_version))

    return ScenarioResult("E08", "重复审批", checks, sample)


# ---------------------------------------------------------------- E09–E11（教练链路）
#
# 这三条要跑真实的 skill（Node 引擎）。没有 skill 的开发机/CI 上不注册，
# 而不是"假装通过"——评测报告里少三条，比多三条假的诚实。

DAIRY_FOODS = {"希腊酸奶", "牛奶", "乳清蛋白粉", "奶酪", "黄油"}


def _setup_coach_profile(client: TestClient, headers) -> dict:
    resp = client.put("/api/v1/me/profile", headers=headers, json={
        "goal": "fat_loss",
        "sex": "male",
        "birth_year": 1997,
        "height_cm": 178,
        "weight_kg": 78,
        "weekly_days": 3,
        "session_minutes": 40,
        "equipment": ["dumbbell"],
        "allergies": ["乳制品"],
        "goal_note": "3 个月瘦 8 公斤，不想每天称重",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def e09_coach_generation() -> ScenarioResult:
    """生成整份计划：训练日、食谱、推荐摄入都要有，且过敏原被排除。"""
    checks: list[Check] = []
    sample = Sample()
    client, app = _env()
    with client:
        headers = _auth(client)
        _setup_coach_profile(client, headers)

        resp = client.post("/api/v1/plans/drafts", headers=headers,
                           json={"instruction": "减脂，每周 3 天每次 40 分钟，家里有哑铃"})
        _expect(checks, "生成返回 200", resp.status_code == 200, str(resp.status_code))
        draft = resp.json()["data"]
        meta = draft.get("meta") or {}

        _expect(checks, "草案状态 ready", draft["status"] == "ready", draft["status"])
        _expect(checks, "训练日 = 每周 3 天", len(draft["days"]) == 3, str(len(draft["days"])))
        _expect(checks, "食谱覆盖 7 天", len(draft["meals"]) == 28, str(len(draft["meals"])))
        _expect(checks, "推荐摄入高于基础代谢",
                (draft["nutrition"].get("target_kcal") or 0) > (draft["nutrition"].get("bmr") or 0),
                str(draft["nutrition"].get("target_kcal")))
        _expect(checks, "技能工具被真实调用",
                {"coach_safety_gate", "coach_plan_program", "coach_nutrition_targets"}
                <= set(meta.get("tools_called") or []),
                str(meta.get("tools_called")))
        _expect(checks, "未走模型降级（无 Key 时应走确定性编排）",
                meta.get("model_fallback") is True, str(meta.get("model_fallback")))

        names = {item["name"] for meal in draft["meals"] for item in meal["items"]}
        blocked = names & DAIRY_FOODS
        _expect(checks, "乳制品过敏被排除", not blocked, f"命中={sorted(blocked)}")

        # 确认前不写版本
        before = _plan(client, headers)
        _expect(checks, "草案阶段版本不变", before["version"] == 1, str(before["version"]))

        resp = client.post(f"/api/v1/plans/drafts/{draft['draft_id']}/confirm",
                           headers=headers, json={"comment": "确认"})
        _expect(checks, "确认返回 200", resp.status_code == 200, resp.text[:120])
        body = resp.json()["data"]
        plan = body["plan"]
        _expect(checks, "版本 +1", body["committed_version"] == before["version"] + 1,
                str(body["committed_version"]))
        _expect(checks, "计划来源为 coach", plan["source"] == "coach", plan["source"])
        _expect(checks, "餐食已落库", bool(plan["meals"]), str(len(plan["meals"])))
        _expect(checks, "今日安排可读", plan["today"] is not None, "")

        sample.actions_checked += len(plan["actions"])
        sample.commits += 1
        sample.commits_with_approval += 1

    return ScenarioResult("E09", "教练计划生成", checks, sample)


def e10_free_text_change() -> ScenarioResult:
    """自由文本改动：休息、只能练两天，都要能落到具体槽位，且审批前不写库。"""
    checks: list[Check] = []
    sample = Sample()
    client, app = _env()
    with client:
        headers = _auth(client)
        plan = _plan(client, headers)

        rest = client.post(f"/api/v1/plans/{plan['plan_id']}/patch-requests",
                           headers=headers, json={"text": "今天想休息"})
        _expect(checks, "休息请求 200", rest.status_code == 200, str(rest.status_code))
        rest_data = rest.json()["data"]
        _expect(checks, "休息草案 ready", rest_data["status"] == "ready", rest_data["status"])
        _expect(checks, "休息有具体操作",
                bool((rest_data.get("draft") or {}).get("operations")),
                str(len((rest_data.get("draft") or {}).get("operations") or [])))

        limited = client.post(f"/api/v1/plans/{plan['plan_id']}/patch-requests",
                              headers=headers, json={"text": "这周只能练两天，每次 30 分钟"})
        limited_data = limited.json()["data"]
        _expect(checks, "减天数草案 ready", limited_data["status"] == "ready", limited_data["status"])
        removes = [op for op in (limited_data.get("draft") or {}).get("operations", [])
                   if op["op"] == "remove_action"]
        _expect(checks, "减天数会删掉多余训练日", bool(removes), f"删除 {len(removes)} 个槽位")

        unchanged = _plan(client, headers)
        _expect(checks, "草案不改版本", unchanged["version"] == plan["version"],
                f"{plan['version']} → {unchanged['version']}")

        approve = client.post(
            f"/api/v1/patch-requests/{rest_data['request_id']}/approve",
            headers=headers, json={"comment": "确认休息"},
        )
        _expect(checks, "确认应用 200", approve.status_code == 200, approve.text[:120])
        after = approve.json()["data"]
        _expect(checks, "版本 +1", after["committed_version"] == plan["version"] + 1,
                str(after["committed_version"]))
        sample.patches_created += 1
        sample.approval_decisions += 1
        sample.commits += 1
        sample.commits_with_approval += 1
        sample.actions_checked += len(after["plan"]["actions"])

        audit = client.get(f"/api/v1/audit/{plan['plan_id']}", headers=headers).json()["data"]
        actions = {event["action"] for event in audit}
        _expect(checks, "审计记录了生成与应用",
                {"patch_created", "approval_granted", "patch_committed"} <= actions, str(sorted(actions)))

    return ScenarioResult("E10", "自由文本改动", checks, sample)


def e11_draft_round_limit() -> ScenarioResult:
    """生成草案最多改 3 轮：第 4 轮拒绝，旧草案不能再确认。"""
    checks: list[Check] = []
    sample = Sample()
    client, app = _env()
    with client:
        headers = _auth(client)
        _setup_coach_profile(client, headers)

        first = client.post("/api/v1/plans/drafts", headers=headers,
                            json={"instruction": "减脂，每周 3 天"}).json()["data"]
        current = first
        for index in range(3):
            resp = client.post(f"/api/v1/plans/drafts/{current['draft_id']}/feedback",
                               headers=headers, json={"text": f"再保守一点 {index}"})
            _expect(checks, f"第 {index + 2} 轮可改", resp.status_code == 200, str(resp.status_code))
            current = resp.json()["data"]

        _expect(checks, "轮次累加到 4", current["round"] == 4, str(current["round"]))

        over = client.post(f"/api/v1/plans/drafts/{current['draft_id']}/feedback",
                           headers=headers, json={"text": "再改一次"})
        _expect(checks, "第 5 轮被拒", over.status_code == 422, str(over.status_code))

        stale = client.post(f"/api/v1/plans/drafts/{first['draft_id']}/confirm",
                            headers=headers, json={"comment": "旧草案"})
        _expect(checks, "被取代的草案不能确认", stale.status_code == 422, str(stale.status_code))

        _expect(checks, "全程没有写版本", _plan(client, headers)["version"] == 1,
                str(_plan(client, headers)["version"]))

    return ScenarioResult("E11", "生成草案轮次上限", checks, sample)


# 只有 skill 真的可用时才注册这三条，避免在无 skill 的机器上假通过
if get_bridge().available:
    SCENARIOS.extend([
        Scenario("E09", "教练计划生成",
                 "模型→skill 生成整份计划：训练日 / 食谱 / 摄入目标，过敏原被排除，确认后才落版本",
                 e09_coach_generation),
        Scenario("E10", "自由文本改动",
                 "休息 / 只能练两天 → 具体槽位操作；审批前不写库，审批后版本 +1",
                 e10_free_text_change),
        Scenario("E11", "生成草案轮次上限",
                 "草案最多改 3 轮，第 4 轮 422；被取代的草案不能确认",
                 e11_draft_round_limit),
    ])
