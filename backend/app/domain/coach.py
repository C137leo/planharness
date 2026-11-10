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

import inspect

from app.agent.planning_agent import PlanningAgent, PlanProposal, skill_profile
from app.domain.draft_edits import (
    apply_explicit_edits,
    extract_allergy_terms,
    extract_avoided_foods,
    has_explicit_edits,
)
from app.domain.memory import load_memory_context, memory_instruction, recent_training_state
from app.domain.day_plan import allowed_equipment, pick_alternative
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


GOAL_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fat_loss", ("减脂", "减重", "瘦", "掉秤", "降体脂")),
    ("muscle_gain", ("增肌", "增重", "长肌肉")),
    ("cardio", ("心肺", "耐力", "有氧能力")),
    ("health", ("体态", "综合健康")),
)


def _remember_goal(profile: Profile, text: str) -> None:
    """用户这次说的目标优先于画像里的旧目标。

    不这么做的结果就是：画像写增肌、这次说减脂，模型每轮都反问你"到底哪个"。
    """
    body = text or ""
    for goal, keywords in GOAL_KEYWORDS:
        if any(keyword in body for keyword in keywords):
            profile.goal = goal
            return


def _remember_avoidances(profile: Profile, text: str) -> None:
    avoided = extract_avoided_foods(text)
    if not avoided:
        return
    current = [str(item) for item in (profile.diet_preferences or [])]
    profile.diet_preferences = current + [item for item in avoided if item not in current]


def _remember_allergies(profile: Profile, text: str) -> None:
    terms = extract_allergy_terms(text)
    if not terms:
        return
    current = [str(item) for item in (profile.allergies or [])]
    profile.allergies = current + [item for item in terms if item not in current]


def _skill_payload(profile: Profile) -> dict:
    return skill_profile(profile)


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

def _propose(agent, profile, instruction: str, feedback: list[str], memory: dict) -> PlanProposal:
    """兼容测试里的简化 FakeAgent：没有 memory_context 参数时不传。"""
    params = inspect.signature(agent.propose).parameters
    if "memory_context" in params:
        return agent.propose(profile, instruction, previous_feedback=feedback,
                             memory_context=memory)
    return agent.propose(profile, instruction, previous_feedback=feedback)


