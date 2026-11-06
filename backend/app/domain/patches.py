"""补丁（PlanPatch）领域服务：生成 → 安全门 → 人工审批 → 提交新版本 / 回滚。

这是 PlanHarness 的核心：不重新生成整份计划，只对少数槽位做最小替换，
并用确定性安全门 + 人工审批两道闸口，保证"安全、低 churn、可回滚"。

数据流：
    create_knee_patch  → 落 pending_approval 草稿（不动计划版本）
    approve_patch      → 重跑安全门 → 生成新版本 → 记录审批 → committed
    reject_patch       → 只改状态并留痕
    rollback_plan      → 用旧版本内容再提交一个新版本
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.context import ContextBudgetError, ContextBuilder, ContextBundle
from app.audit import append_event
from app.llm.fallback import fallback_explanation
from app.llm.types import LlmBudget, as_explain_result
from app.llm.prompts import SYSTEM_PROMPT, build_explanation_payload
from app.models import (
    MealPlan,
    AgentRun,
    Approval,
    Checkin,
    ContextSnapshot,
    Goal,
    Plan,
    PlanAction,
    PlanPatch,
    PlanVersion,
    User,
)

logger = logging.getLogger(__name__)

# 膝痛场景的确定性禁忌动作集：出现即拒绝，不交给模型判断
FORBIDDEN_KNEE_REFS = {"squat", "goblet_squat", "run", "jump", "lunge"}

# "没传 meals_from" ≠ "不要餐食"：前者要沿用基线版本的餐食，
# 后者（比如按新目标重建餐食）必须留空，否则会复制旧餐再写新餐 → 一天两套菜。
MEALS_FROM_UNSET = object()


class PatchValidationError(ValueError):
    """补丁校验/状态机失败：携带错误列表，由接口层转成 422。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _action_dict(action: PlanAction) -> dict[str, Any]:
    """ORM 动作 → 纯 dict。

    补丁的"应用操作 / 算 diff / 跑安全门"全部在纯 dict 上做，
    这样计算过程与数据库解耦，也避免误改已提交的 ORM 实体。
    """
    return {
        "slot_key": action.slot_key,
        "day_index": action.day_index,
        "slot": action.slot,
        "seq": action.seq,
        "action_type": action.action_type,
        "action_ref": action.action_ref,
        "sets": action.sets,
        "reps": action.reps,
        "duration_min": action.duration_min,
        "intensity": action.intensity,
        "portion_g": action.portion_g,
        "meta": dict(action.meta or {}),
        "evidence_ids": list(action.evidence_ids or []),
    }


def _load_plan(db: Session, user: User, plan_id: str) -> Plan:
    """按 ID 取计划并校验归属（越权一律当作不存在）。"""
    plan = db.get(Plan, plan_id)
    if plan is None or plan.user_id != user.id:
        raise PatchValidationError(["plan not found"])
    return plan


def _current_version(db: Session, plan: Plan) -> PlanVersion:
    """取计划当前生效的版本对象。"""
    version = db.scalar(
        select(PlanVersion).where(
            PlanVersion.plan_id == plan.id,
            PlanVersion.version == plan.current_version,
        )
    )
    if version is None:
        raise PatchValidationError(["current plan version not found"])
    return version


def _actions_for_version(db: Session, version_id: str) -> list[PlanAction]:
    """按展示顺序读出某版本的全部动作。"""
    return list(
        db.scalars(
            select(PlanAction)
            .where(PlanAction.plan_version_id == version_id)
            .order_by(PlanAction.day_index, PlanAction.slot, PlanAction.seq)
        ).all()
    )


