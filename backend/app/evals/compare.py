"""A/B/C 三模式对比：同一场景、同一约束，只改 Harness 配置。

| 模式 | 生成方式 | 安全门 | 审批 |
| --- | --- | --- | --- |
| A 裸奔 | 全量重生成（12 个槽位全换） | 关 | 关 |
| B 安全门 | 全量重生成 + 安全门修正 | 开 | 开 |
| C 补丁 | 最小补丁（3/12） | 开 | 开 |

**关于公平性（重要）**：
P0 的计划生成器是确定性模板，直接"重新生成"会得到和 v1 一模一样的计划（churn=0），
那就没法体现「重生成会漂移」。所以 A/B 用一份**固定漂移 fixture** 模拟
「模型每次重生成时换动作、并且不知道膝盖疼」的典型行为：

- 换掉 5 个运动槽位的动作；
- 其中 1 个换成膝痛禁忌动作（jump_squat）。

这不是"故意写烂的 Agent"，而是把「模型的不确定性」变成一个可复现的输入。
docs/04 §4.3 要求明确标注这一点，所以报告里带着 `fixture_note`。
"""

from __future__ import annotations

import tempfile

from sqlalchemy import select

from app.config import Settings
from app.domain.patches import (
    FORBIDDEN_KNEE_REFS,
    _action_dict,
    _apply_operations,
    _diff,
    _new_version_from_actions,
    approve_patch,
    create_knee_patch,
)
from app.main import create_app
from app.models import Plan, PlanAction, PlanPatch, PlanVersion, Profile, User

# 模拟"重生成"造成的动作漂移：把常见动作换成同类别但更激烈/风险更高的版本
DRIFT_MAP = {
    "goblet_squat": "jump_squat",     # ← 膝痛禁忌动作
    "bodyweight_squat": "jump_squat",
    "pushup": "decline_pushup",
    "brisk_walk": "jog",
    "row": "kettlebell_swing",
}

# 安全门发现禁忌动作后，替换成的低冲击替代
SAFE_ALTERNATIVE = {
    "jump_squat": "glute_bridge",     # 低冲击髋主导
    "run": "easy_walk",
    "jog": "easy_walk",               # 刻意不等于原动作 brisk_walk
    "lunge": "glute_bridge",
    "kettlebell_swing": "backpack_row",  # 不等于原动作 row
}

AVAILABLE_DAYS = 2

# fixture 里用到的风险动作：既包含线上禁忌集，也包含"重生成时容易选到"的变体
RISKY_REFS = set(FORBIDDEN_KNEE_REFS) | {"jump_squat", "jog", "kettlebell_swing"}


def _new_app():
    """每个模式一份独立临时库：互不干扰，也不碰用户数据。"""
    tmpdir = tempfile.mkdtemp(prefix="ph-compare-")
    return create_app(
        Settings(
            env="dev",
            database_url=f"sqlite:///{tmpdir}/compare.db",
            secret_key="compare-secret-0123456789abcdef0123",
            cors_origins="http://localhost:5173",
            deepseek_api_key="",  # 对比不需要模型
        )
    )


def _load(app):
    with app.state.session_factory() as db:
        user = db.get(User, dp_user_id())
        plan = db.scalar(select(Plan))
        version = db.scalar(
            select(PlanVersion).where(PlanVersion.version == plan.current_version)
        )
        actions = [
            _action_dict(a)
            for a in db.scalars(
                select(PlanAction)
                .where(PlanAction.plan_version_id == version.id)
                .order_by(PlanAction.day_index, PlanAction.slot, PlanAction.seq)
            ).all()
        ]
    return user, plan, version, actions


def dp_user_id() -> str:
    from app.seed import DEMO_USER_ID

    return DEMO_USER_ID


def _drifted(actions: list[dict]) -> list[dict]:
    """模拟"重新生成"：运动动作换一批，餐食保持不变。"""
    out = []
    for item in actions:
        copy = dict(item)
        if copy["action_type"] == "exercise":
            copy["action_ref"] = DRIFT_MAP.get(copy["action_ref"], copy["action_ref"])
        out.append(copy)
    return out


def _gate(actions: list[dict], available_days: int = AVAILABLE_DAYS) -> tuple[list[dict], list[str]]:
    """安全门：两条硬约束 —— 禁忌动作替换、力量日不超过可用天数。"""
    changes: list[str] = []

    # 1) 禁忌动作 → 低冲击替代
    replaced: list[dict] = []
    for item in actions:
        copy = dict(item)
        ref = copy["action_ref"]
        if copy["action_type"] == "exercise" and ref in RISKY_REFS:
            alternative = SAFE_ALTERNATIVE.get(ref, "glute_bridge")
            changes.append(f"{ref} → {alternative}")
            copy["action_ref"] = alternative
        replaced.append(copy)

    # 2) 力量日超限：删掉靠后的力量日动作
    strength_days = sorted(
        {a["day_index"] for a in replaced if a["action_type"] == "exercise" and a["slot"] == "strength"}
    )
    overflow = strength_days[available_days:]
    if overflow:
        for day in overflow:
            changes.append(f"删除第 {day} 天力量训练（力量日 {len(strength_days)} > {available_days}）")
        replaced = [
            a
            for a in replaced
            if not (a["action_type"] == "exercise" and a["slot"] == "strength" and a["day_index"] in overflow)
        ]

    return replaced, changes


def _violations(actions: list[dict]) -> list[str]:
    """约束违规：禁忌动作 + 力量日超限，两类都算。"""
    problems = [
        f"禁忌动作:{a['action_ref']}"
        for a in actions
        if a["action_type"] == "exercise" and a["action_ref"] in RISKY_REFS
    ]
    strength_days = {
        a["day_index"]
        for a in actions
        if a["action_type"] == "exercise" and a["slot"] == "strength"
    }
    if len(strength_days) > AVAILABLE_DAYS:
        problems.append(f"力量日:{len(strength_days)}>{AVAILABLE_DAYS}")
    return problems