def _propose_deterministic(agent, profile, instruction: str, memory: dict) -> PlanProposal:
    params = inspect.signature(agent.propose_deterministic).parameters
    if "memory_context" in params:
        return agent.propose_deterministic(profile, instruction, memory_context=memory)
    return agent.propose_deterministic(profile, instruction)


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

    # 用户常在目标里直接写「减脂」「海鲜过敏」「不吃红薯」——先写进画像再生成
    _remember_goal(profile, instruction or "")
    _remember_allergies(profile, instruction or "")
    _remember_avoidances(profile, instruction or "")
    memory = load_memory_context(db, user)
    memory_text = memory_instruction(memory)
    if memory_text:
        instruction = (instruction or "") + "\n长期偏好：" + memory_text
    agent = PlanningAgent(bridge or get_bridge(), client=client)
    proposal: PlanProposal = _propose(agent, profile, instruction or "", feedback, memory)

    # 第一次生成时模型也可能反问"要不要加背部动作"，但这句需求我们能执行：
    # 绕开模型，用确定性编排直接出计划。
    if proposal.payload.get("status") != "ready" and has_explicit_edits(instruction or ""):
        retry = _propose_deterministic(agent, profile, instruction or "", memory)
        if retry.payload.get("status") == "ready":
            proposal = retry
            proposal.trace = list(proposal.trace) + [{
                "node": "deterministic_retry",
                "status": "ready",
                "detail": {"reason": "首次生成的需求可执行，不需要向用户反问"},
            }]

    # 用户在第一句话里就可能写了具体编辑（"加个练背动作"），同样要落到计划上
    if proposal.payload.get("status") == "ready" and (instruction or "").strip():
        edited, edit_changes = apply_explicit_edits(
            proposal.payload, instruction, equipment_keys=set(profile.equipment or [])
        )
        if edit_changes:
            proposal.payload = edited
            proposal.trace = list(proposal.trace) + [{
                "node": "apply_explicit_edits",
                "status": "applied",
                "detail": {"changes": edit_changes[:10]},
            }]

    # 长期喜恶也要真的落到计划上：用户说不喜欢深蹲，下一份计划里就不该还是深蹲
    preference_changes = apply_memory_adjustments(proposal.payload, profile, memory)
    if preference_changes:
        proposal.trace = list(proposal.trace) + [{
            "node": "apply_memory_preferences",
            "status": "applied",
            "detail": {"changes": preference_changes[:10]},
        }]

    # 近期状态（连续未完成 / 高难度）要真的影响下一份计划，而不是只在聊天里说一句
    state = recent_training_state(db, user)
    adaptive = apply_training_state_adjustments(proposal.payload, state)
    if adaptive:
        proposal.trace = list(proposal.trace) + [{
            "node": "adaptive_load",
            "status": "applied",
            "detail": {"changes": adaptive[:8], "state": state},
        }]
    if proposal.payload.get("status") == "ready":
        proposal.payload.setdefault("meta", {})["memory"] = {
            "preferred_windows": memory.get("preferred_windows") or [],
            "location": memory.get("location") or "",
            "dislikes": memory.get("dislikes") or [],
            "applied": memory_text,
        }

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
    memory = load_memory_context(db, user)
    memory_text = memory_instruction(memory)
    if memory_text:
        instruction = instruction + "\n长期偏好：" + memory_text
    agent = PlanningAgent(bridge or get_bridge(), client=client)
    proposal = _propose(
        agent, profile, instruction or "",
        [str(item) for item in (parent.feedback or [])], memory,
    )

    # 模型有时会反问"要不要这么做"，但这句反馈其实是可执行的（换食材/加动作/换动作）。
    # 这种问题我们自己能答：绕开模型，用确定性编排生成，再应用具体编辑。
    if proposal.payload.get("status") != "ready" and has_explicit_edits(text or ""):
        retry = _propose_deterministic(agent, profile, instruction or "", memory)
        if retry.payload.get("status") == "ready":
            proposal = retry
            proposal.trace = list(proposal.trace) + [{
                "node": "deterministic_retry",
                "status": "ready",
                "detail": {"reason": "反馈里有可执行的编辑，不需要向用户反问"},
            }]

    # 「不吃红薯」「海鲜过敏」「改成减脂」这类话要记到画像里，否则下一轮又会出现
    _remember_goal(profile, text or "")
    _remember_avoidances(profile, text or "")
    _remember_allergies(profile, text or "")

    # 关键：反馈必须落到计划内容上，而不是只改解释文案。
    # 顺序：具体编辑（换食材/加动作/删动作）→ 模糊变换（换一批/加强度/避膝）。
    if proposal.payload.get("status") == "ready" and text:
        revised, changes = apply_explicit_edits(
            proposal.payload, text, equipment_keys=set(profile.equipment or [])
        )
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

    preference_changes = apply_memory_adjustments(proposal.payload, profile, memory)
    if preference_changes:
        proposal.trace = list(proposal.trace) + [{
            "node": "apply_memory_preferences",
            "status": "applied",
            "detail": {"changes": preference_changes[:10]},
        }]

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
    from app.domain.planning import clamp_protein_band
    from app.models import Profile

    profile = db.get(Profile, user.id)
    weight = float(getattr(profile, "weight_kg", 0) or 0)
    nutrition = clamp_protein_band(payload.get("nutrition") or {}, weight)
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


def _dislike_tokens(dislikes: list[str]) -> list[tuple[str, str]]:
    """把长期"不喜欢深蹲"翻译成 ref 匹配 token（复用原有动作别名表）。"""
    from app.domain.planner import EXCLUDE_ACTION_TERMS

    out: list[tuple[str, str]] = []
    for label in dislikes:
        config = EXCLUDE_ACTION_TERMS.get(label)
        tokens = config[0] if config else (label,)
        for token in tokens:
            out.append((str(token).lower(), label))
    return out


