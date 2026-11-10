"""Profile 与 Agent Memory 的单一事实来源。

职责分工（本次升级后的硬约定）：

| 层 | 负责 |
| --- | --- |
| Profile | 目标、经验、时间偏好、地点、器械、喜恶 —— **结构化事实的唯一来源** |
| Workout History | 真实训练记录、完成率、时长、难度、未完成原因 |
| Agent Memory | 历史经历、偏好形成原因、长期行为总结、跨能力的语义背景 |
| Temporary Context | 今天的时间/地点/器械/身体状态，只对当天有效 |

因此：
- 用户明确表达长期偏好变化 → 先改 Profile，再把结果同步成一条 `canonical` Memory；
- 临时要求 → 只写 Temporary Context，绝不改 Profile；
- 范围模糊且与现有 Profile 冲突 → 不写入，返回一个问题让用户确认。

Memory 可以保留旧值做镜像/历史，但 Agent 读取时以 Profile 为准。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.memory import (
    CANONICAL_CATEGORIES,
    GOAL_LABELS,
    LOCATION_LABELS,
    EQUIPMENT_LABELS,
    LONG_TERM,
    TIME_WINDOWS,
    has_long_term_signal,
    has_temp_signal,
    record_memory_revision,
    upsert_memory,
)
from app.models import Memory, Profile, User

GOAL_CODES = {label: code for code, label in GOAL_LABELS.items()}
LOCATION_CODES = {label: code for code, label in LOCATION_LABELS.items()}
EQUIPMENT_CODES = {label: code for code, label in EQUIPMENT_LABELS.items()}

PROFILE_FIELD = {
    "goal": "goal",
    "schedule": "preferred_time_window",
    "location": "preferred_location",
    "equipment": "equipment",
    "session_minutes": "session_minutes",
    "weekly_days": "weekly_days",
    "experience": "training_experience",
    "weight_kg": "weight_kg",
    "height_cm": "height_cm",
    "birth_year": "birth_year",
    "sex": "sex",
    "like": "likes",
    "dislike": "dislikes",
}


@dataclass
class PreferenceApplyResult:
    saved: list[Memory] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    needs_confirmation: bool = False
    question: str = ""
    conflicts: list[dict] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _display_location(code: str) -> str:
    return LOCATION_LABELS.get(code, code or "")


def _desired(item: dict):
    category = item.get("category")
    structured = item.get("structured") or {}
    if category == "goal":
        return structured.get("goal") or GOAL_CODES.get(item.get("value") or "")
    if category == "schedule":
        window = structured.get("window")
        if not window:
            match = re.search(r"(\d{1,2}:\d{2})\s*[-~至到]\s*(\d{1,2}:\d{2})", item.get("value") or "")
            window = f"{match.group(1)}-{match.group(2)}" if match else ""
        return window
    if category == "location":
        return structured.get("location") or LOCATION_CODES.get(item.get("value") or "")
    if category == "equipment":
        return list(structured.get("items") or [])
    if category in ("session_minutes", "weekly_days"):
        try:
            return int(structured.get("value") or re.sub(r"\D", "", item.get("value") or ""))
        except (TypeError, ValueError):
            return None
    if category == "experience":
        return structured.get("value") or item.get("value")
    if category in ("weight_kg", "height_cm", "birth_year", "sex"):
        return structured.get("value")
    if category in ("like", "dislike"):
        return item.get("key") or item.get("value")
    return item.get("value")


def _current(profile: Profile, item: dict):
    category = item.get("category")
    if category == "goal":
        return profile.goal
    if category == "schedule":
        return profile.preferred_time_window
    if category == "location":
        return profile.preferred_location
    if category == "equipment":
        return list(profile.equipment or [])
    if category == "session_minutes":
        return profile.session_minutes
    if category == "weekly_days":
        return profile.weekly_days
    if category == "experience":
        return profile.training_experience
    if category == "weight_kg":
        return profile.weight_kg
    if category == "height_cm":
        return profile.height_cm
    if category == "birth_year":
        return profile.birth_year
    if category == "sex":
        return profile.sex
    if category == "like":
        return list(profile.likes or [])
    if category == "dislike":
        return list(profile.dislikes or [])
    return None


def _values_equal(current, desired) -> bool:
    if isinstance(desired, list):
        return set(map(str, current or [])) == set(map(str, desired))
    if isinstance(current, list):
        return str(desired) in {str(item) for item in current}
    return str(current or "") == str(desired or "")


def _is_explicit_long_term(text: str, item: dict) -> bool:
    if has_temp_signal(text) and not has_long_term_signal(text):
        return False
    if has_long_term_signal(text):
        return True
    # 「我不喜欢深蹲」没有"以后"，但语义就是稳定偏好；目标同样
    return item.get("category") in ("goal", "dislike", "like")


def detect_conflicts(profile: Profile, candidates: list[dict], text: str) -> list[dict]:
    """检查新表达是否与 Profile 中的结构化事实冲突。"""
    conflicts: list[dict] = []
    for item in candidates:
        category = item.get("category")
        if category not in PROFILE_FIELD:
            continue
        desired = _desired(item)
        if desired in (None, "", []):
            continue
        current = _current(profile, item)
        if _values_equal(current, desired):
            continue
        if has_temp_signal(text) and not has_long_term_signal(text):
            # 临时表达即使抽到了候选，也不能覆盖长期档案
            conflicts.append({
                "category": category,
                "current": current,
                "desired": desired,
                "scope": "temporary",
                "explicit": False,
            })
            continue
        conflicts.append({
            "category": category,
            "current": current,
            "desired": desired,
            "scope": "long_term" if _is_explicit_long_term(text, item) else "ambiguous",
            "explicit": _is_explicit_long_term(text, item),
        })
    return conflicts


def _conflict_question(conflicts: list[dict]) -> str:
    if not conflicts:
        return ""
    item = conflicts[0]
    category = item["category"]
    label = {
        "schedule": "训练时间",
        "location": "训练地点",
        "goal": "训练目标",
        "session_minutes": "单次训练时长",
        "weekly_days": "每周训练天数",
        "equipment": "可用器械",
        "experience": "训练经验",
    }.get(category, "长期偏好")
    old = item.get("current")
    new = item.get("desired")
    if category == "schedule":
        old = str(old or "未设置")
        new = str(new or "未设置")
    elif category == "location":
        old = _display_location(str(old or ""))
        new = _display_location(str(new or ""))
    return (
        f"你原来的{label}是「{old}」，这次说「{new}」。"
        "这是以后都按新的来，还是只调整今天/这一次？"
    )


def _apply_to_profile(profile: Profile, item: dict) -> str | None:
    category = item.get("category")
    structured = item.get("structured") or {}
    desired = _desired(item)
    if category == "goal" and desired:
        profile.goal = str(desired)
        return f"目标改为「{GOAL_LABELS.get(str(desired), str(desired))}」"
    if category == "schedule" and desired:
        profile.preferred_time_window = str(desired)
        profile.preferred_time_label = str(structured.get("label") or item.get("value") or "")
        return f"训练时间改为「{profile.preferred_time_label}（{desired}）」"
    if category == "location" and desired:
        profile.preferred_location = str(desired)
        return f"训练地点改为「{_display_location(str(desired))}」"
    if category == "equipment" and isinstance(desired, list):
        supported = {"dumbbell", "band", "bench", "treadmill", "barbell", "mat",
                     "bodyweight", "kettlebell"}
        items = [str(code) for code in desired if str(code) in supported]
        if items:
            profile.equipment = items
            return "可用器械已同步到档案"
    if category == "session_minutes" and desired:
        try:
            minutes = int(desired)
            if 10 <= minutes <= 180:
                profile.session_minutes = minutes
                return f"单次训练时长改为 {minutes} 分钟"
        except (TypeError, ValueError):
            pass
    if category == "weekly_days" and desired:
        try:
            days = int(desired)
            if 1 <= days <= 7:
                profile.weekly_days = days
                return f"每周训练天数改为 {days} 天"
        except (TypeError, ValueError):
            pass
    if category == "experience" and desired:
        profile.training_experience = str(desired)
        return "训练经验已同步到档案"
    if category == "weight_kg" and desired:
        profile.weight_kg = float(desired)
        return f"体重改为 {profile.weight_kg} kg"
    if category == "height_cm" and desired:
        profile.height_cm = float(desired)
        return f"身高改为 {profile.height_cm} cm"
    if category == "birth_year" and desired:
        profile.birth_year = int(desired)
        return f"出生年改为 {profile.birth_year}"
    if category == "sex" and desired:
        profile.sex = str(desired)
        return "性别已同步到档案"
    if category in ("like", "dislike") and desired:
        field = "likes" if category == "like" else "dislikes"
        values = list(getattr(profile, field) or [])
        if desired not in values:
            values.append(str(desired))
            setattr(profile, field, values)
            return f"{'喜欢' if category == 'like' else '不喜欢'}「{desired}」已记录"
    return None


def _canonical_memory_value(profile: Profile, category: str) -> tuple[str, dict]:
    if category == "goal":
        label = GOAL_LABELS.get(profile.goal, profile.goal)
        return label, {"goal": profile.goal, "canonical": True, "profile_field": "goal"}
    if category == "schedule":
        window = profile.preferred_time_window or ""
        label = profile.preferred_time_label or "训练时间"
        return (f"{label}（{window}）" if window else ""), {
            "window": window, "label": label, "canonical": True,
            "profile_field": "preferred_time_window",
        }
    if category == "location":
        code = profile.preferred_location or ""
        return _display_location(code), {
            "location": code, "canonical": True, "profile_field": "preferred_location",
        }
    if category == "equipment":
        codes = [str(item) for item in (profile.equipment or [])]
        return "、".join(EQUIPMENT_LABELS.get(code, code) for code in codes), {
            "items": codes, "canonical": True, "profile_field": "equipment",
        }
    if category == "session_minutes":
        return f"每次 {profile.session_minutes} 分钟", {
            "value": profile.session_minutes, "canonical": True,
            "profile_field": "session_minutes",
        }
    if category == "weekly_days":
        return f"每周 {profile.weekly_days} 天", {
            "value": profile.weekly_days, "canonical": True,
            "profile_field": "weekly_days",
        }
    if category == "experience":
        return str(profile.training_experience), {
            "value": profile.training_experience, "canonical": True,
            "profile_field": "training_experience",
        }
    return "", {"canonical": True}


def sync_memory_from_profile(db: Session, user: User, profile: Profile, category: str,
                             *, source: str = "profile_sync") -> Memory | None:
    value, structured = _canonical_memory_value(profile, category)
    if not value:
        return None
    key = {
        "goal": "primary_goal",
        "schedule": "preferred_time",
        "location": "preferred_location",
        "equipment": "available",
        "session_minutes": "session_minutes",
        "weekly_days": "weekly_days",
        "experience": "level",
    }.get(category, category)
    return upsert_memory(
        db, user,
        kind=LONG_TERM,
        category=category,
        key=key,
        value=value,
        structured=structured,
        source=source,
        evidence="由 Profile 同步",
        confidence=1.0,
        importance=0.9,
        expires_at=None,
        last_confirmed_at=_now(),
        reason=f"profile_sync:{category}",
    )


def apply_preference_candidates(
    db: Session,
    user: User,
    candidates: list[dict],
    text: str,
    *,
    evidence: str = "",
) -> PreferenceApplyResult:
    """明确的长期偏好：更新 Profile，并把结果同步为 canonical Memory。"""
    profile = db.get(Profile, user.id)
    if profile is None:
        return PreferenceApplyResult(needs_confirmation=False)
    conflicts = detect_conflicts(profile, candidates, text)

    # 临时表达与长期档案冲突：不写长期，交给 Temporary Context 路径
    temporary_conflicts = [item for item in conflicts if item["scope"] == "temporary"]
    if temporary_conflicts:
        return PreferenceApplyResult(
            conflicts=conflicts,
            changes=["这是临时条件，只影响今天，不改长期档案"],
        )

    ambiguous = [item for item in conflicts if item["scope"] == "ambiguous"]
    if ambiguous:
        return PreferenceApplyResult(
            conflicts=conflicts,
            needs_confirmation=True,
            question=_conflict_question(ambiguous),
        )

    result = PreferenceApplyResult(conflicts=conflicts)
    for item in candidates:
        category = item.get("category")
        if category in PROFILE_FIELD:
            change = _apply_to_profile(profile, item)
            if change:
                result.changes.append(change)
            if category in ("like", "dislike"):
                desired = _desired(item)
                if desired:
                    row = upsert_memory(
                        db, user,
                        kind=LONG_TERM,
                        category=category,
                        key=str(desired),
                        value=(
                            f"喜欢{desired}" if category == "like"
                            else f"不喜欢{desired}"
                        ),
                        structured={"action": str(desired), "canonical": True,
                                    "profile_field": "likes" if category == "like" else "dislikes"},
                        source="profile_sync",
                        evidence=evidence,
                        confidence=1.0,
                        importance=0.8,
                        expires_at=None,
                        reason=f"profile_sync:{category}",
                    )
                    result.saved.append(row)
            else:
                row = sync_memory_from_profile(db, user, profile, category)
                if row is not None:
                    result.saved.append(row)
        else:
            # 非结构化记忆：经历、行为、备注，Profile 不参与
            result.saved.append(upsert_memory(
                db, user,
                kind=LONG_TERM,
                category=category or "note",
                key=item.get("key") or "value",
                value=item.get("value") or "",
                structured=item.get("structured") or {},
                source="chat",
                evidence=evidence,
                confidence=0.85,
                importance=0.6,
                expires_at=None,
            ))
    profile.updated_at = _now()
    db.flush()
    return result


def apply_memory_to_profile(db: Session, user: User, row: Memory) -> None:
    """用户直接编辑 canonical Memory 时，反向同步 Profile。

    编辑的 value 优先于旧 structured：否则改了"20:00"仍会被旧的 window 覆盖。
    """
    profile = db.get(Profile, user.id)
    if profile is None:
        return
    category = row.category
    text = row.value or ""
    structured = dict(row.structured or {})
    item = {"category": category, "key": row.key, "value": text, "structured": structured}
    if category == "schedule":
        match = re.search(r"(\d{1,2}:\d{2})\s*[-~至到]\s*(\d{1,2}:\d{2})", text)
        if match:
            item["structured"] = {
                **structured, "window": f"{match.group(1)}-{match.group(2)}",
                "label": structured.get("label") or text,
            }
        elif re.search(r"\d{1,2}:\d{2}", text):
            start = re.search(r"(\d{1,2}:\d{2})", text).group(1)
            hour, minute = start.split(":")
            item["structured"] = {
                **structured, "window": f"{start}-{(int(hour) + 1) % 24:02d}:{minute}",
                "label": structured.get("label") or text,
            }
        else:
            for label, window in TIME_WINDOWS.items():
                if label in text:
                    item["structured"] = {
                        **structured, "window": f"{window[0]}-{window[1]}", "label": label,
                    }
                    break
    elif category == "goal":
        for label, code in GOAL_CODES.items():
            if label in text:
                item["structured"] = {**structured, "goal": code}
                break
    elif category == "location":
        for label, code in LOCATION_CODES.items():
            if label in text:
                item["structured"] = {**structured, "location": code}
                break
        else:
            if text in LOCATION_CODES.values():
                item["structured"] = {**structured, "location": text}
    elif category == "session_minutes":
        digits = re.sub(r"\D", "", text)
        if digits:
            item["structured"] = {**structured, "value": int(digits)}
    elif category == "weekly_days":
        digits = re.sub(r"\D", "", text)
        if digits:
            item["structured"] = {**structured, "value": int(digits)}
    elif category == "experience":
        if "新手" in text or "beginner" in text.lower():
            item["structured"] = {**structured, "value": "beginner"}
        elif "有经验" in text or "intermediate" in text.lower():
            item["structured"] = {**structured, "value": "intermediate"}
        elif "高级" in text or "advanced" in text.lower():
            item["structured"] = {**structured, "value": "advanced"}
    elif category == "equipment":
        labels = [label for label in EQUIPMENT_CODES if label in text]
        if labels:
            item["structured"] = {
                **structured,
                "items": [EQUIPMENT_CODES[label] for label in labels],
            }
    elif category in ("like", "dislike"):
        item["key"] = structured.get("action") or row.key

    _apply_to_profile(profile, item)
    profile.updated_at = _now()
    row.structured = {
        **structured,
        **(item.get("structured") or {}),
        "canonical": True,
    }
    db.flush()


def canonical_categories() -> set[str]:
    return set(CANONICAL_CATEGORIES) & set(PROFILE_FIELD)


def reconcile_profile_and_memory(db: Session, user: User) -> int:
    """启动/每日维护：用 Profile 校准 canonical Memory，并回填旧数据。

    规则：Profile 是事实来源。对新建的 time/location/likes 字段，如果 Profile
    为空而旧 Memory 有值，先回填 Profile；之后以 Profile 为准同步 Memory。
    """
    profile = db.get(Profile, user.id)
    if profile is None:
        return 0

    rows = list(db.scalars(
        select(Memory).where(
            Memory.user_id == user.id,
            Memory.kind == LONG_TERM,
            Memory.status == "active",
        )
    ).all())
    by_category: dict[str, list[Memory]] = {}
    for row in rows:
        by_category.setdefault(row.category, []).append(row)

    # 旧数据回填：只填新字段，避免覆盖已有画像
    if not profile.preferred_time_window:
        row = (by_category.get("schedule") or [None])[0]
        if row is not None:
            profile.preferred_time_window = str((row.structured or {}).get("window") or "")
            profile.preferred_time_label = str((row.structured or {}).get("label") or row.value or "")
    if not profile.preferred_location:
        row = (by_category.get("location") or [None])[0]
        if row is not None:
            profile.preferred_location = str((row.structured or {}).get("location") or "")
    if not profile.likes:
        profile.likes = [row.key for row in (by_category.get("like") or [])]

    synced = 0
    for category in ("like", "dislike"):
        desired = list(profile.likes or []) if category == "like" else list(profile.dislikes or [])
        for key in desired:
            upsert_memory(
                db, user,
                kind=LONG_TERM,
                category=category,
                key=str(key),
                value=(f"喜欢{key}" if category == "like" else f"不喜欢{key}"),
                structured={"action": str(key), "canonical": True,
                            "profile_field": "likes" if category == "like" else "dislikes"},
                source="profile_sync",
                evidence="由 Profile 同步",
                confidence=1.0,
                importance=0.8,
                expires_at=None,
                reason=f"profile_sync:{category}",
            )
            synced += 1
    for category in canonical_categories() - {"like", "dislike"}:
        value, _ = _canonical_memory_value(profile, category)
        if not value:
            continue
        existing = by_category.get(category) or []
        # 只有"用户曾表达过"或该字段非默认值时才镜像，避免给每个新用户灌一堆默认记忆
        non_default = {
            "goal": profile.goal != "fat_loss",
            "schedule": bool(profile.preferred_time_window),
            "location": bool(profile.preferred_location),
            "equipment": bool(profile.equipment),
            "session_minutes": profile.session_minutes != 45,
            "weekly_days": profile.weekly_days != 3,
            "experience": profile.training_experience != "beginner",
            "like": bool(profile.likes),
            "dislike": bool(profile.dislikes),
        }.get(category, False)
        if not existing and not non_default:
            continue
        try:
            sync_memory_from_profile(db, user, profile, category)
            synced += 1
        except Exception:  # noqa: BLE001
            continue
    db.flush()
    return synced


def _supersede_canonical(db: Session, user: User, category: str, key: str | None = None) -> int:
    """Profile 清空/删除某项时，canonical Memory 也失效，避免 Agent 读到旧值。"""
    query = select(Memory).where(
        Memory.user_id == user.id,
        Memory.kind == LONG_TERM,
        Memory.category == category,
        Memory.status == "active",
    )
    if key is not None:
        query = query.where(Memory.key == key)
    changed = 0
    for row in db.scalars(query).all():
        old_value, old_status = row.value or "", row.status
        row.status = "superseded"
        row.updated_at = _now()
        db.add(row)
        record_memory_revision(
            db, row,
            old_value=old_value, new_value=row.value,
            old_status=old_status, new_status="superseded",
            source="profile_sync", reason=f"profile_cleared:{category}",
        )
        changed += 1
    db.flush()
    return changed


def sync_profile_change(db: Session, user: User, profile: Profile,
                        changed_fields: list[str]) -> list[str]:
    """用户在「我的」表单直接改档案时，把变化同步成 canonical Memory。

    与 Agent 对话共用同一套规则：Profile 是事实来源；清空字段要让旧 Memory 失效，
    不能让下一次 reconcile 又把它从历史 Memory 里回填回来。
    """
    categories: set[str] = set()
    for field in changed_fields:
        if field == "goal":
            categories.add("goal")
        elif field in ("weekly_days", "session_minutes", "equipment", "training_experience"):
            categories.add({
                "weekly_days": "weekly_days",
                "session_minutes": "session_minutes",
                "equipment": "equipment",
                "training_experience": "experience",
            }[field])
        elif field in ("preferred_time_window", "preferred_time_label"):
            categories.add("schedule")
        elif field == "preferred_location":
            categories.add("location")
        elif field in ("likes", "dislikes"):
            categories.add("like" if field == "likes" else "dislike")

    changes: list[str] = []
    for category in categories:
        if category in ("like", "dislike"):
            field = "likes" if category == "like" else "dislikes"
            current = list(getattr(profile, field) or [])
            rows = list(db.scalars(
                select(Memory).where(
                    Memory.user_id == user.id,
                    Memory.kind == LONG_TERM,
                    Memory.category == category,
                    Memory.status == "active",
                )
            ).all())
            for row in rows:
                if row.key not in current:
                    _supersede_canonical(db, user, category, row.key)
            for key in current:
                upsert_memory(
                    db, user,
                    kind=LONG_TERM, category=category, key=str(key),
                    value=("喜欢" if category == "like" else "不喜欢") + str(key),
                    structured={"action": str(key), "canonical": True,
                                "profile_field": field},
                    source="profile_sync", evidence="用户在档案页修改",
                    confidence=1.0, importance=0.8, expires_at=None,
                    reason=f"profile_form:{category}",
                )
            changes.append(f"{field} 已同步")
            continue

        value, _ = _canonical_memory_value(profile, category)
        if not value:
            _supersede_canonical(db, user, category)
            changes.append(f"{category} 已清空，旧记忆已失效")
            continue
        if sync_memory_from_profile(db, user, profile, category) is not None:
            changes.append(f"{category} 已同步到记忆")
    return changes


def clear_profile_for_memory(db: Session, user: User, row: Memory) -> None:
    """删除 canonical 记忆时同步清空 Profile，避免每日 reconcile 又把镜像写回来。"""
    if not (row.structured or {}).get("canonical") and row.category not in PROFILE_FIELD:
        return
    profile = db.get(Profile, user.id)
    if profile is None:
        return
    if row.category == "goal":
        profile.goal = "fat_loss"
    elif row.category == "schedule":
        profile.preferred_time_window = ""
        profile.preferred_time_label = ""
    elif row.category == "location":
        profile.preferred_location = ""
    elif row.category == "equipment":
        profile.equipment = []
    elif row.category == "session_minutes":
        profile.session_minutes = 45
    elif row.category == "weekly_days":
        profile.weekly_days = 3
    elif row.category == "experience":
        profile.training_experience = "beginner"
    elif row.category in ("like", "dislike"):
        field = "likes" if row.category == "like" else "dislikes"
        key = str((row.structured or {}).get("action") or row.key)
        setattr(profile, field, [item for item in (getattr(profile, field) or []) if str(item) != key])
    profile.updated_at = _now()
    db.flush()