def _apply_operations(base: list[dict], operations: list[dict]) -> list[dict]:
    """在动作快照上应用补丁操作，返回应用后的新列表（不修改入参）。

    支持的操作：
    - remove_action：按 slot_key 删除槽位；
    - replace_action：替换槽位内容，可选 before_action_ref 做乐观并发校验
      （若槽位已被别的补丁改过就判为 stale，避免"基于旧内容的改动"覆盖新内容）。
    """
    by_key = {item["slot_key"]: dict(item) for item in base}
    for op in operations:
        kind = op["op"]
        slot_key = op.get("slot_key")
        if kind == "remove_action":
            by_key.pop(slot_key, None)
        elif kind == "replace_action":
            if slot_key not in by_key:
                raise PatchValidationError([f"slot not found: {slot_key}"])
            current = by_key[slot_key]
            before = op.get("before_action_ref")
            if before and current["action_ref"] != before:
                raise PatchValidationError([f"stale patch at {slot_key}"])
            after = op["after_action"]
            current.update(after)
        else:
            raise PatchValidationError([f"unsupported op: {kind}"])
    return sorted(by_key.values(), key=lambda item: (item["day_index"], item["slot"], item["seq"]))


def _diff(base: list[dict], proposed: list[dict], required_changes: int) -> dict:
    """计算变更指标，作为"最小补丁"的量化证据。

    口径：
    - changed_slots：新增/删除，或 action_ref/sets/reps/duration 发生变化的槽位数；
    - churn = changed / total，越低说明对原计划扰动越小；
    - unnecessary_churn：超出"必要改动数"的那部分扰动，理想值为 0。
    """
    base_map = {item["slot_key"]: item for item in base}
    proposed_map = {item["slot_key"]: item for item in proposed}
    changed = 0
    for key in set(base_map) | set(proposed_map):
        left = base_map.get(key)
        right = proposed_map.get(key)
        if left is None or right is None:
            # 槽位被删除或新增，都算一次变更
            changed += 1
            continue
        if (
            left["action_ref"] != right["action_ref"]
            or left.get("sets") != right.get("sets")
            or left.get("reps") != right.get("reps")
            or left.get("duration_min") != right.get("duration_min")
        ):
            changed += 1
    total = len(base)
    return {
        "total_slots": total,
        "changed_slots": changed,
        "churn": round(changed / total, 4) if total else 0,
        "required_changes": required_changes,
        "unnecessary_churn": round(max(0, changed - required_changes) / total, 4) if total else 0,
        "unchanged_ratio": round((total - changed) / total, 4) if total else 0,
    }


# skill 生成的动作用的是 slug（goblet-squat），模板用的是 ref（goblet_squat），
# 安全门要同时认得这两种写法，否则换了计划来源就静默漏检。
KNEE_RISK_KEYWORDS = ("squat", "lunge", "run", "jump", "step-up", "split")
KNEE_RISK_MOVEMENTS = {"knee_dominant"}


def is_knee_risky(item: dict) -> bool:
    """这个动作对膝盖是否有风险（保留旧入口，逻辑委托给通用规则表）。"""
    from app.domain.safety_rules import is_risky_for

    return is_risky_for(str(item.get("action_ref") or ""), "knee", item.get("meta") or {})


def _safety_check(proposed: list[dict], available_days: int, *,
                  check_knee: bool = True, regions: list[str] | None = None) -> list[str]:
    """确定性安全门（第一道闸口）：膝痛禁忌动作 + 训练日不超过可用天数。

    `check_knee` 只在"膝痛换动作"这类补丁里开启：
    - 「今天休息」「受伤停练」这类补丁本来就会保留部分原动作（只是删掉当天/部位），
      用膝痛禁忌去卡它，会让合理的补丁在审批时被 422 拦住；
    - 模板膝痛补丁和 injury_swap 仍然必须跑这道检查。

    返回错误列表，空表示通过。审批时会再跑一次，防止两次审批之间计划被改动。
    """
    from app.domain.safety_rules import why_risky

    # 兼容两种调用：老代码给 check_knee=True，新代码直接给 regions=[...]
    scoped = list(regions) if regions is not None else (["knee"] if check_knee else [])
    errors: list[str] = []
    for item in proposed:
        if item["action_type"] != "exercise":
            continue
        hit = why_risky(item["action_ref"], scoped, item.get("meta") or {})
        if hit:
            name = (item.get("meta") or {}).get("name_zh") or item["action_ref"]
            errors.append(f"{hit}禁忌动作: {name}")
    strength_days = {
        item["day_index"]
        for item in proposed
        if item["action_type"] == "exercise" and item["slot"] == "strength"
    }
    if len(strength_days) > available_days:
        errors.append(f"力量训练日 {len(strength_days)} 超过可用 {available_days} 天")
    return errors


