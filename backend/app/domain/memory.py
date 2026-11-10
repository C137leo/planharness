"""分层记忆：长期偏好 / 近期训练状态 / 临时上下文。

设计目标（对应产品上的三个坑）：

1. **不要把所有聊天都存成长期记忆**：只有稳定信息（目标、通常时间、地点、
   器械、喜恶、经验）才进 long_term；"今天只有 20 分钟"只进 TemporaryContext。
2. **不要让临时要求覆盖长期偏好**：写入前先判断句子里有没有长期信号
   （以后 / 通常 / 一直 / 每次 / 每周 …），没有就默认临时。
3. **不要让记忆只进不出**：这里的记忆都能在「我的」页面查看、编辑、删除；
   Agent 也可以被明确要求"忘掉这条"。

`extract_candidates` 是确定性规则，返回的是**候选**，由 Agent 决策层决定
要不要写、写 long_term 还是 temp。这样接模型时只要替换抽取器，写入策略不变。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import append_event
from app.domain.timeutil import as_utc, day_end_utc, user_now, user_today
from app.models import (
    Memory,
    MemoryRevision,
    Profile,
    TemporaryContext,
    User,
    WorkoutSession,
    new_id,
)

LONG_TERM = "long_term"
SHORT_TERM = "short_term"

# 短期记忆默认保留 30 天；真实训练历史在 WorkoutSession/WorkoutLog，不在此清理范围。
SHORT_TERM_RETENTION_DAYS = 30
# 临时上下文默认保留 14 天（7~30 天区间，结合当前产品频率取 14）
TEMPORARY_RETENTION_DAYS = 14
# 这些短期记忆有长期价值：到期前先提炼为 long_term 行为总结，再标记 expired
IMPORTANT_SHORT_TERM_CATEGORIES = {"training_state", "behavior", "session"}
CANONICAL_CATEGORIES = {
    "goal", "schedule", "location", "equipment", "like", "dislike",
    "experience", "session_minutes", "weekly_days",
}

CATEGORY_LABELS: dict[str, str] = {
    "goal": "长期目标",
    "schedule": "训练时间偏好",
    "location": "训练地点",
    "equipment": "器械条件",
    "like": "喜欢的动作",
    "dislike": "不喜欢的动作",
    "experience": "训练经验",
    "session_minutes": "单次训练时长",
    "weekly_days": "每周训练天数",
    "intensity": "强度偏好",
    "injury": "长期不适 / 禁忌",
    "training_state": "近期训练状态",
    "session": "最近一次训练",
    "behavior": "行为规律",
    "note": "其他偏好",
}

LONG_TERM_MARKERS = (
    "以后", "今后", "通常", "平时", "一般", "一直", "长期", "每次", "每天",
    "每周", "习惯", "总是", "我一般", "我通常",
)
TEMP_MARKERS = (
    "今天", "今晚", "今早", "这次", "现在", "临时", "只剩", "只有", "今天下午",
)

GOAL_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fat_loss", ("减脂", "减重", "瘦", "掉秤", "降体脂")),
    ("muscle_gain", ("增肌", "增重", "长肌肉")),
    ("cardio", ("心肺", "耐力", "有氧能力")),
    ("health", ("体态", "综合健康", "健康一点")),
)
GOAL_LABELS = {
    "fat_loss": "减脂",
    "muscle_gain": "增肌",
    "cardio": "心肺",
    "health": "综合健康",
}

LOCATION_LABELS = {
    "gym": "健身房",
    "home": "家里",
    "office": "办公室",
    "outdoor": "户外",
}

EQUIPMENT_LABELS = {
    "dumbbell": "哑铃",
    "band": "弹力带",
    "bench": "训练凳",
    "treadmill": "跑步机",
    "barbell": "杠铃",
    "mat": "瑜伽垫",
    "kettlebell": "壶铃",
    "bodyweight": "徒手",
}

TIME_WINDOWS = {
    "午休": ("12:00", "13:00"),
    "中午": ("12:00", "13:00"),
    "早上": ("07:00", "08:00"),
    "早晨": ("07:00", "08:00"),
    "上午": ("09:00", "10:00"),
    "下午": ("15:00", "16:00"),
    "晚上": ("19:00", "20:00"),
    "下班后": ("19:00", "20:00"),
    "晚饭后": ("20:00", "21:00"),
    "睡前": ("21:00", "22:00"),
}

CN_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}

# 只收常见动作：宁可漏记，也不要把"不要加量"误判成不喜欢某个动作
ACTION_TERMS: dict[str, tuple[str, ...]] = {
    "深蹲": ("深蹲", "squat"),
    "俯卧撑": ("俯卧撑", "pushup", "push-up"),
    "划船": ("划船", "row"),
    "硬拉": ("硬拉", "deadlift"),
    "卧推": ("卧推", "bench"),
    "弓步": ("弓步", "lunge"),
    "引体向上": ("引体", "pull-up", "pullup"),
    "平板支撑": ("平板支撑", "plank"),
    "臀桥": ("臀桥", "glute-bridge", "glute_bridge"),
    "跑步": ("跑步", "jog", "run"),
    "快走": ("快走", "walk"),
    "拉伸": ("拉伸", "stretch"),
    "波比": ("波比", "burpee"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def has_long_term_signal(text: str) -> bool:
    return any(marker in (text or "") for marker in LONG_TERM_MARKERS)


def has_temp_signal(text: str) -> bool:
    return any(marker in (text or "") for marker in TEMP_MARKERS)


# ---------------------------------------------------------------- 读写


def is_memory_expired(row: Memory, now: datetime | None = None) -> bool:
    # expired 是明确的终态；archived / superseded 仍可由管理接口按需查看
    if row.status == "expired":
        return True
    expires = as_utc(row.expires_at)
    if expires is None:
        return False
    return expires <= (as_utc(now) or _now())


def record_memory_revision(
    db: Session,
    row: Memory,
    *,
    old_value: str,
    new_value: str,
    old_status: str,
    new_status: str,
    source: str,
    reason: str = "",
) -> MemoryRevision | None:
    """只有真正发生变化才写历史，避免每次读取都产生噪音。"""
    if old_value == new_value and old_status == new_status:
        return None
    revision = MemoryRevision(
        id=new_id(),
        memory_id=row.id,
        user_id=row.user_id,
        category=row.category,
        key=row.key,
        old_value=(old_value or "")[:2000],
        new_value=(new_value or "")[:2000],
        old_status=old_status or "",
        new_status=new_status or "",
        source=source or "",
        reason=(reason or "")[:500],
    )
    db.add(revision)
    db.flush()
    return revision


def upsert_memory(
    db: Session,
    user: User,
    *,
    kind: str,
    category: str,
    key: str,
    value: str,
    structured: dict | None = None,
    source: str = "chat",
    evidence: str = "",
    confidence: float = 1.0,
    importance: float | None = None,
    expires_at: datetime | None = None,
    last_confirmed_at: datetime | None = None,
    audit: bool = True,
    reason: str = "",
) -> Memory:
    """同一 (kind, category, key) 覆盖更新；历史值写入 MemoryRevision。

    生命周期规则：
    - long_term：expires_at 永远为 None（除非调用方显式指定），不会因时间久而删除；
    - short_term：默认 now + 30 天；重要类别可延长或先由清理任务提炼；
    - 归档/过期的行再次写入时重新激活，保留原 created_at。
    """
    row = db.scalar(
        select(Memory).where(
            Memory.user_id == user.id,
            Memory.kind == kind,
            Memory.category == category,
            Memory.key == key,
        )
    )
    now = _now()
    if row is None:
        row = Memory(
            id=new_id(),
            user_id=user.id,
            kind=kind,
            category=category,
            key=key,
        )
        db.add(row)
        db.flush()
    old_value = row.value or ""
    old_status = row.status or "active"
    row.value = (value or "")[:2000]
    row.structured = structured or {}
    row.source = source
    row.evidence = (evidence or "")[:1000]
    row.confidence = max(0.0, min(1.0, float(confidence)))
    if importance is not None:
        row.importance = max(0.0, min(1.0, float(importance)))
    elif row.importance is None:
        row.importance = 0.5
    row.last_confirmed_at = as_utc(last_confirmed_at) or now
    if kind == LONG_TERM:
        # 长期记忆默认永久有效；显式传入 expires_at 时才允许过期
        row.expires_at = as_utc(expires_at)
    else:
        row.expires_at = as_utc(expires_at) or (now + timedelta(days=SHORT_TERM_RETENTION_DAYS))
    row.status = "active"
    row.updated_at = now
    db.flush()
    record_memory_revision(
        db, row,
        old_value=old_value,
        new_value=row.value,
        old_status=old_status,
        new_status=row.status,
        source=source,
        reason=reason or ("short_term_expiry_extended" if kind == SHORT_TERM else "memory_upsert"),
    )
    if audit and (old_value != row.value or old_status != "active"):
        append_event(
            db,
            f"user:{user.id}",
            "memory_updated",
            {"kind": kind, "category": category, "key": key,
             "previous_value": old_value[:160], "value": row.value[:160],
             "source": source, "status": row.status},
            actor_type="agent",
            actor_id="coach",
        )
    return row


def list_memories(
    db: Session,
    user: User,
    *,
    kind: str | None = None,
    include_archived: bool = False,
    include_expired: bool = False,
    now: datetime | None = None,
) -> list[Memory]:
    """默认只返回 active 且未过期的记忆；读取接口不依赖定时任务先跑。"""
    query = select(Memory).where(Memory.user_id == user.id)
    if kind:
        query = query.where(Memory.kind == kind)
    if not include_archived:
        query = query.where(Memory.status == "active")
    rows = db.scalars(query.order_by(Memory.updated_at.desc())).all()
    if include_expired:
        return list(rows)
    current = as_utc(now) or _now()
    return [row for row in rows if not is_memory_expired(row, current)]


def memory_payload(row: Memory) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "category": row.category,
        "category_label": CATEGORY_LABELS.get(row.category, row.category),
        "key": row.key,
        "value": row.value,
        "structured": row.structured or {},
        "source": row.source,
        "status": row.status,
        "confidence": row.confidence,
        "importance": getattr(row, "importance", 0.5),
        "last_confirmed_at": (
            as_utc(row.last_confirmed_at).isoformat() if getattr(row, "last_confirmed_at", None) else ""
        ),
        "expires_at": as_utc(row.expires_at).isoformat() if getattr(row, "expires_at", None) else "",
        "updated_at": as_utc(row.updated_at).isoformat() if row.updated_at else "",
        "canonical": bool((row.structured or {}).get("canonical")),
    }


def update_memory(
    db: Session, user: User, memory_id: str, *, value: str | None = None,
    structured: dict | None = None,
) -> Memory:
    row = db.get(Memory, memory_id)
    if row is None or row.user_id != user.id or row.status != "active":
        raise ValueError("memory not found")
    old_value = row.value or ""
    old_status = row.status
    if value is not None:
        row.value = value[:2000]
    if structured is not None:
        row.structured = structured
    row.source = "user_edit"
    row.last_confirmed_at = _now()
    if row.kind == SHORT_TERM:
        row.expires_at = _now() + timedelta(days=SHORT_TERM_RETENTION_DAYS)
    row.updated_at = _now()
    db.flush()
    record_memory_revision(
        db, row, old_value=old_value, new_value=row.value,
        old_status=old_status, new_status=row.status,
        source="user_edit", reason="用户手动编辑",
    )
    # 档案是结构化事实的唯一来源：编辑 canonical 记忆时同步 Profile
    if (row.structured or {}).get("canonical") or row.category in CANONICAL_CATEGORIES:
        try:
            from app.domain.profile_sync import apply_memory_to_profile

            apply_memory_to_profile(db, user, row)
        except Exception:  # noqa: BLE001 - 记忆编辑不能因档案同步失败而整体 500
            pass
    append_event(
        db, f"user:{user.id}", "memory_updated",
        {"memory_id": row.id, "category": row.category,
         "previous_value": old_value[:160], "value": row.value[:160]},
        actor_type="user", actor_id=user.id,
    )
    return row


def archive_memory(
    db: Session, user: User, memory_id: str, *, status: str = "archived",
) -> Memory:
    row = db.get(Memory, memory_id)
    if row is None or row.user_id != user.id or row.status != "active":
        raise ValueError("memory not found")
    old_value, old_status = row.value or "", row.status
    row.status = status
    row.updated_at = _now()
    db.flush()
    record_memory_revision(
        db, row, old_value=old_value, new_value=row.value,
        old_status=old_status, new_status=status,
        source="user" if status == "archived" else "lifecycle",
        reason="用户删除" if status == "archived" else "生命周期清理",
    )
    # canonical 记忆是 Profile 的镜像：删除镜像要同步清空档案字段，
    # 否则下一次 reconcile 会把它重新写回来，用户会看到"删了又出现"。
    if status == "archived":
        try:
            from app.domain.profile_sync import clear_profile_for_memory

            clear_profile_for_memory(db, user, row)
        except Exception:  # noqa: BLE001 - 删除记忆不能因档案同步失败而整体失败
            pass
    append_event(
        db, f"user:{user.id}", "memory_deleted",
        {"memory_id": row.id, "category": row.category, "key": row.key,
         "status": status},
        actor_type="user" if status == "archived" else "system",
        actor_id=user.id if status == "archived" else "lifecycle",
    )
    return row


def find_memories(db: Session, user: User, text: str) -> list[Memory]:
    """按内容模糊匹配记忆（用户说"忘掉午休那条"时用）。"""
    raw = (text or "").strip()
    tokens = [token for token in re.split(r"[，。；、\s（）()]+", raw) if len(token) >= 2]
    rows = list_memories(db, user)

    def fragments(value: str) -> list[str]:
        return [part for part in re.split(r"[，。；、\s（）()：:]+", value or "")
                if len(part) >= 2]

    hits = []
    for row in rows:
        row_parts = fragments(row.value) + fragments(row.key)
        if (row.value and row.value in raw) \
                or (row.key and row.key in raw) \
                or (CATEGORY_LABELS.get(row.category, "")
                    and CATEGORY_LABELS[row.category] in raw) \
                or any(token in (row.value or "") for token in tokens) \
                or any(token in (row.key or "") for token in tokens) \
                or any(part in raw for part in row_parts):
            hits.append(row)
    return hits


# ---------------------------------------------------------------- 上下文装配


def load_memory_context(db: Session, user: User) -> dict:
    """给 Agent / 计划生成器看的记忆摘要。

    **Profile 是结构化事实的唯一来源**：目标、时间、地点、器械、喜恶优先从
    Profile 读；Memory 只补充非结构化的经历、行为规律和旧数据的兼容值。
    这样即使 Memory 里还留着旧值，Agent 也不会拿它覆盖用户档案。
    """
    profile = db.get(Profile, user.id)
    rows = list_memories(db, user)
    by_category: dict[str, list[Memory]] = {}
    for row in rows:
        by_category.setdefault(row.category, []).append(row)

    def first(category: str) -> Memory | None:
        return (by_category.get(category) or [None])[0]

    goal_code = str(getattr(profile, "goal", "") or "")
    goal = GOAL_LABELS.get(goal_code, goal_code)

    preferred_windows: list[dict] = []
    profile_window = str(getattr(profile, "preferred_time_window", "") or "")
    if profile_window:
        preferred_windows.append({
            "window": profile_window,
            "label": str(getattr(profile, "preferred_time_label", "") or "训练时间"),
        })
    else:
        for row in by_category.get("schedule") or []:
            window = (row.structured or {}).get("window")
            if window and window not in [item.get("window") for item in preferred_windows]:
                preferred_windows.append({
                    "window": window,
                    "label": (row.structured or {}).get("label") or row.value,
                })

    equipment: list[str] = []
    for item in (getattr(profile, "equipment", None) or []):
        code = str(item)
        if code and code not in equipment:
            equipment.append(code)
    if not equipment:
        for row in by_category.get("equipment") or []:
            for item in (row.structured or {}).get("items") or []:
                if item not in equipment:
                    equipment.append(str(item))

    location_code = str(getattr(profile, "preferred_location", "") or "")
    if not location_code:
        loc_row = first("location")
        location_code = str((loc_row.structured or {}).get("location") if loc_row else "")
    location_label = LOCATION_LABELS.get(location_code, location_code)

    profile_dislikes = [str(item) for item in (getattr(profile, "dislikes", None) or [])]
    profile_likes = [str(item) for item in (getattr(profile, "likes", None) or [])]
    memory_dislikes = [row.key for row in (by_category.get("dislike") or [])]
    memory_likes = [row.key for row in (by_category.get("like") or [])]
    dislikes = list(dict.fromkeys(profile_dislikes + memory_dislikes))
    likes = list(dict.fromkeys(profile_likes + memory_likes))

    session_minutes = getattr(profile, "session_minutes", None)
    if session_minutes is None:
        session_minutes = _int_of(first("session_minutes"))
    weekly_days = getattr(profile, "weekly_days", None)
    if weekly_days is None:
        weekly_days = _int_of(first("weekly_days"))

    return {
        "long_term": [memory_payload(row) for row in rows if row.kind == LONG_TERM],
        "short_term": [memory_payload(row) for row in rows if row.kind == SHORT_TERM],
        "goal": goal,
        "goal_code": goal_code,
        "preferred_windows": preferred_windows,
        "location": location_label or location_code,
        "location_code": location_code,
        "equipment": equipment,
        "session_minutes": session_minutes,
        "weekly_days": weekly_days,
        "dislikes": dislikes,
        "likes": likes,
        "experience": str(getattr(profile, "training_experience", "") or ""),
        "intensity": first("intensity").value if first("intensity") else "",
        "injuries": [row.value for row in (by_category.get("injury") or [])],
        "behavior": [memory_payload(row) for row in (by_category.get("behavior") or [])],
        "canonical_source": "profile",
    }


def _int_of(row: Memory | None) -> int | None:
    if row is None:
        return None
    try:
        return int((row.structured or {}).get("value") or row.value)
    except (TypeError, ValueError):
        return None


def memory_preferences(context: dict) -> list[str]:
    """把记忆压成 skill 能读的偏好列表（进 long_term_preferences）。"""
    items: list[str] = []
    if context.get("preferred_windows"):
        labels = [f"{item['label']}（{item['window']}）" for item in context["preferred_windows"]]
        items.append("训练时间优先：" + "、".join(labels))
    if context.get("location"):
        items.append("训练地点：" + str(context["location"]))
    if context.get("equipment"):
        items.append("可用器械：" + "、".join(context["equipment"]))
    if context.get("dislikes"):
        items.append("不喜欢的动作：" + "、".join(context["dislikes"]))
    if context.get("likes"):
        items.append("喜欢的动作：" + "、".join(context["likes"]))
    if context.get("injuries"):
        items.append("长期不适：" + "、".join(context["injuries"]))
    if context.get("intensity"):
        items.append("强度偏好：" + str(context["intensity"]))
    return items


def memory_instruction(context: dict) -> str:
    """拼进计划生成 instruction 的自然语言摘要。"""
    items = memory_preferences(context)
    return "；".join(items)


# ---------------------------------------------------------------- 抽取


def _goal_of(text: str) -> tuple[str, str] | None:
    for code, keywords in GOAL_KEYWORDS:
        if any(word in text for word in keywords):
            return code, GOAL_LABELS[code]
    return None


def _equipment_mentions(text: str) -> tuple[list[str], list[str]]:
    """返回 (可用, 不可用)。不可用靠"没有/不能用/被占用"判定。"""
    available: list[str] = []
    unavailable: list[str] = []
    for code, label in EQUIPMENT_LABELS.items():
        if label not in text:
            continue
        around = text[max(0, text.find(label) - 6): text.find(label) + len(label) + 4]
        if any(word in around for word in ("没有", "不能用", "被占用", "有人用", "坏了", "没带")):
            unavailable.append(code)
        else:
            available.append(code)
    # "徒手 / 无器械"是明确的可用条件
    if "徒手" in text or "无器械" in text:
        if "bodyweight" not in available:
            available.append("bodyweight")
    return available, unavailable


def time_window_of(text: str) -> dict | None:
    """从原话里识别时间窗口；供临时上下文和长期偏好共用。"""
    return _schedule_of(text)


def _plus_minutes(clock: str, minutes: int) -> str:
    hour, minute = (int(part) for part in clock.split(":", 1))
    total = (hour * 60 + minute + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _schedule_of(text: str) -> dict | None:
    # 先识别明确的钟点，例如「20:00 之后」「19:00-20:00」；
    # 这类说法不在 TIME_WINDOWS 的“早上/午休/晚上”里，但用户会直接这么说。
    span = re.search(
        r"(\d{1,2})[:：](\d{2})\s*[-~至到]\s*(\d{1,2})[:：](\d{2})", text
    )
    if span:
        start = f"{int(span.group(1)):02d}:{span.group(2)}"
        end = f"{int(span.group(3)):02d}:{span.group(4)}"
        return {"window": f"{start}-{end}", "label": f"{start}-{end}"}
    after = re.search(r"(\d{1,2})[:：](\d{2})\s*(?:之后|以后|后)", text)
    if after:
        clock = f"{int(after.group(1)):02d}:{after.group(2)}"
        return {"window": f"{clock}-{_plus_minutes(clock, 90)}", "label": f"{clock} 之后"}
    clock = re.search(r"(\d{1,2})[:：](\d{2})", text)
    if clock:
        value = f"{int(clock.group(1)):02d}:{clock.group(2)}"
        return {"window": f"{value}-{_plus_minutes(value, 60)}", "label": value}
    for label, window in TIME_WINDOWS.items():
        if label in text:
            return {
                "window": f"{window[0]}-{window[1]}",
                "label": label,
            }
    return None


def _action_mentions(text: str) -> list[str]:
    found: list[str] = []
    lowered = (text or "").lower()
    for label, terms in ACTION_TERMS.items():
        if any(term.lower() in lowered for term in terms):
            found.append(label)
    return found


def extract_candidates(text: str) -> list[dict]:
    """从一句话里抽记忆候选。

    每条候选：
    {kind, category, key, value, structured, reason}
    kind 只给 long_term：临时上下文由 Agent 决策层写 TemporaryContext。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    long_term = has_long_term_signal(raw)
    temp = has_temp_signal(raw)
    candidates: list[dict] = []

    goal = _goal_of(raw)
    # 目标默认是长期信息；除非用户明确说"今天想练减脂"这种临时说法
    if goal and (long_term or not temp or "目标" in raw or "想" in raw and "今天" not in raw):
        code, label = goal
        candidates.append({
            "kind": LONG_TERM,
            "category": "goal",
            "key": "primary_goal",
            "value": label,
            "structured": {"goal": code},
            "reason": "用户表达了稳定的训练目标",
        })

    weight_match = re.search(r"体重[^\d]{0,8}(\d{2,3}(?:\.\d+)?)", raw)
    if weight_match:
        candidates.append({
            "kind": LONG_TERM, "category": "weight_kg", "key": "weight_kg",
            "value": f"体重 {weight_match.group(1)} kg",
            "structured": {"value": float(weight_match.group(1))},
            "reason": "用户修改体重",
        })
    height_match = re.search(r"身高[^\d]{0,8}(\d{3}(?:\.\d+)?)", raw)
    if height_match:
        candidates.append({
            "kind": LONG_TERM, "category": "height_cm", "key": "height_cm",
            "value": f"身高 {height_match.group(1)} cm",
            "structured": {"value": float(height_match.group(1))},
            "reason": "用户修改身高",
        })
    birth_match = re.search(r"(?:出生年|出生年份|生于)[^\d]{0,5}(19\d{2}|20\d{2})", raw)
    if birth_match:
        candidates.append({
            "kind": LONG_TERM, "category": "birth_year", "key": "birth_year",
            "value": f"出生年 {birth_match.group(1)}",
            "structured": {"value": int(birth_match.group(1))},
            "reason": "用户修改出生年",
        })
    if re.search(r"性别[^男女性别]{0,6}(男|女)", raw):
        sex = re.search(r"性别[^男女性别]{0,6}(男|女)", raw).group(1)
        candidates.append({
            "kind": LONG_TERM, "category": "sex", "key": "sex",
            "value": f"性别{sex}", "structured": {"value": "male" if sex == "男" else "female"},
            "reason": "用户修改性别",
        })

    days_match = re.search(r"(?:每周|一周)[^。；;\n]{0,8}?([1-7一二两三四五六七])\s*[天次]", raw)
    if days_match:
        digits = days_match.group(1)
        days = int(digits) if digits.isdigit() else CN_NUMBERS.get(digits, 0)
        if days:
            candidates.append({
                "kind": LONG_TERM,
                "category": "weekly_days",
                "key": "weekly_days",
                "value": f"每周 {days} 天",
                "structured": {"value": days},
                "reason": "用户明确给出每周训练天数",
            })

    minutes_match = re.search(
        r"(?:每次|单次|一次)[^。；;\n]{0,10}?(\d{1,3})\s*分钟", raw
    )
    if minutes_match:
        minutes = int(minutes_match.group(1))
        if 10 <= minutes <= 180:
            candidates.append({
                "kind": LONG_TERM,
                "category": "session_minutes",
                "key": "session_minutes",
                "value": f"每次 {minutes} 分钟",
                "structured": {"value": minutes},
                "reason": "用户给出单次训练时长",
            })

    schedule = _schedule_of(raw)
    if schedule and (long_term or any(word in raw for word in ("安排", "固定", "训练"))):
        if not temp or long_term:
            candidates.append({
                "kind": LONG_TERM,
                "category": "schedule",
                "key": "preferred_time",
                "value": f"{schedule['label']}（{schedule['window']}）",
                "structured": schedule,
                "reason": "用户表达训练时间偏好",
            })

    if any(word in raw for word in ("健身房", "在家", "家里", "居家", "办公室", "公司", "户外", "公园")):
        if long_term or not temp:
            if "办公室" in raw or "公司" in raw:
                code, label = "office", "办公室"
            elif "在家" in raw or "家里" in raw or "居家" in raw:
                code, label = "home", "家里"
            elif "户外" in raw or "公园" in raw:
                code, label = "outdoor", "户外"
            else:
                code, label = "gym", "健身房"
            candidates.append({
                "kind": LONG_TERM,
                "category": "location",
                "key": "preferred_location",
                "value": label,
                "structured": {"location": code},
                "reason": "用户表达常用训练地点",
            })

    available, _unavailable = _equipment_mentions(raw)
    if available and (long_term or not temp):
        labels = [EQUIPMENT_LABELS[item] for item in available]
        candidates.append({
            "kind": LONG_TERM,
            "category": "equipment",
            "key": "available",
            "value": "、".join(labels),
            "structured": {"items": available},
            "reason": "用户说明可用器械",
        })

    actions = _action_mentions(raw)
    if actions:
        dislike_signals = ("不喜欢", "讨厌", "别安排", "不要", "不做", "不想做", "受不了")
        like_signals = ("喜欢", "想做", "多做", "优先")
        if any(word in raw for word in dislike_signals):
            for name in actions:
                candidates.append({
                    "kind": LONG_TERM,
                    "category": "dislike",
                    "key": name,
                    "value": f"不喜欢{name}",
                    "structured": {"action": name},
                    "reason": "用户明确表达对动作的排斥",
                })
        elif any(word in raw for word in like_signals):
            for name in actions:
                candidates.append({
                    "kind": LONG_TERM,
                    "category": "like",
                    "key": name,
                    "value": f"喜欢{name}",
                    "structured": {"action": name},
                    "reason": "用户明确表达对动作的偏好",
                })

    if "新手" in raw or "零基础" in raw or "刚练" in raw:
        candidates.append({
            "kind": LONG_TERM,
            "category": "experience",
            "key": "level",
            "value": "新手",
            "structured": {"value": "beginner"},
            "reason": "用户说明训练经验",
        })
    elif "有经验" in raw or "练了" in raw and "年" in raw:
        candidates.append({
            "kind": LONG_TERM,
            "category": "experience",
            "key": "level",
            "value": "有训练经验",
            "structured": {"value": "intermediate"},
            "reason": "用户说明训练经验",
        })

    if long_term and any(word in raw for word in ("疼", "痛", "不舒服", "伤", "不能练")):
        from app.domain.safety_rules import detect_regions, labels_of

        regions = detect_regions(raw)
        if regions:
            candidates.append({
                "kind": LONG_TERM,
                "category": "injury",
                "key": ",".join(regions),
                "value": f"长期不适：{labels_of(regions)}",
                "structured": {"regions": regions},
                "reason": "用户表达长期不适 / 禁忌",
            })

    if any(word in raw for word in ("太累", "吃不消", "轻松点", "简单点")):
        if long_term:
            candidates.append({
                "kind": LONG_TERM,
                "category": "intensity",
                "key": "intensity",
                "value": "偏好轻一点 / 恢复优先",
                "structured": {"value": "easier"},
                "reason": "用户表达长期强度偏好",
            })

    # 去重：同一 category+key 只留一条
    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in candidates:
        key = (item["category"], item["key"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def apply_candidates(db: Session, user: User, candidates: list[dict], evidence: str) -> list[Memory]:
    saved: list[Memory] = []
    for item in candidates:
        if item.get("kind") != LONG_TERM:
            continue
        saved.append(upsert_memory(
            db, user,
            kind=LONG_TERM,
            category=item["category"],
            key=item["key"],
            value=item["value"],
            structured=item.get("structured") or {},
            source="chat",
            evidence=evidence,
            confidence=0.9,
            importance=0.8 if item.get("category") in CANONICAL_CATEGORIES else 0.6,
        ))
    return saved


# ---------------------------------------------------------------- 近期训练状态


def consecutive_incomplete(sessions: list[WorkoutSession]) -> int:
    """从最近一次训练往前数：完成率 < 60% 算"没完成"。"""
    count = 0
    for session in sessions:
        if session.status != "finished":
            continue
        if float(session.completion_pct or 0) >= 60:
            break
        count += 1
    return count


def recent_training_state(db: Session, user: User, *, limit: int = 5) -> dict:
    sessions = list(db.scalars(
        select(WorkoutSession)
        .where(WorkoutSession.user_id == user.id, WorkoutSession.status == "finished")
        .order_by(WorkoutSession.date.desc(), WorkoutSession.finished_at.desc())
        .limit(limit)
    ).all())
    incomplete = consecutive_incomplete(sessions)
    difficulties = [int(item.difficulty) for item in sessions if item.difficulty]
    completion = [float(item.completion_pct or 0) for item in sessions]
    state = {
        "sessions": len(sessions),
        "consecutive_incomplete": incomplete,
        "avg_completion_pct": round(sum(completion) / len(completion), 1) if completion else None,
        "avg_difficulty": round(sum(difficulties) / len(difficulties), 1) if difficulties else None,
        "last_date": sessions[0].date.isoformat() if sessions else "",
        "load_reduced": incomplete >= 2,
        "reason": (
            f"最近连续 {incomplete} 次训练完成率偏低，下一次计划应降低训练负担"
            if incomplete >= 2 else ""
        ),
    }
    if state["sessions"]:
        upsert_memory(
            db, user,
            kind=SHORT_TERM,
            category="training_state",
            key="consecutive_incomplete",
            value=str(incomplete),
            structured=state,
            source="feedback",
            importance=0.6,
            expires_at=_now() + timedelta(days=SHORT_TERM_RETENTION_DAYS),
            audit=False,
        )
    return state


# ---------------------------------------------------------------- 临时上下文


def _temp_effective(row: TemporaryContext) -> date:
    return row.effective_date or row.day


def set_temporary(
    db: Session,
    user: User,
    day: date,
    key: str,
    value: dict,
    *,
    text: str = "",
    source: str = "chat",
    status: str = "active",
) -> TemporaryContext:
    """写一天的临时上下文；按用户时区把失效时间落到本地当天结束。"""
    profile = db.get(Profile, user.id)
    effective = day
    row = db.scalar(
        select(TemporaryContext).where(
            TemporaryContext.user_id == user.id,
            TemporaryContext.day == day,
            TemporaryContext.key == key,
        )
    )
    if row is None:
        row = TemporaryContext(id=new_id(), user_id=user.id, day=day, key=key)
        db.add(row)
    row.effective_date = effective
    row.value = value or {}
    row.text = (text or "")[:1000]
    row.source = source
    row.status = status
    row.expires_at = day_end_utc(effective, profile)
    if status == "completed":
        row.completed_at = _now()
    db.flush()
    return row


def load_temporary(
    db: Session,
    user: User,
    day: date,
    *,
    include_completed: bool = False,
    include_expired: bool = False,
) -> dict[str, dict]:
    """读取某天的临时上下文。

    - 读取时就过滤 expired，不能只依赖每天一次的定时任务；
    - 默认只看 active；当天训练结束后相关上下文会标 completed，
      今日页仍可通过 include_completed 读取，但不会在其它日期生效。
    """
    profile = db.get(Profile, user.id)
    now = _now()
    rows = db.scalars(
        select(TemporaryContext).where(
            TemporaryContext.user_id == user.id,
            (TemporaryContext.effective_date == day)
            | ((TemporaryContext.effective_date.is_(None)) & (TemporaryContext.day == day)),
        )
    ).all()
    allowed = {"active"}
    if include_completed:
        allowed.add("completed")
    if include_expired:
        allowed.update({"expired", "reset", "migrated", "completed"})
    result: dict[str, dict] = {}
    for row in rows:
        if (row.status or "active") not in allowed:
            continue
        expires = as_utc(row.expires_at)
        if expires is not None and expires <= now:
            continue
        result[row.key] = row.value or {}
    return result


TEMPORARY_LABELS = {
    "available_minutes": "可用时间",
    "location": "训练地点",
    "equipment_busy": "器械占用",
    "discomfort": "身体状态",
    "time_window": "时间安排",
    "plan_override": "计划调整",
}


def list_temporary(
    db: Session,
    user: User,
    day: date,
    *,
    include_completed: bool = True,
) -> list[TemporaryContext]:
    """给「我的 → 教练记忆」用的临时上下文列表（含有效期/状态）。"""
    profile = db.get(Profile, user.id)
    now = _now()
    rows = list(db.scalars(
        select(TemporaryContext).where(
            TemporaryContext.user_id == user.id,
            (TemporaryContext.effective_date == day)
            | ((TemporaryContext.effective_date.is_(None)) & (TemporaryContext.day == day)),
        ).order_by(TemporaryContext.updated_at.desc())
    ).all())
    allowed = {"active", "completed"} if include_completed else {"active"}
    result = []
    for row in rows:
        if (row.status or "active") not in allowed:
            continue
        expires = as_utc(row.expires_at)
        if expires is not None and expires <= now and row.status != "completed":
            continue
        result.append(row)
    return result


def temporary_context_payload(row: TemporaryContext) -> dict:
    return {
        "id": row.id,
        "key": row.key,
        "label": TEMPORARY_LABELS.get(row.key, row.key),
        "value": row.value or {},
        "text": row.text,
        "status": row.status,
        "effective_date": (row.effective_date or row.day).isoformat()
        if (row.effective_date or row.day) else "",
        "expires_at": as_utc(row.expires_at).isoformat() if row.expires_at else "",
        "completed_at": as_utc(row.completed_at).isoformat() if row.completed_at else "",
        "source": row.source,
    }


def complete_temporary_for_day(db: Session, user: User, day: date) -> int:
    """用户主动结束当天训练后，把相关临时上下文标记为已完成。

    不删除原始记录：这是"当天为什么这么练"的证据；PlanAdjustment 仍保留。
    """
    rows = list(db.scalars(
        select(TemporaryContext).where(
            TemporaryContext.user_id == user.id,
            (TemporaryContext.effective_date == day)
            | ((TemporaryContext.effective_date.is_(None)) & (TemporaryContext.day == day)),
            TemporaryContext.status == "active",
        )
    ).all())
    now = _now()
    for row in rows:
        row.status = "completed"
        row.completed_at = now
        row.updated_at = now
        db.add(row)
    db.flush()
    return len(rows)


def clear_temporary(
    db: Session, user: User, day: date, key: str | None = None,
    *, status: str = "reset", hard: bool = False,
) -> int:
    """恢复/重置：默认标记 reset 保留历史，而不是物理删除。"""
    rows = list(db.scalars(
        select(TemporaryContext).where(
            TemporaryContext.user_id == user.id,
            (TemporaryContext.effective_date == day)
            | ((TemporaryContext.effective_date.is_(None)) & (TemporaryContext.day == day)),
        )
    ).all())
    removed = 0
    for row in rows:
        if key is not None and row.key != key:
            continue
        if hard:
            db.delete(row)
        else:
            row.status = status
            row.updated_at = _now()
            db.add(row)
        removed += 1
    db.flush()
    return removed


def temporary_payload(db: Session, user: User, day: date) -> dict:
    context = load_temporary(db, user, day, include_completed=True)
    return {
        "date": day.isoformat(),
        "constraints": context,
        "has_override": "plan_override" in context,
    }
