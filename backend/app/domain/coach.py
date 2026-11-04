"""教练计划：生成草案 → 多轮修改 → 确认落版本。

和 `patches.py` 的分工：
- `patches.py` 改的是**已生效**的计划（最小补丁，append-only 新版本）；
- 这里生成的是**整份新计划**，所以先存 `plan_drafts`，用户确认后才写版本。

两者共用同一套"确定性安全门 + 审计事件 + append-only 版本"的约束，
区别只在输入：一个是一句话改动，一个是完整的画像 + 目标。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.planning_agent import PlanningAgent, PlanProposal, skill_profile
from app.domain.draft_edits import apply_explicit_edits, has_explicit_edits
from app.domain.revisions import apply_feedback
from app.audit import append_event
from app.domain.patches import PatchValidationError, _action_dict, _load_plan
from app.models import (
    Goal,
    MealPlan,
    Plan,
    PlanAction,
    PlanDraft,
    PlanVersion,
    Profile,
    User,
    new_id,
)
from app.skill.bridge import SkillBridge, get_bridge

MAX_ROUNDS = 3

# 餐次要按生理顺序展示，而不是按字符串排序（否则会出现早/晚/午/加餐）
MEAL_ORDER = {"breakfast": 1, "lunch": 2, "dinner": 3, "snack": 4}

GOAL_TO_SKILL = {
    "fat_loss": "减脂",
    "muscle_gain": "增肌",
    "cardio": "心肺",
    "health": "综合健康",
}


def coach_profile(profile: Profile) -> dict:
    """画像 → skill 入参（供路由/测试直接复用）。"""
    return skill_profile(profile)


def _skill_payload(profile: Profile) -> dict:
    return skill_profile(profile)


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

def create_draft(
    db: Session,
    user: User,
    instruction: str,
    *,
    parent_draft_id: str | None = None,
    bridge: SkillBridge | None = None,
    client=None,
) -> PlanDraft:
    """生成一份草案。不写版本，只写 plan_drafts。"""
    profile = db.get(Profile, user.id)
    if profile is None:
        raise PatchValidationError(["profile not found: 先填基础信息再生成计划"])

    feedback: list[str] = []
    round_no = 1
    cursor = parent_draft_id
    while cursor:
        parent = db.get(PlanDraft, cursor)
        if parent is None:
            break
        round_no += 1
        feedback = [str(item) for item in (parent.feedback or [])] + feedback
        cursor = None  # 只沿一层：反馈已在 parent 里累加

    if round_no > MAX_ROUNDS + 1:
        raise PatchValidationError([f"已经改过 {MAX_ROUNDS} 轮了，请重新描述你的目标"])

    agent = PlanningAgent(bridge or get_bridge(), client=client)
    proposal: PlanProposal = agent.propose(profile, instruction or "", previous_feedback=feedback)

    # 用户在第一句话里就可能写了具体编辑（"加个练背动作"），同样要落到计划上
    if proposal.payload.get("status") == "ready" and (instruction or "").strip():
        edited, edit_changes = apply_explicit_edits(proposal.payload, instruction)
        if edit_changes:
            proposal.payload = edited
            proposal.trace = list(proposal.trace) + [{
                "node": "apply_explicit_edits",
                "status": "applied",
                "detail": {"changes": edit_changes[:10]},
            }]

    plan = db.scalar(
        select(Plan).where(Plan.user_id == user.id, Plan.status == "active")
    )
    draft = PlanDraft(
        user_id=user.id,
        plan_id=plan.id if plan is not None else None,
        base_version=plan.current_version if plan is not None else None,
        instruction=instruction or "",
        feedback=feedback,
        payload=proposal.payload,
        status=proposal.status,
        round=round_no,
        question=str(proposal.payload.get("question") or ""),
        trace=proposal.trace,
    )
    db.add(draft)
    db.flush()
    append_event(
        db,
        draft.plan_id or f"draft:{draft.id}",
        "plan_draft_created",
        {
            "draft_id": draft.id,
            "status": draft.status,
            "round": draft.round,
            "source": proposal.payload.get("meta", {}).get("skill_available"),
            "model_fallback": proposal.fallback,
        },
        actor_type="agent",
        actor_id="coach",
    )
    db.commit()
    db.refresh(draft)
    return draft


def add_feedback(
    db: Session,
    user: User,
    draft_id: str,
    text: str,
    *,
    bridge: SkillBridge | None = None,
    client=None,
) -> PlanDraft:
    """「这里再改改」：把反馈累加到 instruction 后重新生成一份草案。"""
    parent = load_draft(db, user, draft_id)
    if parent.status == "confirmed":
        raise PatchValidationError(["draft already confirmed"])
    if parent.round > MAX_ROUNDS:
        raise PatchValidationError([f"已经改过 {MAX_ROUNDS} 轮了，请重新描述你的目标"])

    instruction = parent.instruction
    if text:
        instruction = f"{instruction}\n修改意见：{text}" if instruction else f"修改意见：{text}"

    profile = db.get(Profile, user.id)
    agent = PlanningAgent(bridge or get_bridge(), client=client)
    proposal = agent.propose(profile, instruction or "",
                             previous_feedback=[str(item) for item in (parent.feedback or [])])

    # 模型有时会反问"要不要这么做"，但这句反馈其实是可执行的（换食材/加动作/换动作）。
    # 这种问题我们自己能答：绕开模型，用确定性编排生成，再应用具体编辑。
    if proposal.payload.get("status") != "ready" and has_explicit_edits(text or ""):
        retry = agent.propose_deterministic(profile, instruction or "")
        if retry.payload.get("status") == "ready":
            proposal = retry
            proposal.trace = list(proposal.trace) + [{
                "node": "deterministic_retry",
                "status": "ready",
                "detail": {"reason": "反馈里有可执行的编辑，不需要向用户反问"},
            }]

    # 关键：反馈必须落到计划内容上，而不是只改解释文案。
    # 顺序：具体编辑（换食材/加动作/删动作）→ 模糊变换（换一批/加强度/避膝）。
    if proposal.payload.get("status") == "ready" and text:
        revised, changes = apply_explicit_edits(proposal.payload, text)
        if not changes:
            revised, changes = apply_feedback(proposal.payload, text)
            node = "apply_feedback"
        else:
            node = "apply_explicit_edits"
        proposal.trace = list(proposal.trace) + [{
            "node": node,
            "status": "applied" if changes else "no_match",
            "detail": {"changes": changes[:10]},
        }]
        if changes:
            proposal.payload = revised
        else:
            # 没改就别让摘要替你吹：模型经常在文案里说"已经换了"，
            # 但结构没动，这是最糟糕的一种"看起来做了"。
            proposal.payload = dict(proposal.payload)
            proposal.payload["summary"] = (
                str(proposal.payload.get("summary") or "") + "（本轮没有改动计划内容）"
            )
            proposal.payload.setdefault("cautions", []).append(
                "这句话没匹配到可以自动执行的调整，计划内容保持不变。"
                "可以试试更具体的说法：「把红薯换成土豆」「新增一个练背动作」「去掉臀桥」"
                "「动作别重复」「膝盖少蹲一点」「加强度」。"
            )

    child = PlanDraft(
        user_id=user.id,
        plan_id=parent.plan_id,
        base_version=parent.base_version,
        instruction=instruction,
        feedback=[str(item) for item in (parent.feedback or [])] + ([text] if text else []),
        payload=proposal.payload,
        status=proposal.status,
        round=parent.round + 1,
        question=str(proposal.payload.get("question") or ""),
        trace=proposal.trace,
    )
    db.add(child)
    parent.status = "superseded"
    db.flush()
    append_event(
        db,
        child.plan_id or f"draft:{child.id}",
        "plan_draft_revised",
        {"draft_id": child.id, "parent": parent.id, "round": child.round,
         "feedback": (text or "")[:200]},
        actor_type="user",
        actor_id=user.id,
    )
    db.commit()
    db.refresh(child)
    return child


# ---------------------------------------------------------------------------
# 确认 / 拒绝
# ---------------------------------------------------------------------------

def confirm_draft(
    db: Session, user: User, draft_id: str, comment: str = ""
) -> tuple[Plan, PlanVersion]:
    """确认草案 → 落成新版本（第一次则创建计划）+ 餐食明细 + 审计。"""
    draft = load_draft(db, user, draft_id)
    if draft.status != "ready":
        raise PatchValidationError([f"draft status is {draft.status}, cannot confirm"])
    payload = draft.payload or {}
    days = payload.get("days") or []
    if not days:
        raise PatchValidationError(["draft has no days to commit"])

    plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    nutrition = payload.get("nutrition") or {}
    program = payload.get("program") or {}
    if plan is None:
        goal = _ensure_goal(db, user, payload)
        plan = Plan(
            user_id=user.id,
            goal_id=goal.id,
            status="active",
            current_version=0,
            source="coach",
            nutrition=nutrition,
            program=program,
        )
        db.add(plan)
        db.flush()

    calorie = float(nutrition.get("target_kcal") or 0) or 1800.0
    protein_band = (nutrition.get("macros") or {}).get("protein_g") or {}
    protein = float(protein_band.get("high_g") or protein_band.get("low_g") or 0) or 90.0
    steps = int(program.get("step_target") or 8000)

    actions = _flatten_actions(days)
    version = _commit_actions(db, plan, actions, calorie, protein, steps, payload, comment)
    _write_meals(db, user.id, version.id, payload.get("meals") or [])

    plan.source = "coach"
    plan.nutrition = nutrition
    plan.program = program
    draft.status = "confirmed"
    db.flush()

    append_event(
        db,
        plan.id,
        "plan_confirmed",
        {"draft_id": draft.id, "version": version.version, "comment": comment,
         "calorie_target": calorie, "days": len(days)},
        actor_type="user",
        actor_id=user.id,
    )
    db.commit()
    db.refresh(version)
    return plan, version


def reject_draft(db: Session, user: User, draft_id: str, reason: str = "") -> PlanDraft:
    draft = load_draft(db, user, draft_id)
    if draft.status == "confirmed":
        raise PatchValidationError(["draft already confirmed"])
    draft.status = "rejected"
    db.flush()
    if draft.plan_id:
        append_event(
            db, draft.plan_id, "plan_draft_rejected",
            {"draft_id": draft.id, "reason": reason[:200]},
            actor_type="user", actor_id=user.id,
        )
    db.commit()
    db.refresh(draft)
    return draft


def load_draft(db: Session, user: User, draft_id: str) -> PlanDraft:
    draft = db.get(PlanDraft, draft_id)
    if draft is None or draft.user_id != user.id:
        raise PatchValidationError(["draft not found"])
    return draft


def draft_payload(draft: PlanDraft) -> dict:
    """前端要的形状：草案正文 + 轮次 + 状态 + 生成链路信息。"""
    payload = draft.payload or {}
    return {
        "draft_id": draft.id,
        "status": draft.status,
        "round": draft.round,
        "instruction": draft.instruction,
        "feedback": draft.feedback or [],
        "question": draft.question,
        "summary": payload.get("summary", ""),
        "highlights": payload.get("highlights", []),
        "cautions": payload.get("cautions", []),
        "warnings": payload.get("warnings", []),
        "program": payload.get("program", {}),
        "prescription": payload.get("prescription", {}),
        "nutrition": payload.get("nutrition", {}),
        "days": payload.get("days", []),
        "meals": payload.get("meals", []),
        "revisions": payload.get("revisions", []),
        "meta": payload.get("meta", {}),
        "trace": draft.trace or [],
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
    }


# ---------------------------------------------------------------------------
# 落库细节
# ---------------------------------------------------------------------------

def _ensure_goal(db: Session, user: User, payload: dict) -> Goal:
    goal_type = str((payload.get("program") or {}).get("goal") or "")
    reverse = {value: key for key, value in GOAL_TO_SKILL.items()}
    mapped = reverse.get(goal_type, "health")
    goal = db.scalar(
        select(Goal).where(Goal.user_id == user.id, Goal.status == "active")
    )
    if goal is None:
        goal = Goal(user_id=user.id, type=mapped, status="active", source="user",
                    raw_text=str((payload.get("summary") or "")[:200]))
        db.add(goal)
        db.flush()
    else:
        goal.type = mapped
    return goal


def _flatten_actions(days: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for day in days:
        day_index = int(day.get("day_index") or 1)
        for action in day.get("actions") or []:
            slot = str(action.get("slot") or "strength")
            seq = int(action.get("seq") or 1)
            rows.append({
                "day_index": day_index,
                "slot": slot,
                "seq": seq,
                "slot_key": f"{day_index}:{slot}:{seq}",
                "action_type": action.get("action_type") or "exercise",
                "action_ref": action.get("action_ref") or "unknown",
                "sets": action.get("sets"),
                "reps": action.get("reps"),
                "duration_min": action.get("duration_min"),
                "intensity": action.get("intensity"),
                "portion_g": action.get("portion_g"),
                "meta": action.get("meta") or {},
            })
    return rows


def _commit_actions(db: Session, plan: Plan, actions: list[dict], calorie: float,
                    protein: float, steps: int, payload: dict, comment: str) -> PlanVersion:
    previous = db.scalar(
        select(PlanVersion).where(
            PlanVersion.plan_id == plan.id, PlanVersion.version == plan.current_version
        )
    ) if plan.current_version else None
    if previous is not None:
        previous.status = "superseded"

    version = PlanVersion(
        plan_id=plan.id,
        version=plan.current_version + 1,
        parent_version=plan.current_version or None,
        status="active",
        calorie_target=calorie,
        protein_target_g=protein,
        step_target=steps,
        summary=str(payload.get("summary") or "")[:500],
    )
    db.add(version)
    db.flush()

    for action in actions:
        db.add(PlanAction(
            plan_version_id=version.id,
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
            kcal=None,
            meta=action.get("meta") or {},
        ))
    plan.current_version = version.version
    db.flush()
    return version


def _write_meals(db: Session, user_id: str, version_id: str, meals: list[dict]) -> None:
    for meal in meals:
        db.add(MealPlan(
            user_id=user_id,
            plan_version_id=version_id,
            day_index=int(meal.get("day_index") or 1),
            slot=str(meal.get("slot") or "meal"),
            title=str(meal.get("title") or ""),
            items=meal.get("items") or [],
            kcal=int(meal.get("kcal") or 0),
            protein_g=float(meal.get("protein_g") or 0),
            fat_g=float(meal.get("fat_g") or 0),
            carb_g=float(meal.get("carb_g") or 0),
        ))
    db.flush()


def today_payload(db: Session, plan: Plan, version: PlanVersion, weekday: int) -> dict:
    """今天该练什么、吃什么。weekday 用 1=周一 … 7=周日，和 day_index 同一编号。"""
    actions = db.scalars(
        select(PlanAction)
        .where(PlanAction.plan_version_id == version.id, PlanAction.day_index == weekday)
        .order_by(PlanAction.slot, PlanAction.seq)
    ).all()
    meals = db.scalars(
        select(MealPlan)
        .where(MealPlan.plan_version_id == version.id, MealPlan.day_index == weekday)
    ).all()
    meals = sorted(meals, key=lambda item: MEAL_ORDER.get(item.slot, 9))
    return {
        "weekday": weekday,
        "date": date.today().isoformat(),
        "actions": [ActionOut(item) for item in actions],
        "meals": [MealPlanOut(item) for item in meals],
    }


# 路由层用到的 pydantic 序列化包装（避免循环导入：在这里延迟取）
def _out_models():
    from app.schemas import ActionOut as _ActionOut, MealPlanOut as _MealPlanOut
    return _ActionOut, _MealPlanOut


def ActionOut(item) -> dict:  # noqa: N802 - 保持与 schemas 同名，便于阅读
    action_out, _ = _out_models()
    return action_out.model_validate(item).model_dump()


def MealPlanOut(item) -> dict:  # noqa: N802
    _, meal_out = _out_models()
    return meal_out.model_validate(item).model_dump()