def _goal_text(db: Session, plan: Plan) -> str:
    """目标卡片：让模型知道"为什么要减脂、减多少"。"""
    goal = db.get(Goal, plan.goal_id) if plan.goal_id else None
    if goal is None:
        return "目标：减脂（暂无明细）"
    return (
        f"目标类型：{goal.type}；目标体重：{goal.target_weight_kg} kg；"
        f"每周下降：{goal.target_rate_kg_per_week} kg；优先级：{goal.priority}"
    )


def _recent_history_text(db: Session, user: User) -> str:
    """最近 3 天打卡摘要；没有记录就返回空串（不占预算）。"""
    rows = db.scalars(
        select(Checkin)
        .where(Checkin.user_id == user.id)
        .order_by(Checkin.date.desc())
        .limit(3)
    ).all()
    if not rows:
        return ""
    return "\n".join(
        f"{row.date}：体重 {row.weight_kg}kg，睡眠 {row.sleep_hours}h，依从性 {row.adherence_pct}%"
        for row in rows
    )


def _build_context(
    db: Session,
    user: User,
    plan: Plan,
    version: PlanVersion,
    payload: dict,
    reason_text: str,
    available_days: int,
    token_budget: int,
) -> ContextBundle:
    """登记 7 段候选材料，交给 ContextBuilder 裁剪。

    priority 决定「预算不够时牺牲谁」，order 决定「模型读到的先后」——
    两者故意分开：把 plan_summary 的优先级调高，不应该让它在正文里前移。
    """
    builder = ContextBuilder(budget=token_budget)

    # required：安全边界与目标，任何情况下都不能丢
    builder.add_section("system_policy", SYSTEM_PROMPT, priority=100, order=10, required=True)
    builder.add_section(
        "constraints",
        f"用户诉求：{reason_text}\n本周可用训练天数：{available_days} 天",
        priority=90,
        order=20,
        required=True,
    )
    builder.add_section("goal_card", _goal_text(db, plan), priority=80, order=30, required=True)

    # 可选材料：从高到低，装不下就被裁掉
    builder.add_section(
        "patch_changes", {"changes": payload["changes"]}, priority=70, order=40
    )
    builder.add_section(
        "safety_diff", {"safety": payload["safety"], "diff": payload["diff"]},
        priority=60,
        order=50,
    )
    builder.add_section("plan_summary", version.summary, priority=50, order=60)

    history = _recent_history_text(db, user)
    if history:
        builder.add_section("recent_history", history, priority=30, order=70)

    return builder.build()


