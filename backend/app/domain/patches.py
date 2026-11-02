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

from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.fallback import fallback_explanation
from app.llm.prompts import build_explanation_payload
from app.models import Approval, Plan, PlanAction, PlanPatch, PlanVersion, User

# 膝痛场景的确定性禁忌动作集：出现即拒绝，不交给模型判断
FORBIDDEN_KNEE_REFS = {"squat", "goblet_squat", "run", "jump", "lunge"}


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


def _safety_check(proposed: list[dict], available_days: int) -> list[str]:
    """确定性安全门（第一道闸口）：膝痛禁忌动作 + 训练日不超过可用天数。

    返回错误列表，空表示通过。审批时会再跑一次，防止两次审批之间计划被改动。
    """
    errors: list[str] = []
    for item in proposed:
        if item["action_type"] != "exercise":
            continue
        if item["action_ref"] in FORBIDDEN_KNEE_REFS:
            errors.append(f"膝痛禁忌动作: {item['action_ref']}")
    strength_days = {
        item["day_index"]
        for item in proposed
        if item["action_type"] == "exercise" and item["slot"] == "strength"
    }
    if len(strength_days) > available_days:
        errors.append(f"力量训练日 {len(strength_days)} 超过可用 {available_days} 天")
    return errors


def create_knee_patch(
    db: Session,
    user: User,
    plan_id: str,
    *,
    available_days: int = 2,
    idempotency_key: str | None = None,
    explainer: Callable[[dict, list[dict], dict], str] | None = None,
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
        raise PatchValidationError(["当前计划没有可演示的 squat/brisk_walk/row 槽位"])

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
    reason_text = f"右膝疼痛，本周只能练 {available_days} 天",
    payload = build_explanation_payload(
        reason_text=reason_text,
        base_version= plan.current_version,
        operations=operations,
        diff=diff,
        safety=safety,
    )

    if explainer is not None:
        try:
            explanation =  explainer(payload, operations, diff)
        except Exception:
            explanation = fallback_explanation(operations, diff)
    else:
        explanation = fallback_explanation(operations, diff)

    patch = PlanPatch(
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
        },
        diff=diff,
        status="pending_approval",
        requires_approval=True,
        idempotency_key=idempotency_key,
        created_by="agent",
    )
    db.add(patch)
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
        # 补丁只动动作，不重算营养目标：热量/蛋白质沿用基线版本
        calorie_target=base_version.calorie_target,
        protein_target_g=base_version.protein_target_g,
        step_target=base_version.step_target,
        summary=f"{summary_prefix} v{old_version + 1}：保留热量/蛋白质目标。",
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

    plan.current_version = new_version.version
    return new_version, records


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
    # 第二道安全门：审批时重跑，防止草稿期与审批期之间的状态漂移
    safety = _safety_check(proposed, available_days)
    if safety:
        raise PatchValidationError(safety)

    version, _ = _new_version_from_actions(
        db, plan, base_version, proposed, patch_id=patch.id, summary_prefix="膝痛补丁"
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
    db.commit()
    db.refresh(version)
    return plan, version


def reject_patch(db: Session, user: User, patch_id: str, comment: str = "") -> PlanPatch:
    """驳回补丁：只更新补丁状态并记录原因，计划版本保持不变。"""
    patch = db.get(PlanPatch, patch_id)
    if patch is None:
        raise PatchValidationError(["patch not found"])
    _load_plan(db, user, patch.plan_id)
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
    db.commit()
    db.refresh(patch)
    return patch


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
        db, plan, current, actions, summary_prefix=f"回滚到 v{target_version}"
    )
    db.commit()
    db.refresh(version)
    return version
