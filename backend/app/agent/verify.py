"""提交后的独立验证器：用「环境事实」核对一次提交是否真的成立。

为什么需要它：

- 模型（或 Agent 的自我陈述）会说「已完成」，但那不是证据；
- 真正的完成标准是**数据库里的事实**：版本指针、审批记录、动作内容、churn。

三条硬规矩：

1. **不调用 LLM**，纯 SQL + 纯函数；
2. **不信任传入的状态**，只查数据库；
3. 失败要让调用方把 `AgentRun` 标成 failed，不允许"模型自称完成"。

对应 docs/14 §2.4「独立验证器」的最小落地版。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.patches import FORBIDDEN_KNEE_REFS, _action_dict, _diff
from app.models import Approval, Plan, PlanAction, PlanPatch, PlanVersion


@dataclass(frozen=True)
class Check:
    """一条断言的结果。name 用 snake_case，便于日志统计。"""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    checks: list[Check] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
        }

    def failed_names(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]


def verify_commit(
    db: Session,
    *,
    plan_id: str,
    patch_id: str,
    expected_version: int,
) -> VerificationResult:
    """核对「补丁已提交成新版本」这件事在数据库里是否真的成立。"""
    checks: list[Check] = []

    patch = db.get(PlanPatch, patch_id)
    plan = db.get(Plan, plan_id)
    version = db.scalar(
        select(PlanVersion).where(
            PlanVersion.plan_id == plan_id,
            PlanVersion.version == expected_version,
        )
    )

    # 1) 补丁必须真的 committed
    checks.append(
        Check(
            "patch_committed",
            patch is not None and patch.status == "committed",
            f"status={getattr(patch, 'status', None)}",
        )
    )

    # 2) 必须有审批记录（人工把关的痕迹）
    approval = db.scalar(
        select(Approval).where(
            Approval.patch_id == patch_id, Approval.decision == "approved"
        )
    )
    checks.append(
        Check("approval_recorded", approval is not None, "approvals 表里应有 approved 记录")
    )

    # 3) 计划指针必须指向这个版本
    checks.append(
        Check(
            "current_version_matches",
            plan is not None and plan.current_version == expected_version,
            f"plan.current_version={getattr(plan, 'current_version', None)}",
        )
    )

    # 4) 新版本必须是 active
    checks.append(
        Check(
            "version_active",
            version is not None and version.status == "active",
            f"version.status={getattr(version, 'status', None)}",
        )
    )

    if version is None:
        return _finish(checks)

    # 5) 上一版必须被标成 superseded（历史不可变，但指针要移走）
    parent = None
    if version.parent_version is not None:
        parent = db.scalar(
            select(PlanVersion).where(
                PlanVersion.plan_id == plan_id,
                PlanVersion.version == version.parent_version,
            )
        )
    checks.append(
        Check(
            "previous_superseded",
            version.parent_version is None
            or (parent is not None and parent.status == "superseded"),
            f"parent_version={version.parent_version}",
        )
    )

    # 6) 已提交动作里不能有膝痛禁忌动作（安全门的事后复核）
    #    只有"确实在处理膝盖"的补丁才查这条：休息/减天数的补丁保留其它天的深蹲是合理的，
    #    否则验证器会把正确结果判成失败（实测 E10 就是这样误报的）。
    actions = list(
        db.scalars(
            select(PlanAction)
            .where(PlanAction.plan_version_id == version.id)
            .order_by(PlanAction.day_index, PlanAction.slot, PlanAction.seq)
        ).all()
    )
    intent = ((patch.validation or {}).get("intent") or {}) if patch is not None else {}
    intent_parts = {str(item) for item in intent.get("body_parts") or []}
    check_knee = "knee" in intent_parts or (
        not intent_parts and str(getattr(patch, "reason_code", "")) in
        ("injury", "injury_swap", "knee_patch")
    )
    if check_knee:
        forbidden = [
            a.action_ref
            for a in actions
            if a.action_type == "exercise" and a.action_ref in FORBIDDEN_KNEE_REFS
        ]
        checks.append(
            Check("no_forbidden_actions", not forbidden, f"命中={forbidden}")
        )
    else:
        checks.append(
            Check("no_forbidden_actions", True,
                  f"本补丁未涉及膝盖（{'/'.join(sorted(intent_parts)) or '无部位'}），跳过")
        )

    # 7) churn 必须与补丁当时记录的一致（拿父版本动作重算一遍）
    if patch is not None and patch.diff and parent is not None:
        base = [
            _action_dict(a)
            for a in db.scalars(
                select(PlanAction).where(PlanAction.plan_version_id == parent.id)
            ).all()
        ]
        proposed = [_action_dict(a) for a in actions]
        recomputed = _diff(base, proposed, required_changes=len(patch.operations or []))
        recorded = patch.diff
        same = (
            recomputed["changed_slots"] == recorded.get("changed_slots")
            and recomputed["total_slots"] == recorded.get("total_slots")
        )
        checks.append(
            Check(
                "churn_matches_recorded_diff",
                same,
                f"重算={recomputed['changed_slots']}/{recomputed['total_slots']} "
                f"记录={recorded.get('changed_slots')}/{recorded.get('total_slots')}",
            )
        )

    # 8) 力量训练日不能超过补丁声明的可用天数
    available_days = (patch.diff or {}).get("available_days") if patch is not None else None
    if available_days is not None:
        strength_days = {
            a.day_index
            for a in actions
            if a.action_type == "exercise" and a.slot == "strength"
        }
        checks.append(
            Check(
                "strength_days_within_limit",
                len(strength_days) <= int(available_days),
                f"力量日={len(strength_days)} 上限={available_days}",
            )
        )

    return _finish(checks)


def _finish(checks: list[Check]) -> VerificationResult:
    return VerificationResult(passed=all(c.passed for c in checks), checks=checks)