def create_knee_patch(
    db: Session,
    user: User,
    plan_id: str | None=None,
    *,
    patch_id: str | None = None,
    available_days: int = 2,
    idempotency_key: str | None = None,
    explainer: Callable[[list[dict], list[dict], dict], object] | None = None,
    token_budget: int = 2000,
    llm_budget: LlmBudget | None = None,
) -> PlanPatch:
    """生成"膝痛 + 只能练 N 天"的最小补丁草稿。

    固定三个操作（对应 README 的 3/12、churn 25%）：
    1. 深蹲 → 臀桥（去掉膝关节屈伸负荷）；
    2. 快走 → 低冲击快走（降低冲击）；
    3. 删除第 5 天力量训练（本周只练 available_days 天）。
    注意：这里只写 PlanPatch 草稿，不产生新版本——审批通过前计划保持原样。
    """
    plan = _load_plan(db, user, plan_id)
    version = _current_version(db, plan)
    base = [_action_dict(item) for item in _actions_for_version(db, version.id)]

    # 定位本次要改的三个槽位；找不到说明计划结构已变化，直接判失败而不是猜
    squat = next(
        (
            item
            for item in base
            if item["action_ref"] in {"squat", "goblet_squat", "bodyweight_squat"}
        ),
        None,
    )
    walk = next((item for item in base if item["action_ref"] == "brisk_walk"), None)
    # 不写死第几周/第几天：取最后一个 row 力量槽位。
    # 这样 seed v1、重新生成的 v2、以及历史版本都能触发同一个 demo 补丁。
    row_candidates = [
        item
        for item in base
        if item["action_ref"] == "row"
        and item["action_type"] == "exercise"
        and item["slot"] == "strength"
    ]
    row = max(row_candidates, key=lambda item: item["day_index"]) if row_candidates else None
    if squat is None or walk is None or row is None:
        # 打完一次补丁后，risky 动作就被换掉了；再打一次自然「无可替换槽位」。
        # 这不是错误，而是提示调用方：要重走演示，先全量重新生成计划。
        raise PatchValidationError(
            [
                "当前计划已不含可替换的 squat/brisk_walk/row 槽位；"
                "如需再次演示膝痛补丁，请先全量重新生成计划（POST /api/v1/plans/generate）"
            ]
        )

    operations = [
        {
            "op": "replace_action",
            "slot_key": squat["slot_key"],
            "before_action_ref": squat["action_ref"],
            "after_action": {
                "action_ref": "glute_bridge",
                "sets": 3,
                "reps": 15,
                "duration_min": None,
                "meta": {"name": "臀桥", "equipment": []},
            },
            "reason": "膝痛：深蹲改为低冲击臀桥",
        },
        {
            "op": "replace_action",
            "slot_key": walk["slot_key"],
            "before_action_ref": walk["action_ref"],
            "after_action": {
                "action_ref": "easy_walk",
                "duration_min": 25,
                "meta": {"name": "低冲击快走", "equipment": []},
            },
            "reason": "膝痛：快走降为低冲击",
        },
        {
            "op": "remove_action",
            "slot_key": row["slot_key"],
            "before_action_ref": row["action_ref"],
            "reason": f"本周只能练 {available_days} 天，删除第 {row['day_index']} 天的一个力量动作",
        },
    ]
    # 先在内存里应用并过安全门；不通过就不落库（也不返回半成品补丁）
    proposed = _apply_operations(base, operations)
    safety = _safety_check(proposed, available_days)
    if safety:
        raise PatchValidationError(safety)

    diff = _diff(base, proposed, required_changes=len(operations))
    # available_days 存进 diff，审批时会取出来重跑安全门
    diff["available_days"] = available_days
    # 注意末尾不要留逗号：`x = f"...",` 得到的是 tuple，会把整段文本变成数组
    reason_text = f"右膝疼痛，本周只能练 {available_days} 天"
    payload = build_explanation_payload(
        reason_text=reason_text,
        base_version=plan.current_version,
        operations=operations,
        diff=diff,
        safety=safety,
    )

    # 一次运行 = 一条 AgentRun；ContextSnapshot 挂在它下面（run_id 非空外键）
    run = AgentRun(
        user_id=user.id,
        kind="knee_patch",
        mode="patch",
        status="running",
        input={"reason_code": "injury", "available_days": available_days},
    )
    db.add(run)
    db.flush()

    bundle: ContextBundle | None = None
    context_error: str | None = None
    try:
        bundle = _build_context(
            db, user, plan, version, payload, reason_text, available_days, token_budget
        )
    except ContextBudgetError as exc:
        # 上下文配错了：计划照常生成，只把「解释」降级成模板文案
        context_error = str(exc)

    budget = llm_budget or LlmBudget()
    explain_result = None
    budget_reason: str | None = None

    # 调用前先看 prompt 估算：明显超预算就别花这笔钱
    if bundle is not None:
        budget_reason = budget.check_prompt(bundle.token_estimate)

    if explainer is not None and bundle is not None and budget_reason is None:
        try:
            explain_result = as_explain_result(
                explainer(bundle.messages, operations, diff)
            )
        except Exception as exc:  # noqa: BLE001 - 解释失败不能影响补丁
            explain_result = None
            budget_reason = None
            logger.warning("explainer failed: %s", exc)

    if explain_result is None:
        explain_result = as_explain_result(fallback_explanation(operations, diff))
        tokens_in = tokens_out = latency_ms = 0
    else:
        tokens_in = explain_result.tokens_in
        tokens_out = explain_result.tokens_out
        latency_ms = explain_result.latency_ms

    explanation = explain_result.text

    # 调用后核对实际用量/成本（只有真调了模型才有数）
    if budget_reason is None and explain_result.tokens_in + explain_result.tokens_out > 0:
        budget_reason = budget.check_usage(tokens_in, tokens_out)

    cost_cny = budget.cost_cny(tokens_in, tokens_out)
    run.tokens_in = tokens_in
    run.tokens_out = tokens_out
    run.latency_ms = latency_ms

    # 无论是否裁剪都写快照：否则你永远不知道「裁剪发生率」的分母是多少
    snapshot_fields = (
        bundle.to_snapshot_fields()
        if bundle is not None
        else {
            "sections": [],
            "token_count": 0,
            "dropped": [
                {"name": "context", "reason": "required_over_budget", "detail": context_error}
            ],
            "compressed": False,
            "compression_method": None,
        }
    )
    snapshot = ContextSnapshot(
        run_id=run.id,
        user_id=user.id,
        purpose="explain_patch",
        token_budget=token_budget,
        **snapshot_fields,
    )
    db.add(snapshot)
    db.flush()
    run.context_snapshot_id = snapshot.id
    run.status = "waiting_approval"

    # patch_id 允许外部指定：REST 层把它同时用作 LangGraph 的 thread_id，
    # 不指定时由模型的 default=new_id 生成。
    patch = PlanPatch(
        **({"id": patch_id} if patch_id else {}),
        plan_id=plan.id,
        base_version=plan.current_version,
        goal_id=plan.goal_id,
        constraint_ids=[],
        reason_code="injury",
        reason_text=f"右膝疼痛，本周只能练 {available_days} 天",
        operations=operations,
        validation={
            "passed": True,
            "safety": safety,
            "proposed_actions": len(proposed),
            "explanation": explanation,
            "context": {
                "token_count": snapshot.token_count,
                "token_budget": token_budget,
                "dropped": [item.get("name") for item in snapshot.dropped],
                "compressed": snapshot.compressed,
                "error": context_error,
            },
            "run_id": run.id,
        },
        diff=diff,
        status="pending_approval",
        requires_approval=True,
        idempotency_key=idempotency_key,
        created_by="agent",
    )

    db.add(patch)
    db.flush()
    run.output = {
        "patch_id": patch.id,
        "diff": diff,
        "explanation_fallback": explain_result.fallback,
        "explanation_error": explain_result.error,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_cny": cost_cny,
        "budget": {
            "max_tokens_per_run": budget.max_tokens_per_run,
            "max_cost_cny_per_run": budget.max_cost_cny_per_run,
            "exceeded": budget_reason,
        },
        "stop_reason": "budget_exceeded" if budget_reason else "waiting_approval",
    }
    if budget_reason:
        run.status = "budget_exceeded"
        run.error_code = "BUDGET_EXCEEDED"

    append_event(
        db,
        plan.id,
        "patch_created",
        {
            "patch_id": patch.id,
            "base_version": patch.base_version,
            "changed_slots": diff.get("changed_slots"),
            "churn": diff.get("churn"),
            "operations": len(operations),
            "run_id": run.id,
        },
        actor_type="agent",
        actor_id="planharness",
    )
    db.commit()
    db.refresh(patch)
    return patch