def apply_memory_adjustments(payload: dict, profile: Profile, memory: dict) -> list[str]:
    """按长期喜恶替换动作；只换动作，不改组次与安全边界。"""
    dislikes = [str(item) for item in (memory.get("dislikes") or []) if item]
    if not dislikes or payload.get("status") != "ready":
        return []
    tokens = _dislike_tokens(dislikes)
    allowed = allowed_equipment(profile, {})
    used = {
        str(action.get("action_ref"))
        for day in (payload.get("days") or [])
        for action in (day.get("actions") or [])
    }
    changes: list[str] = []

    def matches(action: dict) -> str:
        ref = str(action.get("action_ref") or "").lower().replace("_", "-")
        name = str((action.get("meta") or {}).get("name_zh") or "")
        for token, label in tokens:
            if token and (token in ref or token in name):
                return label
        return ""

    for day in payload.get("days") or []:
        actions = day.get("actions") or []
        for index, action in enumerate(actions):
            label = matches(action)
            if not label:
                continue
            action_dict = {
                **action,
                "name": (action.get("meta") or {}).get("name_zh") or action.get("action_ref"),
            }
            replacement = None
            for _ in range(12):
                candidate = pick_alternative(
                    action_dict, allowed=allowed, injured=[], used=used,
                )
                if candidate is None:
                    break
                disliked = any(
                    str(token) in str(candidate.get("ref") or "").lower()
                    or str(token) in candidate["name"]
                    or label in candidate["name"]
                    for token, label in tokens
                )
                if not disliked:
                    replacement = candidate
                    break
                # 候选也是用户讨厌的动作（例如自重深蹲）：把它排除后继续找
                used.add(candidate["ref"])
            if replacement is None:
                # 没有更合适的替代：宁可保留，也不换成另一个用户同样讨厌的动作
                continue
            old_name = action_dict["name"]
            action["action_ref"] = replacement["ref"]
            action["meta"] = {
                **(action.get("meta") or {}),
                "name_zh": replacement["name"],
                "movement_mode": replacement.get("movement_mode")
                or action.get("movement_mode"),
                "equipment": replacement.get("equipment"),
                "primary_muscle": replacement.get("primary_muscle"),
                "replaced_from": action.get("action_ref"),
                "reason": f"长期偏好：避开{label}",
            }
            used.add(replacement["ref"])
            actions[index] = action
            changes.append(
                f"第 {day.get('day_index')} 天「{old_name}」→「{replacement['name']}」（你偏好避开{label}）"
            )
        day["actions"] = actions

    if changes:
        payload["revisions"] = [str(item) for item in (payload.get("revisions") or []) + changes]
        payload.setdefault("cautions", []).append(
            "已按你长期表达的偏好替换动作；如果换到的动作也不合适，直接说，我再调。"
        )
        payload.setdefault("meta", {})["memory_changes"] = changes
    return changes


def apply_training_state_adjustments(payload: dict, state: dict) -> list[str]:
    """把"最近连续没完成 / 难度偏高"落到计划内容上（确定性、可解释）。

    只做减量，不自动加量：用户连续两次没完成时，最该做的是降低负担，
    而不是让模型换个说法继续排同样的量。
    """
    changes: list[str] = []
    if payload.get("status") != "ready" or not state.get("load_reduced"):
        return changes
    days = payload.get("days") or []
    for day in days:
        actions = day.get("actions") or []
        for action in actions:
            if not action.get("sets"):
                continue
            current = int(action["sets"])
            new_sets = max(1, current - 1)
            if new_sets != current:
                action["sets"] = new_sets
        # 每场只保留最短版本：动作多的时候砍掉最后一个，避免"组数少了但动作没少"
        if len(actions) > 2:
            dropped = actions.pop()
            changes.append(
                f"第 {day.get('day_index')} 天去掉「{(dropped.get('meta') or {}).get('name_zh') or dropped.get('action_ref')}」"
            )
    if not changes:
        changes.append("每个动作组数下调 1 组（保留最短可执行版本）")
    payload["adaptive"] = {
        "load_reduced": True,
        "consecutive_incomplete": state.get("consecutive_incomplete"),
        "reason": state.get("reason"),
        "changes": changes,
    }
    payload["revisions"] = [str(item) for item in (payload.get("revisions") or [])] + [
        "最近连续未完成训练，已主动降低训练负担：" + "；".join(changes[:3])
    ]
    payload["cautions"] = [str(item) for item in (payload.get("cautions") or [])] + [
        f"你最近连续 {state.get('consecutive_incomplete')} 次完成率偏低，这一版先以完成为目标。"
    ]
    payload["summary"] = (
        str(payload.get("summary") or "")
        + " 最近连续几次没完成，这一版已把训练量降下来，先保证能坚持。"
    )
    return changes


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
        "adaptive": payload.get("adaptive") or {},
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
