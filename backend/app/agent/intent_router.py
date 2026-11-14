"""分层意图理解：先判任务，再抽参数，最后才交给业务决策。

为什么拆成两段：
原来的 `classify_with_model` 让模型一次性输出 intent + scope + 所有 constraints，
任何字段缺失、JSON 截断或 intent 拼写不一致都会让整次理解被丢弃，用户看到
"没听出你要改哪一层"。这里按成熟 Agent 的做法拆开：

1. `understand()` 先只判断"用户想完成什么"（intent/scope/confidence）；
2. 需要参数的意图再单独抽 slots；抽不到就保留 intent，标记 missing_slots，
   由业务层做**有上下文的追问**，而不是把已经理解的任务丢掉；
3. 调用失败/JSON 不合法/意图不在白名单时，带上失败原因回退，而不是静默 None。

规则不在这里承担语义理解，只作为模型不可用时的 fallback（由调用方注入）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 规范意图：描述是给模型看的，不是把 label 拼成一行。
INTENT_SPECS: dict[str, dict] = {
    "create_plan": {
        "label": "建立 / 重排长期计划",
        "scope": "long_term",
        "description": "用户要一份新的长期训练计划，或明确要求重排现有长期计划。",
        "slots": [],
    },
    "adjust_today": {
        "label": "调整今天",
        "scope": "today",
        "description": "只改今天的训练安排，不影响长期计划。包括今天休息、缩短时长、"
                       "换地点、换器械、加减强度。",
        "slots": ["rest", "minutes", "location", "equipment_busy", "easier", "harder",
                  "injured_regions", "avoid_actions"],
    },
    "adjust_workout": {
        "label": "调整当前训练",
        "scope": "current_workout",
        "description": "用户正在训练，要改当前动作/剩余动作/组数/强度，或提前结束。",
        "slots": ["rest", "minutes", "equipment_busy", "easier", "harder",
                  "injured_regions", "avoid_actions", "exercise"],
    },
    "adjust_plan": {
        "label": "修改长期计划结构",
        "scope": "long_term",
        "description": "修改长期计划的结构、动作、训练日，需要先出补丁预览再确认。",
        "slots": ["avoid_actions", "weekly_days", "injured_regions"],
    },
    "update_preference": {
        "label": "更新长期偏好 / 档案",
        "scope": "long_term",
        "description": "修改长期有效的目标、训练时间、地点、器械、喜恶、身体数据。",
        "slots": ["goal", "weekly_days", "session_minutes", "location", "equipment",
                  "dislikes", "likes", "preferred_time_window", "weight_kg",
                  "height_cm", "birth_year", "sex"],
    },
    "injury_advice": {
        "label": "伤病与不适",
        "scope": "today",
        "description": "用户明确说某个身体部位疼痛、受伤、不适，需要安全建议/替代动作；"
                       "不是所有“今天不舒服”都属于这里，没有部位的全身不适按休息处理。",
        "slots": ["injured_regions", "avoid_actions"],
    },
    "nutrition_advice": {
        "label": "饮食建议",
        "scope": "message",
        "description": "问吃什么、外卖怎么调、热量/蛋白、某餐怎么安排。不是点餐下单。",
        "slots": [],
    },
    "exercise_guidance": {
        "label": "动作指导",
        "scope": "message",
        "description": "问某个动作怎么做、姿势、常见错误；可指代当前动作。",
        "slots": ["exercise"],
    },
    "query_memory": {
        "label": "查看记忆",
        "scope": "message",
        "description": "问教练记得什么、档案里有什么。",
        "slots": [],
    },
    "edit_memory": {
        "label": "删除/修改记忆",
        "scope": "long_term",
        "description": "要求忘掉、删除某条记忆。",
        "slots": [],
    },
    "today_brief": {
        "label": "今天的整体安排",
        "scope": "today",
        "description": "问今天练什么/吃什么，或要求生成今天的整体安排。",
        "slots": [],
    },
    "plan_overview": {
        "label": "查看每周安排",
        "scope": "long_term",
        "description": "要求把计划整理成一周可执行的安排，只读展示。",
        "slots": [],
    },
    "progress_review": {
        "label": "复盘进展",
        "scope": "message",
        "description": "看完成率、训练量、体重趋势等数据。",
        "slots": [],
    },
    "coach_review": {
        "label": "综合评估与建议",
        "scope": "long_term",
        "description": "结合档案、记忆、最近训练，给出下一步建议。",
        "slots": [],
    },
    "review_followup": {
        "label": "执行上一条建议",
        "scope": "today",
        "description": "用户用“就按你说的/开始吧/好的”接受上一条具体建议。",
        "slots": [],
    },
    "log_feedback": {
        "label": "记录训练反馈",
        "scope": "current_workout",
        "description": "报告完成/没完成/太累/难度等训练结果。",
        "slots": ["completed", "difficulty", "fatigue", "minutes"],
    },
    "mcd_order": {
        "label": "麦当劳点餐",
        "scope": "meal",
        "description": "明确要点麦当劳/汉堡外卖，需要城市、门店、餐品。",
        "slots": ["city", "store", "foods"],
    },
}

SCOPES = ("message", "current_workout", "today", "long_term", "meal", "none")

# 模型常见近义写法 → 规范 intent（只做兼容，不是关键词意图识别）
INTENT_ALIASES = {
    "injury": "injury_advice",
    "pain": "injury_advice",
    "nutrition": "nutrition_advice",
    "diet": "nutrition_advice",
    "guidance": "exercise_guidance",
    "exercise": "exercise_guidance",
    "memory": "query_memory",
    "preference": "update_preference",
    "profile": "update_preference",
    "plan": "adjust_plan",
    "training_adjust": "adjust_today",
    "workout_adjust": "adjust_workout",
    "rest": "adjust_today",
}

# 这些意图需要参数；抽不到时保留 intent，标记 missing_slots。
SLOT_REQUIRED: dict[str, tuple[str, ...]] = {
    "injury_advice": ("injured_regions",),
    "adjust_today": ("adjustment_type",),
    "adjust_workout": ("adjustment_type",),
    "mcd_order": ("store",),
    "exercise_guidance": (),  # 可以用当前动作兜底
}

SLOT_SCHEMA: dict[str, dict] = {
    "adjust_today": {
        "rest": "bool", "minutes": "int|null", "location": "home|gym|office|outdoor|null",
        "equipment_busy": "bool", "easier": "bool", "harder": "bool",
        "injured_regions": ["knee|lower_back|shoulder|wrist|ankle|elbow|neck|hip"],
        "avoid_actions": ["string"],
    },
    "adjust_workout": {
        "rest": "bool", "minutes": "int|null", "equipment_busy": "bool",
        "easier": "bool", "harder": "bool",
        "injured_regions": ["knee|lower_back|shoulder|wrist|ankle|elbow|neck|hip"],
        "avoid_actions": ["string"], "exercise": "string|null",
    },
    "adjust_plan": {
        "avoid_actions": ["string"], "weekly_days": "int|null",
        "injured_regions": ["knee|lower_back|shoulder|wrist|ankle|elbow|neck|hip"],
    },
    "update_preference": {
        "goal": "fat_loss|muscle_gain|cardio|health|null",
        "weekly_days": "int|null", "session_minutes": "int|null",
        "location": "home|gym|office|outdoor|null",
        "equipment": ["dumbbell|band|bench|treadmill|mat|barbell"],
        "dislikes": ["string"], "likes": ["string"],
        "liked_foods": ["string"], "disliked_foods": ["string"],
        "allergen_foods": ["string"], "avoid_foods": ["string"],
        "preferred_time_window": "HH:MM-HH:MM|null",
        "weight_kg": "float|null", "height_cm": "float|null",
        "birth_year": "int|null", "sex": "male|female|null",
    },
    "nutrition_advice": {
        "meal_slot": "breakfast|lunch|dinner|snack|all|null",
        "avoid_foods": ["string"], "from_food": "string|null",
        "to_food": "string|null", "replacement_food": "string|null",
        "foods": ["string"], "kcal": "int|null", "protein_g": "float|null",
        "note": "string|null",
    },
    "injury_advice": {
        "injured_regions": ["knee|lower_back|shoulder|wrist|ankle|elbow|neck|hip"],
        "avoid_actions": ["string"],
    },
    "exercise_guidance": {"exercise": "string|null"},
    "mcd_order": {"city": "string|null", "store": "string|null", "foods": ["string"]},
    "log_feedback": {
        "completed": "bool|null", "difficulty": "int|null",
        "fatigue": "int|null", "minutes": "int|null",
    },
}


KNOWN_SLOT_KEYS = {
    key for schema in SLOT_SCHEMA.values() for key in schema.keys()
} | {"adjustment_type"}


@dataclass
class IntentResult:
    intent: str
    scope: str = "none"
    confidence: float = 0.0
    reason: str = ""
    secondary_intents: list[str] = field(default_factory=list)
    user_goal: str = ""
    slots: dict = field(default_factory=dict)
    action: str = ""
    missing_slots: list[str] = field(default_factory=list)
    clarification_needed: bool = False
    context_reference: dict = field(default_factory=dict)
    source: str = "model"
    error: str = ""
    raw_stage1: str = ""
    raw_stage2: str = ""

    def trace_detail(self) -> dict:
        return {
            "intent": self.intent,
            "scope": self.scope,
            "confidence": round(self.confidence, 2),
            "reason": self.reason[:120],
            "secondary_intents": self.secondary_intents,
            "user_goal": self.user_goal[:120],
            "missing_slots": self.missing_slots,
            "clarification_needed": self.clarification_needed,
            "source": self.source,
            "error": self.error[:160],
        }


def _clean_json(content: str) -> dict:
    raw = (content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _preview(content: str, limit: int = 240) -> str:
    return " ".join((content or "").split())[:limit]


def _call(client, messages: list[dict], max_tokens: int) -> tuple[str, str]:
    """返回 (content, error)。错误分类型记录，方便排查是网络还是 JSON。"""
    if client is None or not getattr(client, "available", lambda: False)():
        return "", "client_unavailable"
    token_budget = max_tokens
    for attempt in range(2):
        try:
            kwargs = {"tools": None, "temperature": 0, "max_tokens": token_budget,
                      "reasoning_effort": "low",
                      "response_format": {"type": "json_object"}}
            try:
                reply = client.complete(messages, **kwargs)
            except TypeError:
                # 兼容不支持 response_format / reasoning_effort 的客户端
                kwargs.pop("response_format", None)
                kwargs.pop("reasoning_effort", None)
                reply = client.complete(messages, **kwargs)
            if reply.get("finish_reason") == "length":
                error = "output_truncated"
                token_budget = min(6400, token_budget * 2)
                logger.warning("semantic_output_truncated attempt=%s", attempt + 1)
                continue
            content = str((reply.get("message") or {}).get("content") or "").strip()
            if content:
                return content, ""
            error = "empty_content"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {str(exc)[:120]}"
        logger.warning("[Intent] model call failed attempt=%s error=%s", attempt + 1, error)
    return "", error


def _stage1_prompt() -> str:
    lines = [
        "你是健身 Agent 的自然语言理解模块，只做任务判断，不执行任何业务操作。",
        "结合用户原话、当前页面、训练状态、计划、档案和最近对话，判断用户真正想完成什么。",
        "用户可能使用口语、省略、指代、同义表达或错别字；不要因为没有命中某个词就判为无法理解。",
        "",
        "可选 intent（必须从中选一个，不能创造新 intent）：",
    ]
    for key, spec in INTENT_SPECS.items():
        lines.append(f"- {key}：{spec['description']}")
    lines += [
        "",
        f"scope 只能取：{' / '.join(SCOPES)}。",
        "判断规则：",
        "- 先判断用户的任务，再判断影响范围；不要因为出现动作名就改长期计划。",
        "- 用户说“第一个/第二个动作”时，以 today_plan.actions 或 current_workout.visible_items 的编号为准；热身不算第一个动作。",
        "- 训练中改当前动作/提前结束 → adjust_workout；非训练中只改今天 → adjust_today。",
        "- 有明确身体部位（膝/腰/肩等）的疼痛/受伤 → injury_advice；只有全身不舒服 → adjust_today/adjust_workout 的 rest。",
        "- 明确“以后/长期/每次” → long_term 偏好或计划；只说“今天” → today。",
        "- 能判断任务但缺少参数时，保留 intent，clarification_needed=true，missing_slots 填缺少的槽位，不要输出 clarify。",
        "- 只有完全无法推断用户目标时，才输出 intent=clarify（仅作为最后手段）。",
        "- 一句话可能包含多个目标：用 secondary_intents 列出次要任务，但 primary_intent 只能有一个，且安全优先。",
        "  例：腰疼 + 今天不想练腿 + 想练上半身 → primary=injury_advice，secondary=[adjust_today]。",
        "- 输出 user_goal：一句用户真正想完成的事，不要复述原话。",
        "",
        "只输出一个 JSON 对象，不要 Markdown、不要解释，字段：",
        '{"intent":"...","scope":"...","confidence":0.0,"reason":"...",'
        '"secondary_intents":[],"user_goal":"...",'
        '"clarification_needed":false,"missing_slots":[]}',
    ]
    return "\n".join(lines)


def _stage2_prompt(intent: str, operation: str = "") -> str:
    schema = SLOT_SCHEMA.get(intent, {})
    lines = [
        "你是健身 Agent 的参数抽取模块。只抽参数，不改变意图，不执行操作。",
        f"当前意图：{intent}；operation：{operation or '未指定'}",
    ]
    if operation == "add_action":
        lines += [
            "这是新增动作：从“练胸/练背/练腿/练肩/练手臂/练核心”抽 target_muscle，"
            "值用 chest/back/legs/shoulders/arms/core；不要要求 target_index。",
        ]
    if operation in ("skip_action", "remove_action"):
        lines += [
            "这是跳过/移除动作：抽 target_index 或 target_action；"
            "如果原文说的是“去掉拉伸”，target_action=拉伸。",
        ]
    if operation == "replace_meal_item":
        lines += [
            "这是替换餐食：抽 meal_slot（breakfast/lunch/dinner/snack）和 "
            "from_food/to_food/replacement_food；不要因为只给了餐次就追问。",
        ]
    if operation == "modify_meal_plan":
        lines += [
            "这是修改餐单：抽 meal_slot、avoid_foods、to_food/replacement_food。",
        ]
    if operation == "update_food_preference":
        lines += [
            "这是长期饮食偏好：喜欢→liked_foods，讨厌/不吃→disliked_foods，"
            "过敏→allergen_foods；不要写成训练动作 like/dislike。",
        ]
    if intent == "update_preference":
        lines += [
            "把长期偏好落到字段，不要只写一句 reason：",
            "- “以后别安排/不喜欢深蹲” → dislikes:[\"深蹲\"]",
            "- “我喜欢吃牛肉” → liked_foods:[\"牛肉\"]（不是训练动作 like）",
            "- “我不喜欢/不吃牛肉” → disliked_foods:[\"牛肉\"]",
            "- “我对牛肉过敏” → allergen_foods:[\"牛肉\"]",
            "- “以后在家练” → location:\"home\"",
            "- “目标改增肌” → goal:\"muscle_gain\"",
            "- “以后都晚上练” → preferred_time_window:\"19:00-20:00\"（按语义选时段）",
            "- “每周练 4 天/每次 30 分钟” → weekly_days/session_minutes",
            "- 不要把“今天”的临时要求写成长长期偏好。",
        ]
    lines += [
        "从用户原话和上下文里抽取槽位；没有就填 null / []，不要编造。",
        "调整类意图必须把语义落到 rest / harder / easier / minutes / location / equipment_busy：",
        "- 身体不舒服、不想继续、想结束 → rest=true",
        "- 状态好、还能练、想加量 → harder=true",
        "- 累但还想继续、想轻一点 → easier=true",
        "- 只提时间/地点/器械 → 对应字段",
        "- “练胸/练背/练腿/加一个动作” → add_action，target_muscle 填 chest/back/legs",
        "- 指代（第一个/第二个/刚才那个/这个动作）→ exercise 填上下文中对应动作名，"
        "context_reference.referenced_index 填 today_plan.actions / current_workout.visible_items 的编号；不要留空",
        "不要因为可选字段为空就 clarification_needed=true；只有完全没有调整方向时才 missing_slots=[\"adjustment_type\"]。",
        "如果缺少决定下一步所必需的参数，填 missing_slots，不要丢意图。",
        "只输出 JSON 对象，字段：",
        '{"slots":{...},"missing_slots":[],"clarification_needed":false,'
        '"context_reference":{"referenced_exercise":null,"referenced_index":null}}',
        "slots 可用字段（只输出与本意图相关的）：",
        json.dumps(schema, ensure_ascii=False),
    ]
    return "\n".join(lines)


def _normalize_intent(raw: str) -> str:
    intent = (raw or "").strip().lower()
    intent = INTENT_ALIASES.get(intent, intent)
    if intent in ("clarify", "safety"):
        return intent
    return intent if intent in INTENT_SPECS else ""


def understand(
    client,
    text: str,
    *,
    context: dict,
    fallback_intent: str = "clarify",
) -> IntentResult:
    """两阶段理解；失败时返回带 error 的结果，不静默返回 None。"""
    if client is None or not getattr(client, "available", lambda: False)():
        return IntentResult(fallback_intent, source="fallback", error="client_unavailable")

    stage1_messages = [
        {"role": "system", "content": _stage1_prompt()},
        {"role": "user", "content": json.dumps(
            {"text": text, "context": context}, ensure_ascii=False)},
    ]
    raw1, err1 = _call(client, stage1_messages, max_tokens=600)
    data1 = _clean_json(raw1)
    intent = _normalize_intent(str(data1.get("intent") or ""))
    if not intent:
        # 第二次是"修复请求"，不是把同一句话再问一遍。
        repair = stage1_messages + [
            {"role": "assistant", "content": raw1[:600]},
            {"role": "user", "content": (
                "上一次输出不合法或 intent 不在清单里。请只输出合法 JSON，"
                "intent 必须是清单中的一个；必要时用 clarify。"
            )},
        ]
        raw1b, err1b = _call(client, repair, max_tokens=600)
        data1 = _clean_json(raw1b) or data1
        intent = _normalize_intent(str(data1.get("intent") or ""))
        raw1 = raw1b or raw1
        err1 = err1b or err1
    if not intent:
        logger.warning("[Intent] invalid_stage1 error=%s raw=%s", err1, _preview(raw1))
        return IntentResult(
            fallback_intent, source="fallback",
            error=f"stage1_invalid:{err1 or 'invalid_intent'}",
            raw_stage1=_preview(raw1),
        )

    scope = str(data1.get("scope") or INTENT_SPECS.get(intent, {}).get("scope") or "none")
    if intent == "nutrition_advice" and scope == "meal":
        # 饮食建议不是点餐；模型常因为“汉堡”把 scope 判成 meal。
        scope = "message"
    if intent in ("query_memory", "progress_review"):
        scope = "message"
    if intent == "injury_advice":
        scope = "today"
    if intent == "clarify":
        scope = "none"
    if scope not in SCOPES:
        scope = INTENT_SPECS.get(intent, {}).get("scope", "none")
    try:
        confidence = float(data1.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.6
    reason = str(data1.get("reason") or "")[:160]
    user_goal = str(data1.get("user_goal") or "")[:160]
    secondary = []
    for raw in (data1.get("secondary_intents") or []):
        normalized = _normalize_intent(str(raw))
        if normalized and normalized not in (intent, "clarify", "safety"):
            secondary.append(normalized)
    missing = [str(item) for item in (data1.get("missing_slots") or [])
               if str(item).strip()]
    clarify = bool(data1.get("clarification_needed")) or intent == "clarify"

    result = IntentResult(
        intent=intent, scope=scope, confidence=max(0.0, min(1.0, confidence)),
        reason=reason, user_goal=user_goal, secondary_intents=list(dict.fromkeys(secondary)),
        missing_slots=missing, clarification_needed=clarify,
        source="model", error=err1, raw_stage1=_preview(raw1),
    )
    if intent in ("clarify", "safety"):
        return result

    # 第二阶段：只抽该意图需要的槽位；失败不丢意图。
    schema = SLOT_SCHEMA.get(intent)
    if schema:
        stage2_messages = [
            {"role": "system", "content": _stage2_prompt(intent, "")},
            {"role": "user", "content": json.dumps(
                {"text": text, "context": context, "intent": intent},
                ensure_ascii=False)},
        ]
        raw2, err2 = _call(client, stage2_messages, max_tokens=600)
        data2 = _clean_json(raw2)
        if not data2 and raw2:
            repair = stage2_messages + [
                {"role": "assistant", "content": raw2[:600]},
                {"role": "user", "content": "上一次不是合法 JSON，请只输出合法 JSON 对象。"},
            ]
            raw2b, err2b = _call(client, repair, max_tokens=600)
            data2 = _clean_json(raw2b)
            raw2 = raw2b or raw2
            err2 = err2b or err2
        result.raw_stage2 = _preview(raw2)
        if data2:
            slots = data2.get("slots")
            result.slots = slots if isinstance(slots, dict) else {}
            allowed_required = set(SLOT_REQUIRED.get(intent, ())) | {"adjustment_type"}
            more_missing = [str(item) for item in (data2.get("missing_slots") or [])
                            if str(item).strip() and str(item) in allowed_required]
            result.missing_slots = list(dict.fromkeys(result.missing_slots + more_missing))
            # clarify 是 stage1 的决策；stage2 只负责抽参数，不因为可选字段为空就翻案。
            ref = data2.get("context_reference")
            if isinstance(ref, dict):
                result.context_reference = ref
        else:
            result.error = (result.error + f"; stage2:{err2 or 'invalid_json'}").strip("; ")
            logger.warning("[Intent] stage2 failed intent=%s error=%s", intent, err2)
            result.clarification_needed = True

    # 只保留规范槽位名；模型有时会在 stage1 造词（如 intensity_level）。
    result.missing_slots = [item for item in result.missing_slots
                            if item in KNOWN_SLOT_KEYS]

    # stage2 已经抽到参数时，stage1 的“缺少方向”就被满足了：
    # 不能因为 stage1 提前写了 clarification_needed=true 而丢掉已经理解的任务。
    actionable_keys = ("rest", "minutes", "location", "equipment_busy",
                       "easier", "harder", "injured_regions", "avoid_actions",
                       "exercise")
    has_action = any(bool(result.slots.get(key)) for key in actionable_keys)
    if intent in ("adjust_today", "adjust_workout") and has_action:
        result.missing_slots = [item for item in result.missing_slots
                                if item != "adjustment_type"]
        if result.confidence >= 0.55:
            result.clarification_needed = False
    if intent == "injury_advice" and result.slots.get("injured_regions"):
        result.missing_slots = [item for item in result.missing_slots
                                if item != "injured_regions"]
        if result.confidence >= 0.55:
            result.clarification_needed = False

    # 必需槽位缺失：保留 intent，交给业务层追问。
    for required in SLOT_REQUIRED.get(intent, ()):
        if required == "adjustment_type":
            if not has_action and required not in result.missing_slots:
                result.missing_slots.append(required)
            continue
        if not result.slots.get(required) and required not in result.missing_slots:
            result.missing_slots.append(required)

    if result.missing_slots or result.confidence < 0.55:
        result.clarification_needed = True
    return result


# ---------------------------------------------------------------- 多任务拆解

TASK_OPERATIONS: dict[str, set[str]] = {
    "dialogue": {"clarify", "inspect_changes", "inspect_current_state", "undo_task", "cancel_tasks", "acknowledge"},
    "mcd_order": {"order_meal"},
    "adjust_today": {
        "skip_action", "replace_action", "remove_action", "add_action",
        "reorder_action", "change_sets", "change_reps", "change_duration",
        "change_location", "rest_today", "easier", "harder", "modify_training", "regenerate_training_today",
    },
    "adjust_workout": {
        "skip_action", "replace_action", "remove_action", "add_action",
        "change_sets", "change_reps", "rest_today", "easier", "harder",
        "modify_current_workout",
    },
    "adjust_plan": {"patch_plan"},
    "update_preference": {"update_profile", "update_food_preference"},
    "injury_advice": {"safety_advice"},
    "nutrition_advice": {
        "answer_nutrition", "get_meal_plan", "review_meal_plan",
        "modify_meal_plan", "replace_meal_item", "remove_meal_item", "add_meal_item",
        "record_meal", "update_nutrition_preference",
    },
    "exercise_guidance": {"teach_exercise"},
    "query_memory": {"read_memory"},
    "edit_memory": {"edit_memory"},
    "today_brief": {"brief_today"},
    "plan_overview": {"show_plan"},
    "progress_review": {"review_progress"},
    "coach_review": {"coach_review"},
    "review_followup": {"execute_suggestion"},
    "log_feedback": {"log_workout"},
    "create_plan": {"generate_plan"},
}

DEFAULT_OPERATION = {
    "mcd_order": "order_meal", "adjust_today": "modify_training",
    "adjust_workout": "modify_current_workout", "adjust_plan": "patch_plan",
    "update_preference": "update_profile", "injury_advice": "safety_advice",
    "nutrition_advice": "answer_nutrition", "exercise_guidance": "teach_exercise",
    "query_memory": "read_memory", "edit_memory": "edit_memory",
    "today_brief": "brief_today", "plan_overview": "show_plan",
    "progress_review": "review_progress", "coach_review": "coach_review",
    "review_followup": "execute_suggestion", "log_feedback": "log_workout",
    "create_plan": "generate_plan",
}

ACTION_SLOTS = {
    "target_list": "today_plan|current_workout|plan|null",
    "target_index": "int|null（用户可见编号，热身不算）",
    "target_action": "string|null",
    "target_muscle": "chest|back|legs|shoulders|arms|core|glutes|null",
    "target_category": "stretching|mobility|null",
    "replacement_category": "stretching|bodyweight|dumbbell|band|cardio|null",
    "replacement_exercise": "string|null",
    "sets": "int|null",
    "reps": "int|null",
    "duration_min": "int|null",
}


def _task1_prompt() -> str:
    lines = [
        "你是健身 Agent 的任务拆解器。只输出任务数组，不执行任何业务操作。",
        "先按标点/连接词（逗号、然后、再、同时、另外）逐句切分，再对每个子句判断任务。",
        "一句话可能包含多个独立任务；不要只选一个主意图，也不要漏掉点餐、训练、营养、偏好等不同任务。",
        "只要出现多个不同领域、或多个不同操作（例如“加两个动作…然后我喜欢…把晚餐换成…”），必须拆成多个 task；不要合并成一个通用 adjust_today。",
        "同一句话里“两个动作，一个练胸一个练背”要拆成两个 add_action task。",
        "每个任务必须有 domain、intent、operation、scope、task_order、user_goal。",
        "同一 intent 可以有多个 task；同一句话里的多个训练操作要拆成多个 task。",
        "训练动作操作必须用具体 operation：skip_action / replace_action / remove_action / add_action / change_sets / change_reps / change_duration / rest_today。",
        "不要因为用户说了“帮我点”就绕过确认；operation 只描述任务，不代表已授权执行。",
        "如果某个任务无法理解，保留其他任务，并把该任务 operation 设为 clarify。",
        "",
        "可选 intent：",
    ]
    for key, spec in INTENT_SPECS.items():
        lines.append(f"- {key}：{spec['description']}")
    lines += [
        "",
        "operation 参考：",
        "- 点餐：order_meal",
        "- 饮食：get_meal_plan（查看）, review_meal_plan（分析）, modify_meal_plan（改食谱/忌口）, replace_meal_item（换食物）, remove_meal_item（移除食物）, record_meal（记录吃了什么）",
        "- 今天/当前训练：skip_action, replace_action, remove_action, add_action, change_sets, change_reps, change_duration, change_location, rest_today, easier, harder",
        "- 长期计划：patch_plan；长期偏好：update_profile / update_food_preference；伤病：safety_advice；营养：answer_nutrition",
        "",
        "只输出 JSON：",
        '{"tasks":[{"task_id":"T1","domain":"meal|training|nutrition|preference|memory|review",'
        '"intent":"...","operation":"...","action_type":"query|recommend|create|update|delete|replace|remove|record|complete",'
        '"scope":"...","task_order":1,"user_goal":"...","dependencies":[]}]}',
    ]
    return "\n".join(lines)


def _task2_prompt(tasks: list[dict]) -> str:
    schema = {}
    for task in tasks:
        intent = str(task.get("intent") or "")
        base = dict(SLOT_SCHEMA.get(intent) or {})
        if intent in ("adjust_today", "adjust_workout"):
            base.update(ACTION_SLOTS)
        schema[task["task_id"]] = base
    lines = [
        "你是健身 Agent 的批量参数抽取器。只抽参数，不改变任务，不执行操作。",
        "下面是一组已经拆好的任务；为每个 task_id 独立抽取 slots，不能把参数混到别的任务。",
        "没有的字段填 null / []，不要编造；动作序号是用户可见编号（热身不算）。",
        "add_action：必须填 target_muscle（chest/back/legs/shoulders/arms/core），不要要求 target_index；",
        "skip_action/remove_action：填 target_index 或 target_action（如“去掉拉伸”→target_action=拉伸）；",
        "replace_action 若用户只说“换成拉伸”，replacement_category=stretching，replacement_exercise 可为 null；",
        "replace_meal_item：必须填 meal_slot 和 to_food/replacement_food；update_food_preference：liked_foods/disliked_foods/allergen_foods。",
        "只输出 JSON：",
        '{"task_slots":[{"task_id":"T1","slots":{},"missing_slots":[],'
        '"clarification_needed":false,"context_reference":{}}]}',
        "任务：",
        json.dumps(tasks, ensure_ascii=False),
        "每个任务的 slots 字段：",
        json.dumps(schema, ensure_ascii=False),
    ]
    return "\n".join(lines)


def _normalize_task(raw: dict, order: int) -> dict:
    intent = _normalize_intent(str(raw.get("intent") or ""))
    if not intent or intent in ("clarify", "safety"):
        return {}
    operation = str(raw.get("operation") or "").strip().lower()
    allowed = TASK_OPERATIONS.get(intent) or {DEFAULT_OPERATION.get(intent, "modify_training")}
    if operation not in allowed:
        operation = DEFAULT_OPERATION.get(intent, "modify_training")
    domain = str(raw.get("domain") or "").strip() or {
        "mcd_order": "meal", "nutrition_advice": "nutrition",
        "injury_advice": "training", "adjust_today": "training",
        "adjust_workout": "training", "adjust_plan": "training",
        "update_preference": "preference", "query_memory": "memory",
        "edit_memory": "memory",
    }.get(intent, "review")
    scope = str(raw.get("scope") or INTENT_SPECS.get(intent, {}).get("scope") or "today")
    if scope not in SCOPES:
        scope = INTENT_SPECS.get(intent, {}).get("scope", "today")
    action_type = str(raw.get("action_type") or "").strip().lower()
    if action_type not in ("query", "recommend", "create", "update", "delete",
                           "replace", "remove", "record", "complete"):
        action_type = {
            "get_meal_plan": "query", "review_meal_plan": "recommend",
            "modify_meal_plan": "update", "replace_meal_item": "replace",
            "remove_meal_item": "remove", "record_meal": "record",
            "answer_nutrition": "recommend", "order_meal": "create",
            "skip_action": "delete", "replace_action": "replace",
            "rest_today": "update", "patch_plan": "update",
            "update_profile": "update", "update_food_preference": "update",
        }.get(operation, "update")
    return {
        "task_id": str(raw.get("task_id") or raw.get("id") or f"T{order}"),
        "domain": domain, "intent": intent, "operation": operation,
        "action_type": action_type,
        "scope": scope, "task_order": int(raw.get("task_order") or order),
        "user_goal": str(raw.get("user_goal") or "")[:200],
        "dependencies": [str(item) for item in (raw.get("dependencies") or [])
                         if str(item).strip()],
        "confidence": 0.0, "slots": {}, "missing_slots": [],
        "clarification_needed": False, "context_reference": {},
    }


def _expand_task_slots(tasks: list[dict]) -> list[dict]:
    """模型把“两个动作/多个忌口”合并进一个 task 时，按结构化 slots 展开。

    这不是按关键词拆句，而是对模型已经抽出的 target_muscle/avoid_actions 做
    一对多展开，保证 task_id 与执行结果一一对应。
    """
    expanded: list[dict] = []
    for task in tasks:
        slots = task.get("slots") or {}
        if slots.get("add_action"):
            task["operation"] = "add_action"
        muscles = slots.get("target_muscle")
        if task.get("operation") == "add_action" and isinstance(muscles, list) and len(muscles) > 1:
            for index, muscle in enumerate(muscles, 1):
                child = {**task, "task_id": f"{task['task_id']}_{index}",
                         "slots": {**slots, "target_muscle": muscle}}
                child["task_order"] = task["task_order"] + index / 10
                expanded.append(child)
            continue
        if task.get("operation") == "remove_action" and not slots.get("target_action"):
            avoided = slots.get("avoid_actions") or []
            if isinstance(avoided, list) and len(avoided) >= 1:
                task["slots"] = {**slots, "target_action": avoided[0]}
        expanded.append(task)
    return expanded


def _clause_count(text: str) -> int:
    """按标点和连接词估句子里的独立片段数，只用于“是否可能漏任务”的检查。"""
    pieces = [piece for piece in re.split(
        r"[，,。；;！？!?]|然后|并且|同时|另外|接着|再", text or "",
    ) if piece.strip()]
    return max(1, len(pieces))


_MUSCLE_WORDS = {
    "胸": "chest", "背": "back", "腿": "legs", "肩": "shoulders",
    "手臂": "arms", "胳膊": "arms", "核心": "core", "臀": "glutes",
}


def _fallback_decompose(text: str, existing: list[dict]) -> list[dict]:
    """模型明显漏任务时的结构化兜底。

    只识别“加/去掉/喜欢/替换晚餐”这类明确操作语法，不负责普通意图分类；
    只有模型返回的任务数少于句子片段数时才使用。它保证显式操作不会因为模型
    一次输出波动而整段丢失。
    """
    found: list[dict] = []

    def add_task(intent: str, operation: str, slots: dict, goal: str) -> None:
        found.append({
            "task_id": f"F{len(found) + 1}", "domain": (
                "meal" if intent == "mcd_order" else
                "nutrition" if intent in ("nutrition_advice", "update_preference")
                else "training"),
            "intent": intent, "operation": operation,
            "action_type": "", "scope": (
                "long_term" if intent == "update_preference" else "today"),
            "task_order": 90 + len(found), "user_goal": goal,
            "dependencies": [], "confidence": 0.8, "slots": slots,
            "missing_slots": [], "clarification_needed": False,
            "context_reference": {},
        })

    # 新增 / 减少动作：逐个子句处理，避免只读第一句
    for clause in [piece.strip() for piece in re.split(
            r"[，,。；;]|然后|同时|另外|接着|再", text) if piece.strip()]:
        if re.search(r"减少|去掉|删掉|移除|不要", clause):
            # “把划船和俯卧撑都去掉”拆成两个 remove_action。
            pair = re.search(r"把(.+?)(?:都)?(?:去掉|删掉|移除|不要)", clause)
            if pair:
                names = [item.strip("，,。 ") for item in re.split(
                    r"和|、|,|，", pair.group(1)) if item.strip("，,。 ")]
                if len(names) >= 2:
                    for name in names:
                        add_task("adjust_today", "remove_action",
                                 {"target_action": name}, f"去掉{name}")
                    continue
            muscle = None
            for match in re.finditer(r"练(手臂|胳膊|核心|胸|背|腿|肩|臀)", clause):
                muscle = _MUSCLE_WORDS.get(match.group(1))
                if muscle:
                    break
            slots = {}
            if muscle:
                slots["target_muscle"] = muscle
            elif "拉伸" in clause:
                slots["target_action"] = "拉伸"
            add_task("adjust_today", "remove_action", slots,
                     clause or "去掉一个动作")
            continue
        if re.search(r"加|增加|新增", clause):
            if "拉伸" in clause:
                add_task("adjust_today", "add_action",
                         {"target_category": "stretching"}, "加一个拉伸动作")
                continue
            muscles: list[str] = []
            for match in re.finditer(r"练(手臂|胳膊|核心|胸|背|腿|肩|臀)", clause):
                muscle = _MUSCLE_WORDS.get(match.group(1))
                if muscle and muscle not in muscles:
                    muscles.append(muscle)
            if not muscles and re.search(r"蹲|腿", clause):
                muscles = ["legs"]
            if muscles:
                for muscle in muscles:
                    slots = {"target_muscle": muscle}
                    if "蹲" in clause:
                        slots["target_action"] = "深蹲"
                    add_task("adjust_today", "add_action", slots,
                             f"加一个练{muscle}动作")
            else:
                add_task("adjust_today", "add_action", {}, clause)

    # 伤病：手/手腕/膝盖/腰等不适
    from app.domain.safety_rules import detect_regions

    for clause in [piece.strip() for piece in re.split(
            r"[，,。；;]|然后|同时|另外|接着|再", text) if piece.strip()]:
        if any(word in clause for word in ("不舒服", "疼", "痛", "受伤")):
            regions = detect_regions(clause)
            if not regions and "手" in clause:
                regions = ["wrist"]
            if regions:
                add_task("injury_advice", "safety_advice",
                         {"injured_regions": regions}, clause)

    # 点餐：只处理明确的“点个/点一份/帮我点”
    for match in re.finditer(r"(?:帮我点|点个|点一份|点一?个)([\u4e00-\u9fa5]{1,8})", text):
        food = match.group(1).strip("，,。 ")
        if food.startswith("个"):
            food = food[1:]
        if food and not any(word in food for word in ("米饭", "晚餐", "午餐", "早餐")):
            add_task("mcd_order", "order_meal", {"foods": [food]}, f"点{food}")

    # 喜欢 / 不喜欢 / 过敏食物
    for match in re.finditer(r"(?:喜欢|爱吃)吃?([\u4e00-\u9fa5]{1,6})", text):
        food = match.group(1).strip("，,。 ")
        if food:
            add_task("update_preference", "update_food_preference",
                     {"liked_foods": [food]}, f"喜欢吃{food}")
    for match in re.finditer(r"(?:不喜欢|不吃|不要安排)(?:吃)?([\u4e00-\u9fa5]{1,6})", text):
        food = match.group(1).strip("，,。 ")
        if food:
            add_task("update_preference", "update_food_preference",
                     {"disliked_foods": [food]}, f"不喜欢吃{food}")

    # 替换/指定某一餐：“把晚餐替换成牛肉”“晚餐吃点米饭”
    meal_map = {"早": "breakfast", "午": "lunch", "晚": "dinner"}
    for match in re.finditer(r"([早午晚])餐[^，,。；;]{0,12}?(?:替换|换)成?([\u4e00-\u9fa5]{1,6})", text):
        slot, food = match.group(1), match.group(2).strip("，,。 ")
        add_task("nutrition_advice", "replace_meal_item",
                 {"meal_slot": meal_map[slot], "replacement_food": food,
                  "to_food": food}, f"把{slot}餐换成{food}")
    for match in re.finditer(r"([早午晚])餐\s*(?:吃|来|要|想)(?:点|些)?([\u4e00-\u9fa5]{2,6})", text):
        slot, food = match.group(1), match.group(2).strip("，,。 ")
        if food in ("什么", "啥", "什么？", "点"):
            continue
        add_task("nutrition_advice", "replace_meal_item",
                 {"meal_slot": meal_map[slot], "replacement_food": food,
                  "to_food": food}, f"{slot}餐吃{food}")

    # 去掉与已有任务重复的项
    existing_keys = {
        (task.get("intent"), task.get("operation"),
         json.dumps(task.get("slots") or {}, sort_keys=True, ensure_ascii=False))
        for task in existing
    }
    existing_replace_slots = {
        str((task.get("slots") or {}).get("meal_slot") or "")
        for task in existing
        if task.get("operation") in ("replace_meal_item", "modify_meal_plan")
    }
    # 已有“加动作”但只给了肌群：把兜底解析出的具体动作（如“深蹲”）补进去。
    for fallback in list(found):
        if fallback.get("operation") != "add_action":
            continue
        f_slots = fallback.get("slots") or {}
        if not f_slots.get("target_action"):
            continue
        for task in existing:
            t_slots = task.get("slots") or {}
            if (task.get("operation") == "add_action"
                    and t_slots.get("target_muscle") == f_slots.get("target_muscle")
                    and not t_slots.get("target_action")):
                task["slots"] = {**t_slots, "target_action": f_slots["target_action"]}
                found.remove(fallback)
                break
    # 已有“营养替换”但槽位为空：把兜底槽位合并进去，而不是再加一个同餐任务。
    fallback_replace = next(
        (task for task in found
         if task.get("operation") in ("replace_meal_item", "modify_meal_plan")
         and (task.get("slots") or {}).get("meal_slot")),
        None,
    )
    if fallback_replace is not None:
        generic = next(
            (task for task in existing
             if task.get("intent") == "nutrition_advice"
             and task.get("operation") in ("replace_meal_item", "modify_meal_plan")
             and not (task.get("slots") or {})),
            None,
        )
        if generic is not None:
            generic["slots"] = dict(fallback_replace["slots"])
            existing_replace_slots.add(str(fallback_replace["slots"].get("meal_slot")))
            found = [task for task in found if task is not fallback_replace]
    result = []
    for task in found:
        key = (task["intent"], task["operation"],
               json.dumps(task["slots"], sort_keys=True, ensure_ascii=False))
        slot = str((task.get("slots") or {}).get("meal_slot") or "")
        if key in existing_keys:
            continue
        if (task.get("operation") in ("replace_meal_item", "modify_meal_plan")
                and slot and slot in existing_replace_slots):
            continue
        result.append(task)
        existing_keys.add(key)
    # 已有任务是“通用调整”且兜底补出了具体任务时，丢掉通用项，避免重复。
    if result and existing:
        existing = [task for task in existing
                    if task.get("operation") not in ("modify_training", "modify_current_workout")
                    or (task.get("slots") or {})]
    # 同一餐的替换/同一食物偏好只保留一项；模型通用任务与兜底具体任务冲突时，
    # 丢掉通用的空槽任务，避免“晚餐换牛肉”被执行两次。
    if result:
        existing = [
            task for task in existing
            if not (
                task.get("intent") in ("nutrition_advice", "update_preference")
                and task.get("operation") in ("answer_nutrition", "modify_meal_plan",
                                              "modify_training", "update_profile")
                and not (task.get("slots") or {})
            )
        ]
    return existing + result


def _canonical_task(task):
    """Resolve legacy intent labels from tool metadata, never from user keywords.

    Operation is the executable contract. A second free-form model label must
    not veto a valid capability or turn an unknown operation into a default one.
    """
    choices = [intent for intent, operations in TASK_OPERATIONS.items()
               if task["operation"] in operations]
    if set(choices) == {"adjust_today", "adjust_workout"}:
        intent = "adjust_workout" if task["scope"] == "current_workout" else "adjust_today"
    elif len(choices) == 1:
        intent = choices[0]
    else:
        return task
    task["intent"] = intent
    task["domain"] = ("nutrition" if intent == "nutrition_advice" else
                      "dialogue" if intent == "dialogue" else
                      "training" if intent in {"adjust_today", "adjust_workout", "adjust_plan", "create_plan"}
                      else task.get("domain", ""))
    return task


# Live entry: semantics, goals, constraints and task parameters share one schema.
def understand_tasks(client, text: str, *, context: dict, fallback_intent="clarify"):
    from pydantic import ValidationError
    from app.agent.semantic_contract import SemanticPlan

    schema = SemanticPlan.model_json_schema()
    # One operation contract replaces two competing free-form labels.
    schema["$defs"]["SemanticTask"]["properties"].pop("intent", None)
    schema["properties"].pop("execution_mode", None)
    schema["$defs"]["SemanticTask"]["required"].append("execution_mode")
    prompt = (
        "理解健身用户当前真实目标，输出符合 schema 的一个 JSON 对象。"
        "当前明确表达优先于旧话题与页面；页面不能强制决定业务领域。"
        "只把用户要求执行的内容变为任务，否定的修改不能变为肯定任务。"
        "dialogue_act表示用户交际行为；clarify仅在需要追问时使用，解释历史操作必须用inspect_changes。"
        "整体需要澄清不代表丢弃所有任务：每个任务独立标记clarification_needed，其他明确任务保留执行。"
        "区分新增食物(add_meal_item)、替换(replace_meal_item)和移除(remove_meal_item)。"
        "重新生成须明确训练/饮食领域；不能确定则 dialogue_act=clarify，提供简短 clarification_question。"
        "训练重排使用 regenerate_training_today，不能因此生成或覆盖饮食。"
        "execution_mode属于每个任务：先给方案为propose，明确执行为execute。"
        "propose必须在proposal_details给出具体建议、好处与取舍，不能只复述目标；这些内容不能声称数据已经修改。"
        "取消/保持原样用dialogue/cancel_tasks或acknowledge且noop；撤销已保存修改是undo_task且execute。"
        "一句话可取消一个未执行方案同时撤销另一个已保存修改，各任务的执行模式不能互相覆盖。"
        "纠错先查 execution_history 的真实变更；inspect_changes 可筛选 meal_slot/action_ref。"
        "查询现在的组数/餐单用inspect_current_state，读取最新业务状态，不能用旧变更的目标值充当现状。"
        "撤销用 undo_task 并引用 execution_id 或唯一资源，多个可能对象须澄清。"
        "相对组数基于最新业务状态；可输出 sets_delta 或绝对 sets。"
        "scope/date 必须匹配上下文日期，未实现能力保留原操作，不能改为建议伪装完成。"
        "context_reference 用 action_ref/execution_id，不能编造对象或执行记录。"
        "不执行任务的说明可用 dialogue intent 的 clarify/acknowledge/cancel_tasks。"
        "字段 schema：" + json.dumps(schema, ensure_ascii=False) +
        "能力：" + json.dumps({k: sorted(v) for k, v in TASK_OPERATIONS.items()}, ensure_ascii=False) +
        "参数：" + json.dumps({"training": ACTION_SLOTS, "domains": SLOT_SCHEMA,
                              "add_meal_item": {"meal_slot": "餐次", "food": "食材原名", "grams": "默认100"},
                              "remove_meal_item": {"meal_slot": "餐次", "food": "要移除的食材，非替换目标"},
                              "undo_task": {"execution_id": "唯一执行记录ID，可省略", "meal_slot": "餐次，可省略",
                                            "action_ref": "训练动作ref，可省略", "source_operation": "要撤销的原操作名称，可省略"},
                              "inspect_changes": {"meal_slot": "省略表示全部餐次", "action_ref": "省略表示全部动作",
                                                  "source_operation": "省略表示全部操作"},
                              "references": {"execution_id": "已验证记录ID", "sets_delta": "int"}}, ensure_ascii=False)
    )
    raw, error = _call(client, [{"role": "system", "content": prompt},
                              {"role": "user", "content": json.dumps(
                                  {"text": text, "context": context}, ensure_ascii=False)}], 2400)
    if error:
        return [], raw, "", error
    try:
        parsed = SemanticPlan.model_validate(_clean_json(raw))
        tasks = [_canonical_task(t.model_dump()) for t in parsed.tasks]
        if not tasks:
            if not tasks and parsed.dialogue_act == "request":
                raise ValueError("empty_tasks")
            tasks = [{"task_id": "D1", "domain": "dialogue", "intent": "dialogue",
                      "operation": "clarify" if parsed.dialogue_act == "clarify" else "acknowledge",
                      "scope": "message", "slots": {"question": parsed.clarification_question},
                      "user_goal": parsed.clarification_question, "dependencies": []}]
        for task in tasks:
            if task["intent"] == "dialogue" and task["operation"] == "clarify":
                task["execution_mode"] = "execute"
            else:
                source = next((t for t in parsed.tasks if t.task_id == task["task_id"]), None)
                explicit_mode = source is not None and "execution_mode" in source.model_fields_set
                if not explicit_mode:
                    if parsed.dialogue_act in {"cancel", "acknowledge"}:
                        task["execution_mode"] = "noop"
                    elif parsed.execution_mode != "execute":
                        task["execution_mode"] = parsed.execution_mode
        ids = [t["task_id"] for t in tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_task_id")
        # A topological sort; unknown references and cycles cannot be executed.
        ordered = []
        while tasks:
            done = {t["task_id"] for t in ordered}
            ready = next((t for t in tasks if set(t["dependencies"]) <= done), None)
            if ready is None:
                raise ValueError("invalid_dependencies")
            ordered.append(ready)
            tasks.remove(ready)
        # Broad regeneration gets an independent scope check. Existing data and
        # the planner's own guessed goal are not authorization evidence.
        broad = [t for t in ordered if t["operation"] == "regenerate_training_today" and t.get("execution_mode", "execute") == "execute"]
        guard_raw = ""
        if broad:
            guard_raw, guard_error = _call(client, [
                {"role": "system", "content": (
                    "你是业务修改授权核对器，只根据用户原话和明确对话约定检查范围。"
                    "页面、现有训练或餐单、规划器猜测都不能证明用户授权某个领域。"
                    "重生成计划若不能确定训练还是饮食，不能授权，必须追问。"
                    "只输出JSON：{authorized_task_ids: [已明确授权范围的任务ID], question: 未明确时的简短问题}。")},
                {"role": "user", "content": json.dumps({"text": text,
                    "recent": context.get("recent") or [],
                    "tasks": [{"task_id": t["task_id"], "operation": t["operation"], "scope": t["scope"]} for t in broad]}, ensure_ascii=False)},
            ], 1200)
            guard = _clean_json(guard_raw)
            authorized = guard.get("authorized_task_ids") if not guard_error else []
            authorized = authorized if isinstance(authorized, list) else []
            for task in broad:
                if task["task_id"] not in authorized:
                    task.update(intent="dialogue", domain="dialogue", operation="clarify", scope="message",
                                slots={"question": str(guard.get("question") or "你想重新生成训练还是饮食计划？")},
                                clarification_needed=False, missing_slots=[], execution_mode="execute")
        return ordered, raw, guard_raw, ""
    except (ValidationError, ValueError, TypeError) as exc:
        logger.warning("semantic_schema_invalid type=%s", type(exc).__name__)
        return [], raw, "", "semantic_schema_invalid"