def _new_version_from_actions(
    db: Session,
    plan: Plan,
    base_version: PlanVersion,
    actions: list[dict],
    *,
    patch_id: str | None = None,
    summary_prefix: str = "计划调整",
    summary_text: str | None = None,
    meals_from: "PlanVersion | None | object" = MEALS_FROM_UNSET,
    targets=None,
) -> tuple[PlanVersion, list[PlanAction]]:
    """用给定动作快照生成新版本，并把计划指针前移。

    审批提交与回滚都复用这里：两者本质都是"把某份动作内容再提交一次"，
    区别只在动作来源（补丁结果 / 历史版本）。不在此 commit，由调用方控制事务边界。
    """
    old_version = plan.current_version
    old = db.scalar(
        select(PlanVersion).where(
            PlanVersion.plan_id == plan.id, PlanVersion.version == old_version
        )
    )
    if old is not None:
        old.status = "superseded"
    new_version = PlanVersion(
        plan_id=plan.id,
        version=old_version + 1,
        parent_version=old_version,
        created_from_patch_id=patch_id,
        status="active",
        # 补丁只动动作，不重算营养目标：热量/蛋白质沿用基线版本；
        # 只有"按当前档案重算目标"这条链路会显式传 targets
        calorie_target=getattr(targets, "calories", None) or base_version.calorie_target,
        protein_target_g=getattr(targets, "protein_g", None) or base_version.protein_target_g,
        step_target=getattr(targets, "step_target", None) or base_version.step_target,
        summary=summary_text or f"{summary_prefix}（记为 v{old_version + 1}）：保留热量/蛋白质目标。",
    )
    db.add(new_version)
    db.flush()

    records: list[PlanAction] = []
    for action in actions:
        record = PlanAction(
            plan_version_id=new_version.id,
            day_index=action["day_index"],
            slot=action["slot"],
            seq=action["seq"],
            slot_key=action["slot_key"],
            action_type=action["action_type"],
            action_ref=action["action_ref"],
            sets=action.get("sets"),
            reps=action.get("reps"),
            duration_min=action.get("duration_min"),
            intensity=action.get("intensity"),
            portion_g=action.get("portion_g"),
            meta=action.get("meta", {}),
            evidence_ids=action.get("evidence_ids", []),
        )
        db.add(record)
        records.append(record)

    # 餐食不在 PlanAction 里，是单独一张表：新版本必须把旧版本的餐食复制过来，
    # 否则"改训练动作"会把当天食谱一起弄丢（用户看到"这一版没有餐食"）。
    source_version = base_version if meals_from is MEALS_FROM_UNSET else meals_from
    if source_version is not None:
        _copy_meals(db, source_version, new_version)

    plan.current_version = new_version.version
    return new_version, records