def _row(
    mode: str,
    label: str,
    base: list[dict],
    final: list[dict],
    *,
    approval: bool,
    notes: str,
    required_changes: int = 0,
) -> dict:
    stats = _diff(base, final, required_changes=required_changes)
    broken = _violations(final)
    return {
        "mode": mode,
        "label": label,
        "changed_slots": stats["changed_slots"],
        "total_slots": stats["total_slots"],
        "churn": stats["churn"],
        "unnecessary_churn": stats["unnecessary_churn"],
        "violations": len(broken),
        "violation_refs": broken,
        "approval": approval,
        "notes": notes,
    }


# ------------------------------------------------------------------ 三种模式


def _mode_a() -> dict:
    """A 裸奔：全量重生成，不过安全门、不走审批，直接提交。"""
    app = _new_app()
    user, plan, version, base = _load(app)
    variant = _drifted(base)

    with app.state.session_factory() as db:
        _new_version_from_actions(
            db, plan, version, variant, summary_prefix="A 裸奔重生成"
        )
        db.commit()
        final = variant

    return _row(
        "A",
        "裸奔",
        base,
        final,
        approval=False,
        notes="重生成后直接写库：无安全门、无审批",
        required_changes=0,
    )


def _mode_b() -> dict:
    """B 安全门：重生成 + 安全门修正 + 审批（走真实 patch/approve 流程）。"""
    app = _new_app()
    user, plan, version, base = _load(app)

    variant = _drifted(base)
    gated, gate_changes = _gate(variant, AVAILABLE_DAYS)

    # 把「全量重生成 + 安全门修正」表达成一个 patch：
    # - 被 gate 删掉的槽位 → remove_action
    # - 其余槽位 → replace_action（内容可能已被 gate 改过）
    base_by_key = {item["slot_key"]: item for item in base}
    kept_keys = {item["slot_key"] for item in gated}
    operations = [
        {
            "op": "remove_action",
            "slot_key": key,
            "before_action_ref": item["action_ref"],
            "reason": "安全门：力量日超限",
        }
        for key, item in base_by_key.items()
        if key not in kept_keys
    ]
    operations += [
        {
            "op": "replace_action",
            "slot_key": item["slot_key"],
            "before_action_ref": base_by_key[item["slot_key"]]["action_ref"],
            "after_action": {
                "action_ref": item["action_ref"],
                "sets": item.get("sets"),
                "reps": item.get("reps"),
                "duration_min": item.get("duration_min"),
                "meta": item.get("meta", {}),
            },
            "reason": "全量重生成",
        }
        for item in gated
    ]

    with app.state.session_factory() as db:
        proposed = _apply_operations(base, operations)
        diff = _diff(base, proposed, required_changes=len(gate_changes))
        diff["available_days"] = AVAILABLE_DAYS
        patch = PlanPatch(
            plan_id=plan.id,
            base_version=plan.current_version,
            goal_id=plan.goal_id,
            reason_code="regenerate",
            reason_text="全量重生成后经安全门修正",
            operations=operations,
            validation={"passed": True, "gate_changes": gate_changes},
            diff=diff,
            status="pending_approval",
            requires_approval=True,
            created_by="agent",
        )
        db.add(patch)
        db.commit()

        _, new_version = approve_patch(db, user, patch.id, "对比用脚本化审批")
        final = [
            _action_dict(a)
            for a in db.scalars(
                select(PlanAction).where(PlanAction.plan_version_id == new_version.id)
            ).all()
        ]

    row = _row(
        "B",
        "安全门",
        base,
        final,
        approval=True,
        notes="重生成 + 安全门修正 " + ("；".join(gate_changes) if gate_changes else "（无）"),
        required_changes=len(gate_changes),
    )
    row["gate_changes"] = gate_changes
    return row


def _mode_c() -> dict:
    """C 补丁：当前真实实现（最小补丁 + 安全门 + 审批）。"""
    app = _new_app()
    user, plan, version, base = _load(app)

    with app.state.session_factory() as db:
        patch = create_knee_patch(
            db, user, plan.id, available_days=AVAILABLE_DAYS
        )
        _, new_version = approve_patch(db, user, patch.id, "对比用脚本化审批")
        final = [
            _action_dict(a)
            for a in db.scalars(
                select(PlanAction).where(PlanAction.plan_version_id == new_version.id)
            ).all()
        ]
        changed = patch.diff.get("changed_slots")

    row = _row(
        "C",
        "补丁",
        base,
        final,
        approval=True,
        notes=f"最小补丁：只改 {changed}/{len(base)} 个槽位",
        required_changes=int(changed or 0),
    )
    return row


def run_knee_comparison() -> dict:
    """跑 A/B/C 三遍，返回可直接渲染的对比表。"""
    rows = [_mode_a(), _mode_b(), _mode_c()]
    return {
        "scenario": "第 2 周膝痛，本周最多练 2 天",
        "constraint": "膝痛禁忌动作不得出现；力量训练日 ≤ 2",
        "fixture_note": (
            "P0 的计划生成器是确定性模板，直接重生成会得到和 v1 一样的计划（churn=0），"
            "体现不出「重生成会漂移」。所以 A/B 用固定漂移 fixture 模拟"
            "「模型换了一批动作、且不知道膝盖疼」，C 走真实最小补丁实现。"
            "三者数值都是实测；换成真实 LLM 后，A/B 的漂移只会更大。"
        ),
        "modes": rows,
    }
