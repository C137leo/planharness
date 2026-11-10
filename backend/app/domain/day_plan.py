"""今日计划的有效视图 + 临时调整。

核心数据一致性约定：

- **PlanVersion 是长期计划**，临时要求永远不改它；
- 「今天只有 20 分钟 / 不去健身房」这类调整写进 `TemporaryContext.plan_override`，
  只在当天生效，第二天自动失效；
- 今日页读的是"有效计划"（base plan + 当天 override），不是直接读版本，
  所以前端展示和后端存储必然一致。

生成 override 时只做确定性变换：换动作、压时长、减组数、避开不适部位。
安全门（禁忌动作、可用天数）仍由原有 patch / 生成链路负责，这里不另造一套。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.catalog import name_of
from app.domain.exercise_catalog import EXERCISE_CATALOG
from app.domain.memory import (
    clear_temporary,
    has_long_term_signal,
    has_temp_signal,
    load_temporary,
    set_temporary,
    time_window_of,
)
from app.domain.safety_rules import is_risky_for, movement_side
from app.domain.timeutil import as_utc, day_end_utc, user_today
from app.models import (
    MealPlan,
    Plan,
    PlanAction,
    PlanAdjustment,
    PlanVersion,
    Profile,
    User,
)

OVERRIDE_KEY = "plan_override"

# 目录里 equipment 是英文枚举；这里统一成内部 code，方便和画像/约束比较
EQUIPMENT_CODES = {
    "bodyweight": "bodyweight",
    "chair": "bodyweight",
    "doorway": "bodyweight",
    "cardio": "bodyweight",
    "": "bodyweight",
    "dumbbell": "dumbbell",
    "barbell": "barbell",
    "machine": "machine",
    "cable": "cable",
    "resistance band": "band",
    "band": "band",
    "kettlebell": "kettlebell",
    "pull-up bar": "pullup_bar",
    "bench": "bench",
    "mat": "bodyweight",
    "treadmill": "machine",
}

PROFILE_EQUIPMENT = {
    "dumbbell": "dumbbell",
    "band": "band",
    "bench": "bench",
    "treadmill": "machine",
    "barbell": "barbell",
    "mat": "bodyweight",
    "bodyweight": "bodyweight",
}

LEGACY_META: dict[str, dict] = {
    "goblet_squat": {"primary_muscle": "quads", "movement_mode": "knee_dominant",
                     "equipment": "dumbbell"},
    "squat": {"primary_muscle": "quads", "movement_mode": "knee_dominant",
              "equipment": "dumbbell"},
    "pushup": {"primary_muscle": "chest", "movement_mode": "horizontal_push",
               "equipment": "bodyweight"},
    "row": {"primary_muscle": "back", "movement_mode": "horizontal_pull",
            "equipment": "dumbbell"},
    "backpack_row": {"primary_muscle": "back", "movement_mode": "horizontal_pull",
                     "equipment": "bodyweight"},
    "brisk_walk": {"primary_muscle": "legs", "movement_mode": "cardio",
                   "equipment": "bodyweight"},
    "easy_walk": {"primary_muscle": "legs", "movement_mode": "cardio",
                  "equipment": "bodyweight"},
    "dynamic_warmup": {"primary_muscle": "mobility", "movement_mode": "warmup",
                       "equipment": "bodyweight"},
    "full_body_stretch": {"primary_muscle": "mobility", "movement_mode": "mobility",
                          "equipment": "bodyweight"},
    "stretch": {"primary_muscle": "mobility", "movement_mode": "mobility",
                "equipment": "bodyweight"},
    "glute_bridge": {"primary_muscle": "glutes", "movement_mode": "hip_dominant",
                     "equipment": "bodyweight"},
    "run": {"primary_muscle": "legs", "movement_mode": "cardio",
            "equipment": "bodyweight"},
}

CN_NUMBERS = {"半": 30}


def _norm_equipment(value: str) -> str:
    return EQUIPMENT_CODES.get(str(value or "").strip().lower(), "bodyweight")


def _action_equipment(ref: str, meta: dict | None = None) -> str:
    meta = meta or {}
    explicit = meta.get("equipment")
    if isinstance(explicit, str) and explicit.strip():
        return _norm_equipment(explicit)
    item = EXERCISE_CATALOG.get(ref) or EXERCISE_CATALOG.get(
        str(ref or "").replace("_", "-")
    ) or {}
    if item.get("equipment"):
        return _norm_equipment(str(item.get("equipment")))
    legacy = LEGACY_META.get(ref) or LEGACY_META.get(str(ref or "").replace("-", "_")) or {}
    return _norm_equipment(str(legacy.get("equipment") or "bodyweight"))


def _action_mode(ref: str, meta: dict | None = None) -> str:
    meta = meta or {}
    explicit = meta.get("movement_mode")
    if explicit:
        return str(explicit)
    legacy = LEGACY_META.get(ref) or LEGACY_META.get(str(ref or "").replace("-", "_")) or {}
    return str(legacy.get("movement_mode") or "")


def _action_muscle(ref: str, meta: dict | None = None) -> str:
    meta = meta or {}
    explicit = meta.get("primary_muscle")
    if explicit:
        return str(explicit).lower()
    item = EXERCISE_CATALOG.get(ref) or EXERCISE_CATALOG.get(
        str(ref or "").replace("_", "-")
    ) or {}
    if item.get("primary_muscle"):
        return str(item["primary_muscle"]).lower()
    legacy = LEGACY_META.get(ref) or {}
    return str(legacy.get("primary_muscle") or "").lower()


def _estimated_minutes(action: dict) -> int:
    duration = action.get("duration_min")
    if duration:
        return max(1, int(duration))
    slot = str(action.get("slot") or "")
    if slot == "cardio":
        return 25
    if slot in ("warmup",):
        return 5
    if slot in ("mobility",):
        return 10
    sets = int(action.get("sets") or 3)
    # 力量动作：每组约 2 分钟（含组间休息），至少 5 分钟
    return max(5, sets * 2 + 2)


def _rest_seconds(action: dict) -> int:
    slot = str(action.get("slot") or "")
    if slot == "cardio":
        return 30
    if slot in ("warmup", "mobility"):
        return 20
    return 60


def _base_version(db: Session, user: User) -> tuple[Plan | None, PlanVersion | None]:
    plan = db.scalar(
        select(Plan).where(Plan.user_id == user.id, Plan.status == "active")
            .order_by(Plan.created_at.desc())
    )
    if plan is None:
        return None, None
    version = db.scalar(
        select(PlanVersion).where(
            PlanVersion.plan_id == plan.id,
            PlanVersion.version == plan.current_version,
        )
    )
    return plan, version


def _action_dict(action: PlanAction) -> dict:
    meta = dict(action.meta or {})
    ref = action.action_ref
    return {
        "id": action.id,
        "slot_key": action.slot_key,
        "day_index": action.day_index,
        "slot": action.slot,
        "seq": action.seq,
        "action_ref": ref,
        "name": meta.get("name_zh") or name_of(ref),
        "sets": int(action.sets or 0) or None,
        "reps": int(action.reps) if action.reps else None,
        "duration_min": action.duration_min,
        "intensity": action.intensity,
        "movement_mode": _action_mode(ref, meta),
        "primary_muscle": _action_muscle(ref, meta),
        "equipment": _action_equipment(ref, meta),
        "meta": meta,
        "source": "plan",
    }


def _with_warmup(actions: list[dict]) -> list[dict]:
    """每一场训练都从 5 分钟热身开始；已有热身就不重复加。"""
    if not actions:
        return actions
    if any(item.get("slot") in ("warmup",) or item.get("movement_mode") == "warmup"
           for item in actions):
        return actions
    warmup = {
        "id": "warmup",
        "slot_key": "warmup:1",
        "day_index": actions[0].get("day_index") or 1,
        "slot": "warmup",
        "seq": 1,
        "action_ref": "dynamic_warmup",
        "name": "动态热身",
        "sets": 1,
        "reps": None,
        "duration_min": 5,
        "intensity": "light",
        "movement_mode": "warmup",
        "primary_muscle": "mobility",
        "equipment": "bodyweight",
        "meta": {"name_zh": "动态热身"},
        "source": "plan",
    }
    return [warmup] + actions


SLOT_ORDER = {"warmup": 0, "mobility": 1, "strength": 2, "cardio": 3}


def _weekday_actions(db: Session, version: PlanVersion, weekday: int) -> list[dict]:
    rows = db.scalars(
        select(PlanAction)
        .where(
            PlanAction.plan_version_id == version.id,
            PlanAction.day_index == weekday,
            PlanAction.action_type == "exercise",
        )
    ).all()
    # 热身必须排在最前；不能按 slot 字母序（strength 会跑到 warmup 前面）
    rows = sorted(rows, key=lambda row: (
        SLOT_ORDER.get(str(row.slot), 9), row.seq or 1, row.slot_key or ""
    ))
    return [_action_dict(row) for row in rows]


def _next_training_day(db: Session, version: PlanVersion, weekday: int) -> tuple[int, list[dict]] | None:
    """今天没有训练时，找最近的下一个训练日（循环一周）。

    产品上这是"把下一次训练提前到今天"，而不是假装今天原本有安排。
    """
    rows = db.scalars(
        select(PlanAction)
        .where(
            PlanAction.plan_version_id == version.id,
            PlanAction.action_type == "exercise",
        )
        .order_by(PlanAction.day_index)
    ).all()
    # 优先找真正的力量训练日；只有拉伸的恢复日不能当作"下一次训练"提前
    strength_days = sorted({row.day_index for row in rows if str(row.slot) == "strength"})
    days = strength_days or sorted({row.day_index for row in rows})
    if not days:
        return None
    upcoming = [day for day in days if day > weekday]
    target = upcoming[0] if upcoming else days[0]
    actions = _weekday_actions(db, version, target)
    return (target, actions) if actions else None


def load_base_day(db: Session, user: User, day: dt.date) -> dict:
    plan, version = _base_version(db, user)
    if plan is None or version is None:
        return {"plan": None, "version": None, "actions": [], "meals": []}
    weekday = day.isoweekday()
    actions = _weekday_actions(db, version, weekday)
    meals = db.scalars(
        select(MealPlan).where(
            MealPlan.plan_version_id == version.id,
            MealPlan.day_index == weekday,
        )
    ).all()
    return {
        "plan": plan,
        "version": version,
        "weekday": weekday,
        "actions": actions,
        "meals": list(meals),
    }


def adjustment_payload(row: PlanAdjustment) -> dict:
    return {
        "adjustment_id": row.id,
        "date": row.effective_date.isoformat() if row.effective_date else "",
        "status": row.status,
        "plan_id": row.plan_id,
        "base_version": row.base_version,
        "summary": row.summary,
        "reason": row.reason,
        "constraints": row.constraints or {},
        "changes": row.changes or [],
        "actions": row.actions or [],
        "borrowed_from": row.borrowed_from,
        "created_at": as_utc(row.created_at).isoformat() if row.created_at else "",
        "updated_at": as_utc(row.updated_at).isoformat() if row.updated_at else "",
        "expires_at": as_utc(row.expires_at).isoformat() if row.expires_at else "",
    }


def load_active_adjustment(
    db: Session, user: User, day: dt.date, version: PlanVersion | None,
) -> PlanAdjustment | None:
    row = db.scalar(
        select(PlanAdjustment)
        .where(
            PlanAdjustment.user_id == user.id,
            PlanAdjustment.effective_date == day,
            PlanAdjustment.status == "active",
        )
        .order_by(PlanAdjustment.created_at.desc())
    )
    if row is None:
        return None
    expires = as_utc(row.expires_at)
    if expires is not None and expires <= dt.datetime.now(dt.timezone.utc):
        return None
    if version is not None and row.base_version not in (None, version.version):
        # 计划版本已经变化：旧调整不能继续标记为 active，否则计划页会显示"调"但今天页不生效
        row.status = "superseded"
        row.updated_at = dt.datetime.now(dt.timezone.utc)
        db.add(row)
        db.flush()
        return None
    return row


def mark_stale_adjustments(
    db: Session, user: User, current_version: int | None,
) -> int:
    if current_version is None:
        return 0
    rows = list(db.scalars(
        select(PlanAdjustment).where(
            PlanAdjustment.user_id == user.id,
            PlanAdjustment.status == "active",
            PlanAdjustment.base_version.is_not(None),
            PlanAdjustment.base_version != current_version,
        )
    ).all())
    for row in rows:
        row.status = "superseded"
        row.updated_at = dt.datetime.now(dt.timezone.utc)
        db.add(row)
    if rows:
        db.flush()
    return len(rows)


def load_override(db: Session, user: User, day: dt.date, version: PlanVersion | None) -> dict | None:
    row = load_active_adjustment(db, user, day, version)
    if row is not None:
        return {
            **adjustment_payload(row),
            "weekday": day.isoweekday(),
            "base_version": row.base_version,
            "actions": list(row.actions or []),
            "constraints": dict(row.constraints or {}),
            "summary": row.summary,
            "reason": row.reason,
            "borrowed_from": row.borrowed_from,
            "created_at": as_utc(row.created_at).isoformat() if row.created_at else "",
        }
    # 兼容旧数据：TemporaryContext.plan_override。迁移后不会再有新记录。
    context = load_temporary(db, user, day, include_completed=True)
    legacy = context.get(OVERRIDE_KEY)
    if not legacy:
        return None
    if version is not None and legacy.get("base_version") not in (None, version.version):
        return None
    if legacy.get("weekday") not in (None, day.isoweekday()):
        return None
    return legacy


def save_override(
    db: Session,
    user: User,
    day: dt.date,
    *,
    actions: list[dict],
    summary: str,
    constraints: dict,
    reason: str,
    base_version: int | None,
    borrowed_from: int | None = None,
    changes: list[str] | None = None,
) -> dict:
    profile = db.get(Profile, user.id)
    plan = db.scalar(
        select(Plan).where(Plan.user_id == user.id, Plan.status == "active")
    )
    # 同一天只保留一个 active 调整；旧的标 superseded，保留可追溯历史
    previous = list(db.scalars(
        select(PlanAdjustment).where(
            PlanAdjustment.user_id == user.id,
            PlanAdjustment.effective_date == day,
            PlanAdjustment.status == "active",
        )
    ).all())
    for row in previous:
        row.status = "superseded"
        row.updated_at = dt.datetime.now(dt.timezone.utc)
        db.add(row)

    row = PlanAdjustment(
        user_id=user.id,
        plan_id=plan.id if plan is not None else None,
        base_version=base_version,
        effective_date=day,
        status="active",
        reason=(reason or "")[:2000],
        summary=(summary or "")[:2000],
        constraints=constraints or {},
        actions=actions or [],
        changes=changes or [],
        borrowed_from=borrowed_from,
        expires_at=day_end_utc(day, profile),
    )
    db.add(row)
    db.flush()
    return {
        **adjustment_payload(row),
        "weekday": day.isoweekday(),
        "base_version": base_version,
        "actions": list(actions or []),
        "constraints": dict(constraints or {}),
        "summary": summary,
        "reason": reason,
        "borrowed_from": borrowed_from,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


def restore_adjustment(db: Session, user: User, adjustment_id: str) -> PlanAdjustment:
    """把一条历史调整重新设为当天有效；旧 active 标 superseded。"""
    row = db.get(PlanAdjustment, adjustment_id)
    if row is None or row.user_id != user.id:
        raise ValueError("adjustment not found")
    plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    if plan is not None and row.base_version not in (None, plan.current_version):
        raise ValueError("计划版本已变化，这条调整不能直接恢复")
    now = dt.datetime.now(dt.timezone.utc)
    for current in db.scalars(
        select(PlanAdjustment).where(
            PlanAdjustment.user_id == user.id,
            PlanAdjustment.effective_date == row.effective_date,
            PlanAdjustment.status == "active",
        )
    ).all():
        current.status = "superseded"
        current.updated_at = now
        db.add(current)
    row.status = "active"
    row.expires_at = day_end_utc(row.effective_date, db.get(Profile, user.id))
    row.updated_at = now
    db.add(row)
    db.flush()
    return row


def list_adjustments(
    db: Session, user: User, *, start: dt.date | None = None,
    end: dt.date | None = None, limit: int = 60,
) -> list[PlanAdjustment]:
    query = select(PlanAdjustment).where(PlanAdjustment.user_id == user.id)
    if start is not None:
        query = query.where(PlanAdjustment.effective_date >= start)
    if end is not None:
        query = query.where(PlanAdjustment.effective_date <= end)
    return list(db.scalars(
        query.order_by(PlanAdjustment.effective_date.desc(),
                       PlanAdjustment.created_at.desc()).limit(max(1, min(limit, 200)))
    ).all())


def effective_day(
    db: Session,
    user: User,
    day: dt.date,
    *,
    fallback_next: bool = False,
) -> dict:
    """今天真正该执行什么：base plan + active PlanAdjustment + 是否把下一次提前。"""
    base = load_base_day(db, user, day)
    plan, version = base["plan"], base["version"]
    weekday = day.isoweekday()
    actions = base["actions"]
    borrowed_from = None
    if fallback_next and not actions and version is not None:
        upcoming = _next_training_day(db, version, weekday)
        if upcoming is not None:
            borrowed_from, actions = upcoming
    actions = _with_warmup(actions)

    override = load_override(db, user, day, version)
    source = "plan"
    summary = ""
    constraints: dict = {}
    reason = ""
    adjustment = None
    if override:
        actions = list(override.get("actions") or [])
        source = "agent"
        summary = str(override.get("summary") or "")
        constraints = dict(override.get("constraints") or {})
        reason = str(override.get("reason") or "")
        borrowed_from = override.get("borrowed_from") or borrowed_from
        adjustment = override
    elif borrowed_from:
        source = "plan"
        summary = f"今天原本休息，已把第 {borrowed_from} 天的训练提前到今天"

    return {
        "date": day.isoformat(),
        "weekday": weekday,
        "plan_id": plan.id if plan is not None else None,
        "plan_version": version.version if version is not None else None,
        "source": source,
        "summary": summary,
        "reason": reason,
        "constraints": constraints,
        "actions": actions,
        "base_action_count": len(base["actions"]),
        "borrowed_from": borrowed_from,
        "adjustment_id": (adjustment or {}).get("adjustment_id"),
        "adjustment": adjustment,
        "temporary_context": load_temporary(
            db, user, day, include_completed=True,
        ),
        "meals": [
            {
                "slot": meal.slot,
                "title": meal.title,
                "items": meal.items or [],
                "kcal": meal.kcal,
                "protein_g": meal.protein_g,
                "fat_g": meal.fat_g,
                "carb_g": meal.carb_g,
            }
            for meal in base["meals"]
        ],
        "total_minutes": sum(_estimated_minutes(item) for item in actions),
    }


def reset_day(db: Session, user: User, day: dt.date) -> int:
    """恢复原计划：把当天 active 调整标 reset，并重置临时上下文；不物理删除历史。"""
    now = dt.datetime.now(dt.timezone.utc)
    rows = list(db.scalars(
        select(PlanAdjustment).where(
            PlanAdjustment.user_id == user.id,
            PlanAdjustment.effective_date == day,
            PlanAdjustment.status == "active",
        )
    ).all())
    for row in rows:
        row.status = "reset"
        row.updated_at = now
        db.add(row)
    # 兼容旧 override 与当天环境上下文：标 reset，保留证据
    clear_temporary(db, user, day, status="reset")
    db.flush()
    return len(rows)


# ---------------------------------------------------------------- 约束解析 / 应用


def allowed_equipment(profile: Profile | None, constraints: dict) -> set[str]:
    allowed = {"bodyweight"}
    location = str(constraints.get("location") or "")
    no_gym = bool(constraints.get("no_gym"))
    if location == "office":
        # 办公室默认只有身体和椅子；不把"办公室"翻译成"健身房"
        return {"bodyweight"}
    if not no_gym or location in ("home", "outdoor", ""):
        for item in (getattr(profile, "equipment", None) or []):
            code = PROFILE_EQUIPMENT.get(str(item))
            if code:
                allowed.add(code)
    if location == "gym":
        allowed.update({"dumbbell", "barbell", "machine", "cable", "bench", "pullup_bar", "band"})
    # 明确说"没有/不能用"的器械：包括画像里有的也要去掉
    for item in constraints.get("unavailable_equipment") or []:
        code = PROFILE_EQUIPMENT.get(str(item), str(item))
        allowed.discard(code)
    if constraints.get("equipment_busy"):
        # "器械被占用"先按"这组器械暂时不能用"处理；具体替换仍按当前动作判断
        for code in ("machine", "cable", "barbell", "dumbbell", "bench", "pullup_bar"):
            allowed.discard(code)
    return allowed


@dataclass
class AdjustmentResult:
    actions: list[dict]
    changes: list[str] = field(default_factory=list)
    dropped: int = 0
    constraints: dict = field(default_factory=dict)


def _catalog_modes() -> dict[str, str]:
    """目录本身没有 movement_mode；用 revisions 的人工分组反查。"""
    from app.domain.revisions import MODE_ALTERNATIVES

    mapping: dict[str, str] = {}
    for mode, refs in MODE_ALTERNATIVES.items():
        for ref in refs:
            mapping.setdefault(ref, mode)
    return mapping


_CATALOG_MODES = _catalog_modes()


def _candidate_pool(
    allowed: set[str],
    injured: list[str],
    used: set[str],
    *,
    avoid_ref: str,
) -> list[dict]:
    pool: list[dict] = []
    for ref, item in EXERCISE_CATALOG.items():
        if ref == avoid_ref or ref in used:
            continue
        equipment = _norm_equipment(str(item.get("equipment") or ""))
        if equipment not in allowed:
            continue
        if any(is_risky_for(ref, region) for region in injured):
            continue
        pool.append({
            "ref": ref,
            "name": name_of(ref),
            "equipment": equipment,
            "movement_mode": _CATALOG_MODES.get(ref, ""),
            "primary_muscle": str(item.get("primary_muscle") or "").lower(),
            "has_zh": bool(item.get("name_zh")),
        })
    # 模板里的短 ref 也要留在候选池：它们有中文名，适合给普通用户
    for ref, meta in LEGACY_META.items():
        if ref == avoid_ref or ref in used:
            continue
        equipment = _norm_equipment(meta.get("equipment") or "")
        if equipment not in allowed:
            continue
        if any(is_risky_for(ref, region) for region in injured):
            continue
        pool.append({
            "ref": ref,
            "name": name_of(ref),
            "equipment": equipment,
            "movement_mode": str(meta.get("movement_mode") or ""),
            "primary_muscle": str(meta.get("primary_muscle") or "").lower(),
            "has_zh": True,
        })
    return pool


def pick_alternative(
    action: dict,
    *,
    allowed: set[str],
    injured: list[str],
    used: set[str],
    prefer_bodyweight: bool = False,
    mode: str = "",
) -> dict | None:
    """挑一个同肌群 / 同动作模式的替代动作；找不到就返回 None（由调用方删掉）。"""
    ref = str(action.get("action_ref") or "")
    target_mode = mode or _action_mode(ref, action.get("meta"))
    target_muscle = _action_muscle(ref, action.get("meta"))
    pool = _candidate_pool(allowed, injured, used, avoid_ref=ref)

    def score(item: dict) -> tuple:
        same_mode = 1 if target_mode and item["movement_mode"] == target_mode else 0
        same_muscle = 1 if target_muscle and item["primary_muscle"] == target_muscle else 0
        bodyweight = 1 if item["equipment"] == "bodyweight" else 0
        return (
            same_mode * 4 + same_muscle * 3 + bodyweight * (2 if prefer_bodyweight else 0)
            + (1 if item["has_zh"] else 0),
            -len(item["ref"]),
        )

    current_name = str(action.get("name") or "")
    candidates = [item for item in pool if
                  (target_mode and item["movement_mode"] == target_mode)
                  or (target_muscle and item["primary_muscle"] == target_muscle)]
    if not candidates:
        candidates = [item for item in pool if item["primary_muscle"] in ("core", "mobility")]
    if not candidates:
        candidates = pool
    # 名称相同（如 glute-bridge / glute_bridge）不算"换了一个动作"
    candidates = [item for item in candidates if item["name"] != current_name]
    if not candidates:
        return None
    candidates.sort(key=score, reverse=True)
    return candidates[0]


def _replace_action(
    action: dict,
    replacement: dict,
    reason: str,
    *,
    constraints: dict,
) -> dict:
    updated = dict(action)
    updated["action_ref"] = replacement["ref"]
    updated["name"] = replacement["name"]
    updated["equipment"] = replacement["equipment"]
    updated["movement_mode"] = replacement.get("movement_mode") or updated.get("movement_mode")
    updated["primary_muscle"] = replacement.get("primary_muscle") or updated.get("primary_muscle")
    updated["source"] = "agent"
    updated["reason"] = reason
    updated["original_ref"] = action.get("original_ref") or action.get("action_ref")
    updated["meta"] = {
        **(action.get("meta") or {}),
        "name_zh": replacement["name"],
        "movement_mode": updated["movement_mode"],
        "equipment": replacement["equipment"],
        "primary_muscle": updated["primary_muscle"],
        "replaced_from": action.get("action_ref"),
        "reason": reason,
    }
    # 时长沿用原动作，避免"换个动作时间翻倍"
    if not updated.get("duration_min") and action.get("duration_min"):
        updated["duration_min"] = action["duration_min"]
    return updated


def _cap_minutes(actions: list[dict], target: int) -> tuple[list[dict], int]:
    """把动作压到 target 分钟内：先砍后面的动作，最后一个按剩余时间缩。"""
    if target <= 0:
        return actions, 0
    target = max(5, min(target, 240))
    kept: list[dict] = []
    remaining = target
    dropped = 0
    for index, action in enumerate(actions):
        need = _estimated_minutes(action)
        if remaining <= 0:
            dropped += 1
            continue
        if need <= remaining:
            kept.append(action)
            remaining -= need
            continue
        # 最后一个能部分保留：至少 2 分钟，否则整条丢掉
        if remaining >= 2:
            item = dict(action)
            ratio = remaining / max(need, 1)
            item["duration_min"] = max(2, remaining)
            if item.get("sets"):
                item["sets"] = max(1, round(int(item["sets"]) * ratio))
            item["source"] = item.get("source") or "agent"
            kept.append(item)
            remaining = 0
        else:
            dropped += 1
    if not kept and actions:
        # 时间极短：保留第一个动作的最短版本，而不是让用户"今天没安排"
        item = dict(actions[0])
        item["duration_min"] = max(2, target)
        if item.get("sets"):
            item["sets"] = 1
        kept = [item]
    return kept, dropped


def apply_constraints(
    db: Session,
    user: User,
    base_actions: list[dict],
    constraints: dict,
    *,
    day: dt.date | None = None,
) -> AdjustmentResult:
    """把临时约束落到今天的动作上（纯确定性，不依赖模型）。"""
    profile = db.get(Profile, user.id)
    actions = [dict(item) for item in base_actions]
    changes: list[str] = []
    injured = [str(item) for item in (constraints.get("injured_regions") or [])]
    allowed = allowed_equipment(profile, constraints)
    used = {str(item.get("action_ref")) for item in actions}
    dropped_total = 0

    if constraints.get("rest"):
        total = int(constraints.get("minutes") or 15)
        stretch = {
            "id": "rest",
            "slot_key": "mobility:rest:1",
            "day_index": day.isoweekday() if day else 1,
            "slot": "mobility",
            "seq": 1,
            "action_ref": "stretch",
            "name": "全身拉伸",
            "sets": None,
            "reps": None,
            "duration_min": max(5, total),
            "intensity": "light",
            "movement_mode": "mobility",
            "primary_muscle": "mobility",
            "equipment": "bodyweight",
            "meta": {"name_zh": "全身拉伸", "movement_mode": "mobility"},
            "source": "agent",
            "reason": "今天以恢复为主",
        }
        return AdjustmentResult(
            actions=[stretch],
            changes=["今天改成恢复日：训练换成拉伸，饮食结构不变"],
            constraints=constraints,
        )

    prefer_bodyweight = bool(constraints.get("no_gym")) or constraints.get("location") == "office"
    replaced: list[dict] = []
    for action in actions:
        ref = str(action.get("action_ref") or "")
        equipment = _action_equipment(ref, action.get("meta"))
        must_replace = False
        reason = ""
        if injured and any(is_risky_for(ref, region, action.get("meta")) for region in injured):
            must_replace = True
            reason = "避开今天不舒服的部位"
        elif equipment not in allowed:
            must_replace = True
            reason = f"今天没有 {equipment}，换成不需要它的动作" if equipment != "bodyweight" \
                else "按今天的场地条件换成更合适的动作"
        if must_replace:
            replacement = pick_alternative(
                action, allowed=allowed, injured=injured, used=used,
                prefer_bodyweight=prefer_bodyweight,
            )
            if replacement is None:
                changes.append(f"去掉了「{action.get('name') or ref}」（今天条件不满足，又没有合适替代）")
                dropped_total += 1
                used.add(ref)
                continue
            used.add(replacement["ref"])
            replaced.append(_replace_action(action, replacement, reason, constraints=constraints))
            changes.append(
                f"「{action.get('name') or ref}」→「{replacement['name']}」（{reason}）"
            )
        else:
            replaced.append(action)

    if constraints.get("easier"):
        for action in replaced:
            if not action.get("sets"):
                continue
            current = int(action["sets"])
            new_sets = max(1, current - 1)
            if new_sets != current:
                action["sets"] = new_sets
                action["source"] = "agent"
                action["reason"] = "按你的疲劳程度减一组"
        changes.append("每个力量动作减 1 组，先保证完成")

    if constraints.get("minutes"):
        before = sum(_estimated_minutes(item) for item in replaced)
        replaced, dropped = _cap_minutes(replaced, int(constraints["minutes"]))
        dropped_total += dropped
        after = sum(_estimated_minutes(item) for item in replaced)
        changes.append(
            f"按今天可用时间压到约 {after} 分钟（原本约 {before} 分钟，少 {dropped} 个动作）"
            if dropped else f"按今天可用时间压到约 {after} 分钟"
        )

    return AdjustmentResult(
        actions=replaced,
        changes=changes,
        dropped=dropped_total,
        constraints=constraints,
    )


def summarise_adjustment(actions: list[dict], constraints: dict, changes: list[str]) -> str:
    pieces: list[str] = []
    minutes = constraints.get("minutes")
    if minutes:
        pieces.append(f"{int(minutes)} 分钟")
    location = constraints.get("location")
    if location:
        pieces.append({"home": "在家", "office": "办公室", "gym": "健身房",
                       "outdoor": "户外"}.get(str(location), str(location)))
    if constraints.get("no_gym"):
        pieces.append("不去健身房")
    if constraints.get("equipment_busy") or constraints.get("unavailable_equipment"):
        pieces.append("不用被占用 / 不可用的器械")
    if constraints.get("injured_regions"):
        pieces.append("避开不适部位")
    if constraints.get("preferred_window"):
        window = constraints["preferred_window"] or {}
        pieces.append(f"时间：{window.get('label') or window.get('window')}")
    if constraints.get("easier"):
        pieces.append("强度下调")
    head = " · ".join(pieces) if pieces else "按你的要求"
    names = "、".join(str(item.get("name") or item.get("action_ref")) for item in actions[:5])
    if len(actions) > 5:
        names += f" 等 {len(actions)} 个动作"
    return f"{head}：{names}。" + ("；".join(changes[:3]) if changes else "")


# ---------------------------------------------------------------- 解析辅助


def parse_minutes(text: str) -> int | None:
    raw = text or ""
    if "半小时" in raw:
        return 30
    if "一小时" in raw or "1小时" in raw:
        return 60
    match = re.search(r"(\d{1,3})\s*(?:分钟|min|mins)", raw)
    if match:
        value = int(match.group(1))
        if 5 <= value <= 240:
            return value
    return None


def parse_location(text: str) -> tuple[str | None, bool]:
    raw = text or ""
    no_gym = any(word in raw for word in ("不去健身房", "不在健身房", "没去健身房", "不去健身",
                                          "健身房关了", "不去练"))
    if "办公室" in raw or "公司" in raw:
        return "office", no_gym
    if "在家" in raw or "家里" in raw or "居家" in raw:
        return "home", no_gym
    if "户外" in raw or "公园" in raw:
        return "outdoor", no_gym
    if "健身房" in raw and not no_gym:
        return "gym", False
    if no_gym:
        return "home", True
    return None, no_gym


def parse_equipment_constraints(text: str) -> dict:
    raw = text or ""
    busy = any(word in raw for word in ("被占用", "有人用", "在用", "占着", "被人用"))
    unavailable: list[str] = []
    for code, label in (("dumbbell", "哑铃"), ("band", "弹力带"),
                        ("barbell", "杠铃"), ("bench", "训练凳"),
                        ("kettlebell", "壶铃")):
        if label in raw and any(word in raw for word in ("没有", "没带", "坏了", "不能用", "被占用")):
            unavailable.append(code)
    return {
        "equipment_busy": busy,
        "unavailable_equipment": unavailable,
    }


def constraint_from_text(text: str) -> dict:
    location, no_gym = parse_location(text)
    constraints = {
        "minutes": parse_minutes(text),
        "location": location,
        "no_gym": no_gym,
        "rest": any(word in text for word in ("今天想休息", "想休息", "休息一下", "休息吧", "休息",
                                             "不想练", "歇一天", "今天不练", "休息一天", "太累了不想动")),
        "easier": any(word in text for word in ("简单点", "轻一点", "减少", "少一点",
                                                "太累", "有点累", "吃不消", "降一点")),
        "harder": any(word in text for word in ("加强度", "加量", "不够", "太轻松", "加一点")),
        "injured_regions": [],
    }
    from app.domain.safety_rules import detect_regions

    if any(word in text for word in ("疼", "痛", "不舒服", "受伤", "拉伤", "扭到", "不适")):
        constraints["injured_regions"] = detect_regions(text)
    constraints.update(parse_equipment_constraints(text))
    # 「今天晚上有空」只写当天时间窗口，不写长期偏好
    window = time_window_of(text)
    if window and has_temp_signal(text) and not has_long_term_signal(text):
        constraints["preferred_window"] = window
    return constraints


def constraint_summary(constraints: dict) -> list[str]:
    items: list[str] = []
    if constraints.get("minutes"):
        items.append(f"可训练 {constraints['minutes']} 分钟")
    if constraints.get("location"):
        items.append(f"地点：{constraints['location']}")
    if constraints.get("rest"):
        items.append("今天休息")
    if constraints.get("easier"):
        items.append("强度下调")
    if constraints.get("injured_regions"):
        items.append("避开 " + "、".join(constraints["injured_regions"]))
    if constraints.get("equipment_busy"):
        items.append("有器械被占用")
    return items


def movement_side_of(action: dict) -> str | None:
    return movement_side(str(action.get("action_ref") or ""))