def _copy_meals(db: Session, source: PlanVersion, target: PlanVersion) -> None:
    """把一个版本的餐食复制到新版本；训练动作变了，吃什么保持不变。"""
    rows = db.scalars(
        select(MealPlan).where(MealPlan.plan_version_id == source.id)
    ).all()
    for row in rows:
        db.add(MealPlan(
            user_id=row.user_id,
            plan_version_id=target.id,
            day_index=row.day_index,
            slot=row.slot,
            title=row.title,
            items=row.items or [],
            kcal=row.kcal,
            protein_g=row.protein_g,
            fat_g=row.fat_g,
            carb_g=row.carb_g,
        ))
    db.flush()


def approve_patch(
    db: Session, user: User, patch_id: str, comment: str = ""
) -> tuple[Plan, PlanVersion]:
    """审批通过并提交补丁，返回 (计划, 新版本)。

    幂等：已 committed 的补丁重复审批直接返回当前版本；
    安全：提交前重新校验 base_version（计划未被改动）并重跑安全门。
    """
    patch = db.get(PlanPatch, patch_id)
    if patch is None:
        raise PatchValidationError(["patch not found"])
    plan = _load_plan(db, user, patch.plan_id)

    if patch.status == "committed":
        version = _current_version(db, plan)
        return plan, version
    if patch.status != "pending_approval":
        raise PatchValidationError([f"patch status is {patch.status}"])
    # base_version 不一致说明期间计划已被别的操作改过，此时提交会覆盖新内容
    if patch.base_version != plan.current_version:
        raise PatchValidationError(["stale patch: plan changed"])

    base_version = _current_version(db, plan)
    base = [_action_dict(item) for item in _actions_for_version(db, base_version.id)]
    proposed = _apply_operations(base, patch.operations)
    available_days = int(patch.diff.get("available_days", 2))
    intent = (patch.validation or {}).get("intent") or {}
    intent_kind = str(intent.get("kind") or "")
    regions = [str(item) for item in intent.get("body_parts") or []]
    if not regions and intent_kind in ("", "injury", "injury_swap"):
        # 老补丁没有 body_parts 字段：保持原来的膝痛语义
        regions = ["knee"]
    # 第二道安全门：审批时重跑，防止草稿期与审批期之间的状态漂移
    safety = _safety_check(proposed, available_days, regions=regions)
    if safety:
        raise PatchValidationError(safety)

    version, _ = _new_version_from_actions(
        db,
        plan,
        base_version,
        proposed,
        patch_id=patch.id,
        # 摘要用补丁自己的原因（"今天想休息"/"膝盖疼换动作"…），
        # 以前这里写死"膝痛补丁"，任何补丁都会显示成膝痛。
        summary_prefix=(patch.reason_text or patch.reason_code or "计划调整"),
        meals_from=base_version,
    )
    patch.status = "committed"
    patch.validation = {**patch.validation, "approved": True, "safety": safety}
    # 审批留痕：谁、何时、以什么备注放行
    db.add(
        Approval(
            patch_id=patch.id,
            actor_user_id=user.id,
            decision="approved",
            comment=comment,
        )
    )
    append_event(
        db,
        plan.id,
        "approval_granted",
        {"patch_id": patch.id, "comment": comment, "base_version": patch.base_version},
        actor_type="user",
        actor_id=user.id,
    )
    append_event(
        db,
        plan.id,
        "patch_committed",
        {
            "patch_id": patch.id,
            "version": version.version,
            "parent_version": version.parent_version,
        },
        actor_type="agent",
        actor_id="planharness",
    )
    db.commit()
    db.refresh(version)
    return plan, version


def reject_patch(db: Session, user: User, patch_id: str, comment: str = "") -> PlanPatch:
    """驳回补丁：只更新补丁状态并记录原因，计划版本保持不变。"""
    patch = db.get(PlanPatch, patch_id)
    if patch is None:
        raise PatchValidationError(["patch not found"])
    plan = _load_plan(db, user, patch.plan_id)
    if patch.status != "pending_approval":
        raise PatchValidationError([f"patch status is {patch.status}"])
    patch.status = "rejected"
    db.add(
        Approval(
            patch_id=patch.id,
            actor_user_id=user.id,
            decision="rejected",
            comment=comment,
        )
    )
    append_event(
        db,
        plan.id,
        "approval_rejected",
        {"patch_id": patch.id, "comment": comment},
        actor_type="user",
        actor_id=user.id,
    )
    db.commit()
    db.refresh(patch)
    return patch


def retarget_plan(db: Session, user: User, plan_id: str) -> PlanVersion:
    """按当前档案重算营养目标：动作/餐食不动，只换目标，仍生成新版本。

    什么时候需要：用户改了体重（88kg → 65kg），旧版本的 176g 蛋白质就变成 2.7 g/kg。
    直接改旧版本会破坏审计，所以走"新增版本"这条老路。
    """
    from app.domain.planning import calculate_targets, validate_targets
    from app.models import Profile

    plan = _load_plan(db, user, plan_id)
    profile = db.get(Profile, user.id)
    if profile is None:
        raise PatchValidationError(["先填基础档案才能重算目标"])
    targets = calculate_targets(profile)
    problems = validate_targets(profile, targets)
    if problems:
        raise PatchValidationError(problems)

    current = _current_version(db, plan)
    actions = [_action_dict(item) for item in _actions_for_version(db, current.id)]

    # 只改目标不改餐食，会让"三餐合计"和"目标"对不上（2360 → 1785 但饭还是 1995）。
    # 所以用同一套食物库按新目标重建餐食，动作保持不变。
    from app.agent.planning_agent import _build_meals, skill_profile

    nutrition = dict(plan.nutrition or {})
    nutrition["target_kcal"] = round(targets.calories, 1)
    nutrition["bmr"] = round(targets.bmr, 1)
    nutrition["tdee"] = round(targets.tdee, 1)
    macros = dict(nutrition.get("macros") or {})
    macros["protein_g"] = {"low_g": targets.protein_g, "high_g": targets.protein_g}
    macros["fat_g"] = {"low_g": targets.fat_g, "high_g": targets.fat_g}
    macros["carbs_g"] = {"low_g": targets.carbs_g, "high_g": targets.carbs_g}
    nutrition["macros"] = macros
    nutrition["basis_weight_kg"] = round(float(profile.weight_kg), 1)
    meals = _build_meals(skill_profile(profile), nutrition)

    version, _ = _new_version_from_actions(
        db, plan, current, actions, targets=targets,
        summary_text=(
            f"按当前档案（{profile.weight_kg:g}kg）重算目标：{targets.calories:.0f} kcal / "
            f"蛋白 {targets.protein_g:.0f} g，并重建三餐（动作不变）"
        ),
        meals_from=None,
    )
    from app.domain.coach import _write_meals

    _write_meals(db, user.id, version.id, meals)
    # 计划上的 nutrition 是"当前口径"，跟着一起更新
    plan.nutrition = nutrition
    append_event(
        db, plan.id, "plan_retargeted",
        {"version": version.version, "calorie_target": targets.calories,
         "protein_g": targets.protein_g, "basis_weight_kg": profile.weight_kg},
        actor_type="system", actor_id="planharness",
    )
    db.commit()
    db.refresh(version)
    return version


def rollback_plan(db: Session, user: User, plan_id: str, target_version: int) -> PlanVersion:
    """回滚：以历史版本 content 为基础提交一个新版本（而不是把指针改回去）。

    这样版本链只增不改，任何时刻"当前版本之前发生过什么"都能完整追溯。
    """
    plan = _load_plan(db, user, plan_id)
    current = _current_version(db, plan)
    if target_version == current.version:
        raise PatchValidationError(["already on target version"])
    target = db.scalar(
        select(PlanVersion).where(
            PlanVersion.plan_id == plan.id, PlanVersion.version == target_version
        )
    )
    if target is None:
        raise PatchValidationError(["target version not found"])
    # 复制目标版本的动作快照作为新版本内容
    actions = [_action_dict(item) for item in _actions_for_version(db, target.id)]
    version, _ = _new_version_from_actions(
        db,
        plan,
        current,
        actions,
        summary_prefix=f"回滚到 v{target_version}",
        meals_from=target,
        # 版本号是审计锚点，不是给用户记的：说清"内容来自 v6、现在记为 v8"
        summary_text=(
            f"内容已回滚到 v{target_version}，记为 v{current.version + 1}"
            f"（热量与蛋白质目标沿用 v{target_version}）"
        ),
    )
    append_event(
        db,
        plan.id,
        "rollback_committed",
        {
            "from_version": current.version,
            "target_version": target_version,
            "new_version": version.version,
        },
        actor_type="system",
        actor_id="planharness",
    )
    db.commit()
    db.refresh(version)
    return version
