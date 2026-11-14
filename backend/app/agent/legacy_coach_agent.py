
# RETIRED V1: historical regressions only; never imported by the production chat entry.
"""统一教练 Agent：一个入口，自己判断"该改哪一层"。

它不重新实现业务能力，而是把已有能力编排起来：

| 用户说的话 | 决策 | 调用的既有能力 |
| --- | --- | --- |
| 我想减脂，每周 3 次 | 建计划 | `domain.coach.create_draft`（skill + 安全门） |
| 今天只有 20 分钟，不去健身房 | 只改今天 | `domain.day_plan.apply_constraints` → 当天 override |
| 以后把训练放午休 | 改长期偏好 | `domain.memory` 写入 long_term |
| 器械被占用 / 这个动作我不会 | 改当前训练 | `domain.workout.replace_action` / `update_guidance` |
| 今天完成了，有点累 | 记反馈 | `domain.workout.finish_session` → WorkoutLog + short_term |
| 你记得我什么 / 忘掉这条 | 记忆管理 | `domain.memory` |

三条不可越过的边界：
1. 临时上下文只写 `TemporaryContext`，不写长期记忆、不写 PlanVersion；
2. 训练中调整只改当前及之后，不清空已完成组数；
3. 计划层面的变更仍生成 draft，用户确认后才落版本（保留原有 HITL/审计）。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import append_event
from app.domain import coach as coach_plans
from app.domain import requests as patch_requests
from app.domain import workout as workout_service
from app.domain.day_plan import (
    allowed_equipment,
    apply_constraints,
    constraint_from_text,
    constraint_summary,
    effective_day,
    pick_alternative,
    save_override,
    summarise_adjustment,
)
from app.domain.memory import (
    SHORT_TERM,
    archive_memory,
    extract_candidates,
    find_memories,
    list_memories,
    load_memory_context,
    load_temporary,
    memory_payload,
    recent_training_state,
    set_temporary,
    upsert_memory,
)
from app.domain.planner import parse_intent
from app.domain.profile_sync import (
    apply_preference_candidates,
    detect_conflicts,
)
from app.domain.safety_rules import detect_regions
from app.agent.intent_router import (
    IntentResult,
    understand as understand_intent,
    understand_tasks,
)
from app.domain.timeutil import user_today
from app.knowledge.retriever import search as kb_search
from app.knowledge.service import RED_FLAGS
from app.models import (
    AgentMessage,
    AgentRun,
    MealLog,
    Plan,
    PlanAction,
    PlanVersion,
    Profile,
    User,
    WorkoutSession,
    new_id,
)

INTENT_LABELS = {
    "create_plan": "建立 / 重排计划",
    "adjust_today": "调整今天",
    "update_preference": "更新长期偏好",
    "adjust_workout": "调整当前训练",
    "exercise_guidance": "动作指导",
    "log_feedback": "记录训练反馈",
    "query_memory": "查看记忆",
    "edit_memory": "修改记忆",
    "today_brief": "安排今天",
    "progress_review": "复盘训练进展",
    "coach_review": "综合评估与建议",
    "review_followup": "执行上一条建议",
    "adjust_plan": "修改计划（待确认）",
    "injury_advice": "伤病与不适",
    "nutrition_advice": "饮食建议",
    "plan_overview": "查看每周安排",
    "mcd_order": "麦当劳点餐",
    "clarify": "需要补充信息",
    "safety": "安全阻断",
}

LONG_TERM_HINTS = ("以后", "今后", "通常", "平时", "一直", "长期", "每次", "每天",
                   "每周", "习惯", "总是")
TEMP_HINTS = ("今天", "今晚", "这次", "现在", "临时", "只剩", "只有")
GOAL_HINTS = ("减脂", "减重", "增肌", "增重", "心肺", "耐力", "体态", "综合健康",
              "想瘦", "降体脂", "长肌肉")
CREATE_HINTS = ("生成计划", "制定计划", "制定一个计划", "做个计划", "做一份计划",
                "重新制定", "重新生成", "换一份计划", "换一个计划",
                "重新规划", "做一个计划", "给我一份计划", "帮我安排计划", "开始健身",
                "从头开始", "重排计划", "按这个重新生成", "帮我制定", "帮我做")
BRIEF_HINTS = ("规划今天", "安排今天", "今天吃什么", "今日安排", "今日规划",
               "帮我规划", "再规划", "重新规划今天", "今天怎么练",
               "今天的训练计划", "今日训练计划", "生成今天", "生成今日",
               "今天练什么", "今天训练什么")
MEMORY_QUERY_HINTS = ("你记得", "还记得", "记得我", "记住了什么", "我的记忆",
                      "查看记忆", "我的偏好", "你了解我", "记忆里")
MEMORY_EDIT_HINTS = ("忘掉", "删除记忆", "删掉记忆", "别记", "不要记", "忘记这条",
                     "去掉这条记忆", "删掉这条")
GUIDANCE_HINTS = ("不会", "怎么做", "动作要领", "姿势", "示范", "教我")
COMPLETE_HINTS = ("完成了", "练完了", "做完了", "都做完了", "结束了", "打卡")
INCOMPLETE_HINTS = ("没做完", "没完成", "只做了一半", "没练完", "没做完最后")
FEEDBACK_HINTS = COMPLETE_HINTS + INCOMPLETE_HINTS + ("有点累", "太累", "很累", "疲劳",
                                                       "今天训练")
EQUIPMENT_BUSY_HINTS = ("器械有人用", "被占用", "有人用", "占着", "被人用", "没有器械",
                        "器械在用", "这台有人")
WORKOUT_HINTS = ("这个动作", "当前动作", "下一个", "跳过", "换一个动作", "换动作",
                 "做不了", "做不下去", "太累了", "只剩", "没有力气", "不舒服")
# 计划层面的变更：走原有的补丁草案 + 确认 + 安全门，而不是直接改版本
PLAN_CHANGE_HINTS = ("这周只能", "本周只能", "只能练", "这周没时间", "这周没那么多时间",
                     "停练", "先停练", "换部位", "换个部位", "加强度", "加量",
                     "加点强度", "加一点强度", "想加点强度",
                     "这周只能练", "本周只能练")

# 伤病：只要"身体部位 + 不适词"同时出现，就进入安全/调整分支，
# 不再要求"正在训练中"（评审 P-002）。"不疼/没受伤"这类否定先排除。
INJURY_WORDS = ("疼", "痛", "不舒服", "不太舒服", "难受", "不适", "受伤", "拉伤", "扭到", "肿", "发酸", "酸胀")
INJURY_NEGATIONS = ("不疼", "不痛", "没受伤", "没有受伤", "不难受", "不酸", "不肿")
INJURY_ROUTE_HINTS = ("还能练", "能练什么", "怎么练", "更适合", "换", "替代", "注意什么",
                      "要不要停", "能不能练", "怎么办")

# 饮食：没有"记录"能力时，至少先能回答"怎么吃/这餐怎么调"。
NUTRITION_HINTS = ("外卖", "饮食", "吃什么", "怎么吃", "热量", "蛋白", "碳水", "加餐",
                   "早餐", "午餐", "晚餐", "夜宵", "零食", "奶茶", "黄焖鸡", "麻辣烫",
                   "汉堡", "炸鸡", "米饭", "面条", "减脂餐", "食谱")
NUTRITION_LOG_HINTS = ("记一下", "记录", "我吃了", "刚吃", "午饭吃", "晚饭吃", "早餐吃")

PLAN_OVERVIEW_HINTS = ("每周安排", "一周安排", "整周", "这一周",
                       "整理成", "照着执行", "每天练什么")

REGRESSION_HINTS = ("太难", "简单点", "学不会", "做不了", "换个简单", "轻一点", "降一点")
WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
TRAIN_TODAY_HINTS = ("今天想练", "今天要练", "想锻炼", "不休息了", "今天不休息",
                     "开始训练", "开始练", "开始锻炼", "可以锻炼", "能锻炼",
                     "状态不错", "有精神", "想练", "锻炼一下", "为今天生成",
                     "今天练一下", "今天想训练")
MCD_HINTS = ("麦当劳", "mcd", "麦记", "mcptoken", "mcp", "汉堡")
MCD_ORDER_HINTS = ("点餐", "点好", "下单", "帮我点", "来一份", "点一份", "订餐",
                   "点外卖", "订麦当劳", "点麦当劳", "想吃", "买个", "点个")
REVIEW_HINTS = ("根据我的档案", "根据我的记忆", "根据档案", "根据记忆",
                "看看下一步", "下一步该", "下一步怎么", "综合评估",
                "给我点建议", "给我一些建议", "我该怎么练", "接下来怎么练")
FOLLOWUP_TEXTS = ("帮我弄一下", "帮我弄", "弄一下", "就按你说的", "按你说的",
                  "按这个来", "就按这个", "开始吧", "安排吧", "帮我安排",
                  "好", "好的", "可以", "行", "就这个")


@dataclass
class Decision:
    intent: str
    scope: str
    constraints: dict = field(default_factory=dict)
    candidates: list[dict] = field(default_factory=list)
    memory_query: bool = False
    memory_edit: bool = False
    confidence: float = 0.8
    reason: str = ""
    needs_confirmation: bool = False
    # 分层理解留下的元信息：意图不确定时保留 intent，交给业务层追问，而不是丢成 clarify。
    missing_slots: list[str] = field(default_factory=list)
    clarification_needed: bool = False
    secondary_intents: list[str] = field(default_factory=list)
    user_goal: str = ""
    context_reference: dict = field(default_factory=dict)
    action: str = ""
    source: str = "rule"
    understanding_error: str = ""


# ---------------------------------------------------------------- 意图识别


def wants_training_today(text: str) -> bool:
    raw = text or ""
    if any(word in raw for word in ("不想练", "不练了", "不想训练")):
        return False
    # “下周我想练背”是改计划，不是今天立刻开练
    if "今天" not in raw and any(
            word in raw for word in ("下周", "下一周", "下星期", "这周", "本周")):
        return False
    return any(word in raw for word in TRAIN_TODAY_HINTS)


def _is_followup_text(text: str) -> bool:
    clean = re.sub(r"[\s。.!！?？,，]", "", text or "")
    return len(clean) <= 8 and any(
        clean == item or (len(item) >= 3 and clean.startswith(item))
        for item in FOLLOWUP_TEXTS
    )

def _injury_regions(text: str) -> list[str]:
    """识别"身体部位 + 不适"；否定句（不疼/没受伤）不算。"""
    raw = text or ""
    if any(neg in raw for neg in INJURY_NEGATIONS):
        return []
    if not any(word in raw for word in INJURY_WORDS):
        return []
    return detect_regions(raw)


def _is_nutrition_question(text: str) -> bool:
    raw = text or ""
    if any(word in raw for word in ("训练计划", "生成计划", "换一份计划", "重排计划")):
        return False
    return any(word in raw for word in NUTRITION_HINTS)


# classify 有问题吧,这个方法不应该是这样的,得让大模型自己判断text的语意,我们给大模型一个json里面有我们希望的类型,大模型自己判断完给我们回填才对吧
def classify(text: str, *, has_plan: bool, in_workout: bool,
             page: str = "today", recent: list[dict] | None = None) -> Decision:
    raw = (text or "").strip()
    long_term = any(word in raw for word in LONG_TERM_HINTS)
    temp = any(word in raw for word in TEMP_HINTS)
    constraints = constraint_from_text(raw)
    candidates = extract_candidates(raw)

    if any(word in raw for word in RED_FLAGS):
        return Decision("safety", "none", constraints, candidates, reason="红旗症状")

    if any(word in raw for word in MEMORY_EDIT_HINTS):
        return Decision("edit_memory", "none", constraints, candidates, memory_edit=True,
                        reason="用户要删除记忆")
    if any(word in raw for word in MEMORY_QUERY_HINTS):
        return Decision("query_memory", "none", constraints, candidates, memory_query=True,
                        reason="用户要查看记忆")

    # 伤病先于计划：用户说"膝盖不舒服"时，先给安全建议，绝不能当成普通改计划。
    injury_regions = _injury_regions(raw)
    if injury_regions or (any(word in raw for word in INJURY_WORDS)
                          and any(word in raw for word in INJURY_ROUTE_HINTS)):
        return Decision(
            "injury_advice", "none",
            {**constraints, "injured_regions": injury_regions}, candidates,
            reason="用户报告身体不适/伤病",
        )

    # 饮食问答：当前版本没有饮食记录，但至少要能回答"怎么吃/这餐怎么调"。
    if _is_nutrition_question(raw):
        return Decision("nutrition_advice", "today", constraints, candidates,
                        reason="用户询问饮食安排")

    # 把现有计划整理成可执行的每周安排（只读，不改版本）。
    if any(word in raw for word in PLAN_OVERVIEW_HINTS) and not any(
            word in raw for word in ("生成计划", "换一份计划", "重排计划", "重新生成")):
        return Decision("plan_overview", "long_term", constraints, candidates,
                        reason="用户要查看/整理每周安排")

    if recent and _is_followup_text(raw):
        last_agent = next(
            (item for item in reversed(recent) if item.get("role") == "agent"), None,
        )
        if (last_agent or {}).get("intent") == "coach_review":
            return Decision(
                "review_followup", "today", constraints, candidates,
                reason="用户接受并执行上一条综合建议",
            )

    if has_plan and not in_workout and not any(
            word in raw for word in LONG_TERM_HINTS) and any(
            word in raw for word in ("不想", "不要", "不能", "避免", "别安排", "不用")):
        exclude_intent = parse_intent(raw)
        if exclude_intent.excluded_labels:
            return Decision(
                "adjust_plan", "long_term", constraints, candidates,
                reason="用户明确要避开具体动作：" + "、".join(exclude_intent.excluded_labels),
            )

    if any(word in raw.lower() for word in MCD_HINTS) and any(
            word in raw for word in MCD_ORDER_HINTS):
        return Decision("mcd_order", "meal", constraints, candidates,
                        reason="用户要用麦当劳 MCP 点餐")

    # 自然语言里的"根据最近训练/体重，调整下一周"应直接进入计划重排，
    # 不能落到 clarify 再让用户选层级。
    if not in_workout:
        next_week = any(word in raw for word in ("下一周", "下周", "下星期"))
        adjust_words = ("调整", "安排", "重排", "重新排", "降低", "减少", "增加",
                        "加量", "减轻", "怎么练", "训练量")
        evidence_words = ("根据", "最近", "训练", "体重", "完成率", "进展", "状态",
                          "恢复", "疲劳")
        if next_week and any(word in raw for word in adjust_words) and any(
                word in raw for word in evidence_words):
            return Decision("create_plan", "long_term", constraints, candidates,
                            reason="用户根据最近状态要求调整下一周计划")

    # 进展页的"根据最近没完成情况调整下一周"是计划请求，不是对某一场训练打卡。
    # 必须在"没完成"的反馈分支之前判断，否则会把建议误记成一次训练反馈。
    if page == "progress" and not in_workout:
        progress_adjust = ("下一周", "下周", "训练量", "重新安排", "降低", "减少",
                           "增加", "加量", "调整", "调低", "减轻", "强度",
                           "开始训练", "入门训练", "安排")
        progress_trigger = ("帮我", "根据", "最近", "我该", "怎么")
        if any(word in raw for word in progress_adjust) and any(
                word in raw for word in progress_trigger):
            return Decision("create_plan", "long_term", constraints, candidates,
                            reason="用户根据进展要求调整下一版计划")

    # 训练中 / 刚练完：先把"完成反馈"和"继续调整"分开
    if any(word in raw for word in INCOMPLETE_HINTS) or any(word in raw for word in COMPLETE_HINTS):
        return Decision("log_feedback", "workout", constraints, candidates,
                        reason="用户报告训练结果")
    if any(word in raw for word in ("不太舒服", "有点疼", "疼痛", "受伤")) and in_workout:
        return Decision("adjust_workout", "workout", constraints, candidates,
                        reason="训练中不适")

    # 进展页：先判断用户是在"复盘"还是"要求调整下一版计划"
    if page == "progress" and not in_workout:
        adjust_words = ("下一周", "下周", "重新安排", "调整", "降低", "减少", "增加", "加量",
                        "降一点", "加一点", "计划")
        if any(word in raw for word in adjust_words) and not any(
                word in raw for word in ("为什么", "解释", "看看", "趋势")):
            return Decision("create_plan", "long_term", constraints, candidates,
                            reason="用户要求根据进展调整下一版计划")
        return Decision("progress_review", "progress", constraints, candidates,
                        reason="用户在进展页询问训练数据")

    if any(word in raw for word in REVIEW_HINTS) and not any(
            word in raw for word in CREATE_HINTS):
        return Decision("coach_review", "long_term", constraints, candidates,
                        reason="用户要求结合档案、记忆和进展给出下一步建议")

    if any(word in raw for word in GUIDANCE_HINTS) and not any(
            word in raw for word in ("换简单", "换个动作", "替换")):
        return Decision("exercise_guidance", "workout" if in_workout else "today",
                        constraints, candidates, reason="用户询问动作做法")

    # 训练中的即时变化：器械、剩余时间、疲劳、跳过
    if in_workout and any(word in raw for word in EQUIPMENT_BUSY_HINTS + WORKOUT_HINTS):
        return Decision("adjust_workout", "workout", constraints, candidates,
                        reason="训练中上下文变化")

    if has_plan and not in_workout and wants_training_today(raw):
        return Decision("today_brief", "today", constraints, candidates,
                        reason="用户明确今天要训练，需要生成今天的训练安排")

    if "今天" not in raw and any(
            word in raw for word in ("下周", "下一周", "下星期")):
        return Decision("adjust_plan", "long_term", constraints, candidates,
                        reason="用户提出下一周的训练重点/安排，需要走计划变更确认")

    # 计划层面的变更：这周只能练两天 / 停练 / 换部位 / 加强度
    if has_plan and any(word in raw for word in PLAN_CHANGE_HINTS) and not (
            "今天" in raw and not any(word in raw for word in ("这周", "本周", "以后", "计划"))):
        return Decision("adjust_plan", "long_term", constraints, candidates,
                        reason="用户要求修改计划结构")

    # 计划生成：明确说"生成/换一份"或首次表达目标
    if any(word in raw for word in CREATE_HINTS):
        return Decision("create_plan", "long_term", constraints, candidates,
                        reason="用户要求生成 / 更换计划")
    if not has_plan and any(word in raw for word in GOAL_HINTS):
        return Decision("create_plan", "long_term", constraints, candidates,
                        reason="还没有计划，用户在表达目标")
    if any(word in raw for word in GOAL_HINTS) and (
            any(word in raw for word in ("想", "要", "目标")) or long_term
    ) and not temp:
        # "我想减脂，每周三次" 是建立目标，不是今天临时改
        if any(word in raw for word in ("每周", "每次", "减脂", "增肌", "心肺")):
            return Decision("create_plan", "long_term", constraints, candidates,
                            reason="用户在建立长期目标")

    if any(word in raw for word in BRIEF_HINTS):
        return Decision("today_brief", "today", constraints, candidates,
                        reason="用户要今天的整体安排")

    # 长期偏好：有"以后/通常/一直"等信号，且抽到了可存的记忆
    if long_term and candidates:
        return Decision("update_preference", "long_term", constraints, candidates,
                        reason="用户表达长期偏好")
    if candidates and not temp and any(
            item.get("category") in (
                "goal", "weekly_days", "schedule", "location", "equipment",
                "dislike", "like", "experience", "intensity", "injury",
            )
            for item in candidates):
        return Decision("update_preference", "long_term", constraints, candidates,
                        reason="用户说明了稳定条件")

    # 正在训练时，这些条件应该改"当前及剩余动作"，而不是重排今天
    if in_workout and (constraints.get("minutes") or constraints.get("easier")
                       or constraints.get("equipment_busy")
                       or constraints.get("injured_regions")
                       or constraints.get("unavailable_equipment")):
        return Decision("adjust_workout", "workout", constraints, candidates,
                        reason="训练中条件变化")

    # 今天的临时变化
    if constraints.get("minutes") or constraints.get("location") or constraints.get("no_gym") \
            or constraints.get("rest") or constraints.get("easier") \
            or constraints.get("equipment_busy") or constraints.get("injured_regions") \
            or constraints.get("unavailable_equipment") or constraints.get("preferred_window"):
        return Decision("adjust_today", "today", constraints, candidates,
                        reason="用户给出今天的临时条件")

    if in_workout and any(word in raw for word in ("累", "困", "没力气")):
        return Decision("adjust_workout", "workout", constraints, candidates,
                        reason="训练中疲劳")

    # 用户对"只改今天 / 改长期 / 调整当前训练"的短回复，不能再问一遍同一句
    if any(word in raw for word in ("先改今天", "只改今天", "就改今天", "改今天",
                                    "先调整今天", "只调整今天")):
        return Decision("adjust_today", "today", constraints, candidates,
                        reason="用户选择只调整今天")
    if any(word in raw for word in ("先改长期", "只改长期", "改长期偏好", "长期偏好")):
        return Decision("update_preference", "long_term", constraints, candidates,
                        reason="用户选择修改长期偏好")

    if any(word in raw for word in ("改", "调整", "换", "修改", "不要", "不想")):
        return Decision("clarify", "none", constraints, candidates,
                        reason="没有识别出可执行的范围")
    return Decision("clarify", "none", constraints, candidates, reason="没有识别出明确需求")


# ---------------------------------------------------------------- 语义兜底


def _extract_json_object(text: str) -> dict:
    raw = (text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(raw[start:end + 1])
    except (TypeError, ValueError):
        return {}


def _clean_model_constraints(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key in ("rest", "train_today", "equipment_busy", "easier", "harder"):
        if raw.get(key) is True:
            out[key] = True
    if raw.get("minutes") not in (None, "", 0):
        try:
            minutes = int(raw["minutes"])
            if 5 <= minutes <= 300:
                out["minutes"] = minutes
        except (TypeError, ValueError):
            pass
    if raw.get("location"):
        out["location"] = str(raw["location"]).strip()[:32]
    if raw.get("exercise"):
        out["exercise"] = str(raw["exercise"]).strip()[:40]
    for key in ("injured_regions", "avoid_actions", "avoid_foods", "foods"):
        value = raw.get(key)
        if isinstance(value, list):
            cleaned = [str(item).strip() for item in value if str(item).strip()]
            if cleaned:
                out[key] = cleaned
    for key in ("meal_slot", "from_food", "to_food", "replacement_food", "food", "note"):
        value = raw.get(key)
        if value not in (None, ""):
            out[key] = str(value).strip()[:80]
    if raw.get("kcal") not in (None, ""):
        try:
            out["kcal"] = int(float(raw["kcal"]))
        except (TypeError, ValueError):
            pass
    if raw.get("protein_g") not in (None, ""):
        try:
            out["protein_g"] = float(raw["protein_g"])
        except (TypeError, ValueError):
            pass
    return out

# 模型轻量级解析
def classify_with_model(client, text: str, *, has_plan: bool, in_workout: bool,
                        page: str, recent: list[dict] | None = None) -> Decision | None:
    """LEGACY：一次性大 JSON 的旧分类器，仅为兼容测试/旧调用保留。

    新的执行入口 `run()` 已改用 `app.agent.intent_router.understand_tasks()` 的单次结构化
    任务理解；新代码不应再调用本函数。
    """
    if client is None or not getattr(client, "available", lambda: False)():
        return None
    intents = " / ".join(INTENT_LABELS.keys())
    prompt = f"""你是健身教练 Agent 的意图分类器。只输出一个 JSON 对象，不要 Markdown。
可选 intent：{intents}
用户当前页面：{page}；是否有计划：{has_plan}；是否在训练中：{in_workout}。
用户原话：{text}
最近对话（从旧到新）：
{chr(10).join(f"{'用户' if item.get('role') == 'user' else '教练'}: {str(item.get('text') or '')[:240]}" for item in (recent or [])[-6:]) or "（无）"}

规则：
- 点餐/订餐/外卖/汉堡/麦当劳/MCP Token + 城市或门店 => mcd_order
- 只改今天的时长/地点/状态/器械 => adjust_today
- 明确长期习惯（以后、通常、一直）=> update_preference
- 看记忆/问记得什么 => query_memory
- 身体部位 + 疼/痛/不舒服/受伤（不在训练中也算）=> injury_advice，
  constraints.injured_regions 填 knee/lower_back/shoulder 等；不要输出 clarify
- 问吃什么/外卖怎么点/热量蛋白/某餐怎么调 => nutrition_advice（没有记录能力也要回答建议）
- 要“每周安排/整周怎么练/整理成能照着做” => plan_overview
- 改目标（减脂↔增肌↔心肺）=> update_preference，不要 create_plan
- 训练中换动作/器械被占/太累/只剩几分钟 => adjust_workout
- 训练中身体不舒服/不想继续/想结束 => adjust_workout，constraints.rest=true
  （不要只减组数；不要判 clarify）
- 训练中状态好/想加量/想练猛一点 => adjust_workout，constraints.harder=true
- 训练中只是有点累/想轻一点 => adjust_workout，constraints.easier=true
- 不在训练中：今天身体不适/不想练 => adjust_today，constraints.rest=true
- 不在训练中：今天状态好/想加量 => adjust_today，constraints.harder=true
- 明确某个部位疼/受伤（如膝盖、腰）=> injury_advice，并填 injured_regions；
  只是“今天不舒服”没有部位 => 按上面的 rest 处理，不要诊断
- 问动作怎么做 => exercise_guidance，并把用户要学的动作填进 constraints.exercise
- 要求结合档案/记忆/最近状态给出下一步建议 => coach_review，不要澄清反问
- 用户只回复“帮我弄一下 / 就按你说的 / 开始吧 / 好的”时，必须结合最近对话里教练上一条建议，
  选择具体可执行 intent（通常是 review_followup，按上一条建议执行），不要输出 clarify
- 今天的整体训练和餐食安排 => today_brief
- 用户说“今天不休息/今天想锻炼/开始练/今天可以锻炼/为今天生成锻炼计划” => today_brief，并且要出训练安排
- 改长期计划结构 => adjust_plan 或 create_plan
- 修改档案字段（体重/身高/出生年/性别/目标/时间/地点/器械/喜恶）=> update_preference
- 用户明确不要/不想某个具体动作（如“不想深蹲”）=> adjust_plan，并抽取要避开的动作
- 今天睡觉/想休息/不练 => adjust_today，并且 constraints.rest=true
- 今天只有 20 分钟 => adjust_today，constraints.minutes=20
- 看不懂才输出 clarify，不要因为没写“麦当劳”三个字就拒绝点餐。
输出 JSON 字段：
intent, scope, reason, city, store, foods（数组）,
constraints: {{
  rest: true/false, train_today: true/false, minutes: 数字或 null,
  location: "home|gym|office|outdoor|"", equipment_busy: true/false,
  easier: true/false, harder: true/false,
  exercise: "用户要学的动作名，没有就空字符串",
  injured_regions: ["knee"...], avoid_actions: ["深蹲"...]
}}
示例：{{"intent":"adjust_today","scope":"today","reason":"今天休息","constraints":{{"rest":true}}}}
"""
    data: dict = {}
    # 一次失败很常见（网络抖动/JSON 截断）；重试一次再回退到规则，
    # 避免用户第一句话偶尔失败就看到“没听出你要改哪一层”。
    for attempt in range(2):
        try:
            try:
                reply = client.complete(
                    [{"role": "user", "content": prompt}],
                    tools=None, temperature=0, max_tokens=800,
                    reasoning_effort="low",
                )
            except TypeError:  # 兼容测试里的假客户端
                reply = client.complete(
                    [{"role": "user", "content": prompt}],
                    tools=None, temperature=0, max_tokens=800,
                )
            content = (reply.get("message") or {}).get("content") or ""
            data = _extract_json_object(content)
            if data:
                break
        except Exception:  # noqa: BLE001
            if attempt == 1:
                return None
    if not data:
        return None
    intent = str(data.get("intent") or "").strip()
    if intent not in INTENT_LABELS or intent in ("safety",):
        return None
    scope = str(data.get("scope") or "none").strip() or "none"
    constraints = _clean_model_constraints(data.get("constraints"))
    if intent == "mcd_order":
        if data.get("city"):
            constraints["mcd_city"] = str(data["city"]).strip()
        if data.get("store"):
            constraints["mcd_store"] = str(data["store"]).strip()
        foods = data.get("foods")
        if isinstance(foods, list):
            constraints["mcd_foods"] = [str(item).strip() for item in foods if str(item).strip()]
    if intent == "adjust_today" and not constraints:
        # 意图已经对了但模型没展开参数：让模型只做一次参数抽取，不靠规则关键词
        try:
            detail_prompt = (
                "把用户今天的状态变化抽成 JSON constraints，只输出对象，不要解释。"
                "字段：rest, train_today, minutes, location, equipment_busy, easier, harder, "
                "injured_regions, avoid_actions。用户原话：" + text
            )
            try:
                detail = client.complete(
                    [{"role": "user", "content": detail_prompt}],
                    tools=None, temperature=0, max_tokens=500,
                    reasoning_effort="low",
                )
            except TypeError:
                detail = client.complete(
                    [{"role": "user", "content": detail_prompt}],
                    tools=None, temperature=0, max_tokens=500,
                )
            constraints = _clean_model_constraints(
                _extract_json_object((detail.get("message") or {}).get("content") or "")
            )
        except Exception:
            constraints = {}
    return Decision(intent, scope, constraints, [],
                    reason=str(data.get("reason") or "模型语义解析")[:120])


# ---------------------------------------------------------------- 分层理解

def _task_context(recent: list[dict]) -> dict:
    """独立于聊天记录的“当前任务状态”：上一轮助手在等什么、挂了哪些按钮。"""
    last_agent = next(
        (item for item in reversed(recent or []) if item.get("role") == "agent"), None,
    )
    if not last_agent:
        return {"last_agent_intent": "", "pending_actions": [],
                "awaiting_confirmation": False, "last_result_kind": ""}
    pending = [
        {"type": action.get("type"), "label": action.get("label")}
        for action in (last_agent.get("actions") or [])
        if isinstance(action, dict) and action.get("type") in ("quick_reply", "confirm")
    ]
    return {
        "last_agent_intent": last_agent.get("intent") or "",
        "last_agent_text": str(last_agent.get("text") or "")[:300],
        "pending_actions": pending,
        "awaiting_confirmation": any(
            "确认" in str(action.get("label") or "") for action in pending
        ),
        "last_result_kind": last_agent.get("result_kind") or "",
        "last_task_plan": last_agent.get("task_plan") or [],
        "last_task_results": last_agent.get("task_results") or [],
    }


def _intent_context(
    db: Session, user: User, day: dt.date, plan, active: WorkoutSession | None,
    profile: Profile | None, page: str, recent: list[dict],
) -> dict:
    """把理解用户所需的"结构化状态"一次性交给模型，而不是只给最近六句文本。"""
    today = effective_day(db, user, day, fallback_next=False)
    all_actions = []
    visible_actions = []
    for index, action in enumerate(today.get("actions") or []):
        entry = {
            "index": index + 1,
            "slot": action.get("slot"),
            "name": action.get("name") or (action.get("meta") or {}).get("name_zh")
                    or action.get("action_ref"),
            "ref": action.get("action_ref"),
            "sets": action.get("sets"),
            "reps": action.get("reps"),
            "minutes": action.get("duration_min"),
        }
        all_actions.append(entry)
        if entry["slot"] != "warmup":
            visible_actions.append({**entry, "index": len(visible_actions) + 1})
    day_actions = visible_actions
    workout = None
    if active is not None:
        payload = workout_service.session_payload(active)
        current = payload.get("current_item") or {}
        workout = {
            "status": payload.get("status"),
            "current_index": (payload.get("current_index") or 0) + 1,
            "total": len(payload.get("items") or []),
            "current": {
                "name": current.get("name"),
                "ref": current.get("action_ref"),
                "sets": current.get("target_sets"),
                "reps": current.get("target_reps"),
            },
            "items": [
                {"index": index + 1, "name": item.get("name"),
                 "ref": item.get("action_ref"), "status": item.get("status"),
                 "sets": item.get("target_sets"), "completed_sets": item.get("completed_sets")}
                for index, item in enumerate(payload.get("items") or [])
            ],
            "visible_items": [
                {"index": index + 1, "name": item.get("name"),
                 "ref": item.get("action_ref"), "status": item.get("status"),
                 "sets": item.get("target_sets"), "completed_sets": item.get("completed_sets")}
                for index, item in enumerate(payload.get("items") or [])
                if item.get("slot") != "warmup"
            ],
        }
    profile_facts = {}
    if profile is not None:
        profile_facts = {
            "goal": profile.goal,
            "weekly_days": profile.weekly_days,
            "session_minutes": profile.session_minutes,
            "equipment": list(profile.equipment or []),
            "preferred_location": profile.preferred_location,
            "preferred_time_window": profile.preferred_time_window,
            "experience": profile.training_experience,
            "diet_preferences": list(profile.diet_preferences or []),
            "allergies": list(profile.allergies or []),
        }
    food_memory = [
        {"category": row.category, "value": row.value}
        for row in list_memories(db, user)
        if row.status == "active" and row.category in (
            "food_like", "food_dislike", "food_allergy")
    ]
    from app.agent.conversation_state import history_context
    return {
        "execution_history": history_context(db, user, db.info.get("coach_thread", "coach"), day),
        "page": page,
        "has_plan": plan is not None,
        "in_workout": active is not None,
        "plan_version": getattr(plan, "current_version", None),
        "date": day.isoformat(),
        "meals": today.get("meals") or [],
        "today_plan": {
            "summary": today.get("summary") or "",
            "source": today.get("source"),
            "actions": day_actions,
            "warmup": [item for item in all_actions if item["slot"] == "warmup"],
        },
        "temporary_context": today.get("temporary_context") or {},
        "current_workout": workout,
        "profile": profile_facts,
        "food_preferences": food_memory,
        "task_context": _task_context(recent),
        "recent": [
            {"role": item.get("role"), "text": str(item.get("text") or "")[:240]}
            for item in (recent or [])[-8:]
        ],
    }


def _slot_candidates(slots: dict) -> list[dict]:
    """把 update_preference 的结构化槽位翻译成 Profile/Memory 候选。"""
    out: list[dict] = []
    goal = str(slots.get("goal") or "")
    if goal:
        out.append({"kind": "long_term", "category": "goal", "key": "primary_goal",
                    "value": _goal_label(goal), "structured": {"goal": goal},
                    "reason": "模型抽取的长期目标"})
    for key, category, label in (
        ("weekly_days", "weekly_days", "每周 {} 天"),
        ("session_minutes", "session_minutes", "每次 {} 分钟"),
    ):
        value = slots.get(key)
        if value:
            try:
                number = int(value)
                out.append({"kind": "long_term", "category": category, "key": category,
                            "value": label.format(number), "structured": {"value": number},
                            "reason": "模型抽取的长期训练条件"})
            except (TypeError, ValueError):
                pass
    location = str(slots.get("location") or "")
    if location:
        labels = {"home": "家里", "gym": "健身房", "office": "办公室", "outdoor": "户外"}
        out.append({"kind": "long_term", "category": "location", "key": "preferred_location",
                    "value": labels.get(location, location), "structured": {"location": location},
                    "reason": "模型抽取的长期训练地点"})
    equipment = slots.get("equipment")
    if isinstance(equipment, list) and equipment:
        labels = {"dumbbell": "哑铃", "band": "弹力带", "bench": "训练凳",
                  "treadmill": "跑步机", "mat": "瑜伽垫", "barbell": "杠铃"}
        items = [str(item) for item in equipment if str(item).strip()]
        out.append({"kind": "long_term", "category": "equipment", "key": "available",
                    "value": "、".join(labels.get(item, item) for item in items),
                    "structured": {"items": items},
                    "reason": "模型抽取的器械条件"})
    for category, field, prefix in (
        ("dislike", "dislikes", "不喜欢"), ("like", "likes", "喜欢"),
        ("food_dislike", "disliked_foods", "不喜欢吃"),
        ("food_like", "liked_foods", "喜欢吃"),
        ("food_allergy", "allergen_foods", "对"),
    ):
        values = slots.get(field)
        if isinstance(values, list):
            for name in values:
                name = str(name).strip()
                if name:
                    structured = ({"food": name} if category.startswith("food_")
                                  else {"action": name})
                    out.append({"kind": "long_term", "category": category, "key": name,
                                "value": f"{prefix}{name}", "structured": structured,
                                "reason": "模型抽取的饮食/动作偏好"})
    window = str(slots.get("preferred_time_window") or "")
    if window:
        out.append({"kind": "long_term", "category": "schedule", "key": window,
                    "value": f"训练时间 {window}", "structured": {"window": window},
                    "reason": "模型抽取的长期训练时间"})
    for field, category, label in (
        ("weight_kg", "weight_kg", "体重"), ("height_cm", "height_cm", "身高"),
        ("birth_year", "birth_year", "出生年"),
    ):
        value = slots.get(field)
        if value not in (None, ""):
            out.append({"kind": "long_term", "category": category, "key": category,
                        "value": f"{label} {value}", "structured": {"value": value},
                        "reason": "模型抽取的身体数据"})
    sex = str(slots.get("sex") or "")
    if sex in ("male", "female"):
        out.append({"kind": "long_term", "category": "sex", "key": "sex",
                    "value": "性别" + ("男" if sex == "male" else "女"),
                    "structured": {"value": sex}, "reason": "模型抽取的性别"})
    return out


def _resolve_scope(decision: Decision, active: WorkoutSession | None,
                   plan) -> Decision:
    """Stage 3：范围判断的确定性收口，避免模型把“今天”说成长期计划。"""
    intent = decision.intent
    if intent == "adjust_workout":
        if active is None and plan is not None:
            # 不在训练中却说要改“当前训练”，按今天处理，避免凭空开一场训练。
            decision.intent = "adjust_today"
            decision.scope = "today"
        else:
            decision.scope = "current_workout"
    elif intent == "adjust_today":
        decision.scope = "today"
    elif intent in ("update_preference", "adjust_plan", "create_plan"):
        decision.scope = "long_term"
    elif intent == "injury_advice":
        decision.scope = "today"
    elif intent == "exercise_guidance":
        decision.scope = "current_workout" if active is not None else "message"
    elif intent in ("query_memory", "nutrition_advice"):
        decision.scope = "message"
    elif intent == "mcd_order":
        decision.scope = "meal"
    return decision

# decision
def _decision_from_intent(result: IntentResult, text: str,
                          active: WorkoutSession | None = None,
                          plan=None) -> Decision:
    intent = result.intent
    constraints = _clean_model_constraints(result.slots)
    if intent == "mcd_order":
        if result.slots.get("city"):
            constraints["mcd_city"] = str(result.slots["city"])
        if result.slots.get("store"):
            constraints["mcd_store"] = str(result.slots["store"])
        foods = result.slots.get("foods")
        if isinstance(foods, list):
            constraints["mcd_foods"] = [str(item) for item in foods if str(item).strip()]
    candidates = extract_candidates(text) if intent in (
        "update_preference", "create_plan", "adjust_plan") else []
    if intent == "update_preference":
        seen = {(item.get("category"), item.get("key")) for item in candidates}
        for item in _slot_candidates(result.slots):
            key = (item.get("category"), item.get("key"))
            if key not in seen:
                candidates.append(item)
                seen.add(key)
    scope = result.scope if result.scope not in ("", "none") else (
        INTENT_SPECS_SCOPE.get(intent, "none"))
    decision = Decision(
        intent, scope, constraints, candidates,
        reason=result.reason or "分层意图理解",
        confidence=result.confidence,
        missing_slots=list(result.missing_slots),
        clarification_needed=result.clarification_needed,
        secondary_intents=list(result.secondary_intents),
        user_goal=result.user_goal,
        context_reference=dict(result.context_reference or {}),
        action=result.action or INTENT_ACTIONS.get(intent, ""),
        source=result.source,
        understanding_error=result.error,
    )
    return _resolve_scope(decision, active, plan)


INTENT_ACTIONS = {
    "create_plan": "generate_plan", "adjust_today": "modify_today",
    "adjust_workout": "modify_current_workout", "adjust_plan": "patch_plan",
    "update_preference": "update_profile", "injury_advice": "safety_advice",
    "nutrition_advice": "answer_nutrition", "exercise_guidance": "teach_exercise",
    "query_memory": "read_memory", "edit_memory": "edit_memory",
    "today_brief": "brief_today", "plan_overview": "show_plan",
    "progress_review": "review_progress", "coach_review": "coach_review",
    "review_followup": "execute_suggestion", "log_feedback": "log_workout",
    "mcd_order": "order_meal",
}


INTENT_SPECS_SCOPE = {
    "create_plan": "long_term", "adjust_today": "today", "adjust_workout": "current_workout",
    "adjust_plan": "long_term", "update_preference": "long_term",
    "injury_advice": "today", "nutrition_advice": "message",
    "exercise_guidance": "message", "query_memory": "message",
    "edit_memory": "long_term", "today_brief": "today", "plan_overview": "long_term",
    "progress_review": "message", "coach_review": "long_term",
    "review_followup": "today", "log_feedback": "current_workout", "mcd_order": "meal",
}


def _has_actionable_slots(constraints: dict) -> bool:
    return any(bool(constraints.get(key)) for key in (
        "rest", "minutes", "location", "equipment_busy",
        "injured_regions", "avoid_actions", "exercise",
    ))


def _handle_adjust_clarify(decision: Decision, trace: list[dict]) -> dict:
    """意图已确定、只缺调整方向：带着 intent 追问，而不是回到通用兜底。"""
    _push(trace, "clarify", "missing_slots", slots=decision.missing_slots)
    if decision.intent == "adjust_workout":
        reply = "我明白你想调整现在这场训练。你想怎么调？"
        options = [
            {"type": "quick_reply", "label": "休息，今天不练了",
             "text": "我不舒服，今天就别练了"},
            {"type": "quick_reply", "label": "减一组", "text": "减一组"},
            {"type": "quick_reply", "label": "换个动作", "text": "换个动作"},
            {"type": "quick_reply", "label": "加一点强度", "text": "加一点强度"},
        ]
    else:
        reply = "我明白你想调整今天的安排。你想怎么调？"
        options = [
            {"type": "quick_reply", "label": "今天休息",
             "text": "今天想休息，把训练换成拉伸"},
            {"type": "quick_reply", "label": "缩短时间", "text": "今天只有 20 分钟"},
            {"type": "quick_reply", "label": "换个地点", "text": "今天在家练"},
            {"type": "quick_reply", "label": "加一点强度", "text": "今天状态好，加一点强度"},
        ]
    return {"reply": reply, "actions": options, "changes": [],
            "intent": decision.intent, "scope": decision.scope}


def _dispatch_decision(
    db: Session, user: User, day: dt.date, text: str, decision: Decision,
    trace: list[dict], active: WorkoutSession | None, client, recent: list[dict],
) -> dict:
    """把一个 Decision 路由到现有业务 Handler。"""
    result = _base_result(decision, trace)
    try:
        if decision.intent == "safety":
            result.update(_safety_reply(trace))
        elif (decision.clarification_needed
              and decision.intent in ("adjust_today", "adjust_workout")
              and not _has_actionable_slots(decision.constraints)):
            # 已经知道"要调整今天/当前训练"，只是缺方向：带着 intent 追问。
            result.update(_handle_adjust_clarify(decision, trace))
        elif decision.intent == "query_memory":
            result.update(_handle_query_memory(db, user, text))
        elif decision.intent == "edit_memory":
            result.update(_handle_edit_memory(db, user, text, trace))
        elif decision.intent == "create_plan":
            result.update(_handle_create_plan(db, user, text, decision, trace, client))
        elif decision.intent == "adjust_plan":
            result.update(_handle_adjust_plan(db, user, text, decision, trace, client))
        elif decision.intent == "injury_advice":
            result.update(_handle_injury(db, user, text, decision, trace))
        elif (decision.intent == "nutrition_advice"
              and decision.operation in NUTRITION_OPERATIONS):
            result.update(_handle_nutrition_operation(
                db, user, day, text, decision, trace, client,
            ))
        elif decision.intent == "nutrition_advice":
            result.update(_handle_nutrition(db, user, day, text, decision, trace, client))
        elif decision.intent == "plan_overview":
            result.update(_handle_plan_overview(db, user, trace))
        elif decision.intent == "update_preference":
            result.update(_handle_update_preference(db, user, text, decision, trace))
        elif decision.intent == "adjust_today":
            result.update(_handle_adjust_today(db, user, day, text, decision, trace, active))
        elif decision.intent == "adjust_workout":
            result.update(_handle_adjust_workout(
                db, user, day, text, decision, active, trace
            ))
        elif decision.intent == "exercise_guidance":
            result.update(_handle_guidance(
                db, user, day, text, decision, active, trace
            ))
        elif decision.intent == "log_feedback":
            result.update(_handle_feedback(
                db, user, day, text, decision, active, trace
            ))
        elif decision.intent == "today_brief":
            result.update(_handle_today_brief(db, user, day, text, trace))
        elif decision.intent == "progress_review":
            result.update(_handle_progress_review(db, user, trace))
        elif decision.intent == "coach_review":
            result.update(_handle_coach_review(db, user, day, trace))
        elif decision.intent == "review_followup":
            result.update(_handle_review_followup(db, user, day, recent, trace))
        elif decision.intent == "mcd_order":
            result.update(_handle_mcd_order(text, decision, trace))
        else:
            result.update(_handle_clarify(db, user, day, text, decision, trace))
    except Exception as exc:  # noqa: BLE001 - Agent 不能把页面打挂
        db.rollback()
        trace.append({"node": "execute", "status": "failed", "ts": _now(),
                      "detail": {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}})
        result.update({
            "reply": f"这次调整没成功：{type(exc).__name__}。你的原始计划没有被改动。",
            "error": str(exc)[:200],
            "changes": [],
        })

    return result


def _decision_from_task(task: dict, text: str, active: WorkoutSession | None,
                        plan) -> Decision:
    result = IntentResult(
        intent=task["intent"], scope=task.get("scope") or "today",
        confidence=float(task.get("confidence") or 0.8),
        reason=task.get("user_goal") or "",
        slots=dict(task.get("slots") or {}),
        missing_slots=list(task.get("missing_slots") or []),
        clarification_needed=bool(task.get("clarification_needed")),
        context_reference=dict(task.get("context_reference") or {}),
        source="model_task",
    )
    decision = _decision_from_intent(result, text, active=active, plan=plan)
    decision.task_id = str(task.get("task_id") or "")
    decision.operation = str(task.get("operation") or "")
    decision.action_type = str(task.get("action_type") or "")
    decision.domain = str(task.get("domain") or "")
    return decision


def _pick_stretch_replacement() -> dict:
    return {"ref": "full_body_stretch", "name": "全身拉伸", "equipment": "bodyweight",
            "movement_mode": "mobility", "primary_muscle": "mobility",
            "meta": {"name_zh": "全身拉伸"}}


def _ordinal_indexes(text: str) -> list[int]:
    """参数兜底：只解析“第几个/最后一个”，不判断意图。"""
    mapping = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    found: list[tuple[int, int]] = []
    for match in re.finditer(r"第([一二两三四五六七八九十\d]+)个|最后(?:一)?个", text):
        if match.group(1):
            raw = match.group(1)
            value = int(raw) if raw.isdigit() else mapping.get(raw, 0)
        else:
            value = -1
        if value:
            found.append((match.start(), value))
    return [value for _, value in sorted(found)]


def _pick_action_for_muscle(profile: Profile | None, muscle: str,
                            used: set[str]) -> dict | None:
    from app.domain.catalog import EXTRA_ZH
    from app.domain.exercise_catalog import EXERCISE_CATALOG

    wanted = {
        "chest": {"chest"}, "back": {"back", "lats", "upper back"},
        "legs": {"quads", "hamstrings", "legs", "glutes"},
        "shoulders": {"shoulders", "rear delts"}, "arms": {"biceps", "triceps"},
        "core": {"core"}, "glutes": {"glutes"},
    }.get(muscle, {muscle})
    allowed = allowed_equipment(profile, {})
    equipment_map = {
        "bodyweight": "Bodyweight", "dumbbell": "Dumbbell",
        "band": "Resistance Band", "bench": "Bench", "mat": "Bodyweight",
        "barbell": "Barbell", "kettlebell": "Kettlebell",
    }
    allowed_names = {equipment_map.get(item, item) for item in allowed} | {"Bodyweight"}
    rows = []
    for ref, item in EXERCISE_CATALOG.items():
        if ref in used:
            continue
        if str(item.get("primary_muscle") or "").lower() not in wanted:
            continue
        if str(item.get("equipment") or "") not in allowed_names:
            continue
        name = item.get("name_zh") or EXTRA_ZH.get(ref) or item.get("name_en") or ref
        squat_bonus = 0
        if muscle == "legs" and ("squat" in ref.lower() or "蹲" in name):
            squat_bonus = -1
        rows.append((squat_bonus,
                     0 if item.get("equipment") == "Bodyweight" else 1,
                     0 if item.get("name_zh") or EXTRA_ZH.get(ref) else 1, ref, item, name))
    if not rows:
        return None
    rows.sort(key=lambda row: (row[0], row[1], row[2], len(row[3])))
    _, _, _, ref, item, name = rows[0]
    return {"ref": ref, "name": name, "equipment": str(item.get("equipment") or "Bodyweight"),
            "movement_mode": "", "primary_muscle": str(item.get("primary_muscle") or ""),
            "meta": {"name_zh": name, "name_en": item.get("name_en") or ""}}


def _pick_action_by_name(name: str) -> dict | None:
    from app.domain.catalog import EXTRA_ZH
    from app.domain.exercise_catalog import EXERCISE_CATALOG

    lowered = str(name or "").lower()
    for ref, item in EXERCISE_CATALOG.items():
        zh = item.get("name_zh") or EXTRA_ZH.get(ref) or ""
        if lowered and (lowered in zh.lower() or lowered in str(item.get("name_en") or "").lower()
                        or lowered in ref.lower()):
            label = zh or item.get("name_en") or ref
            return {"ref": ref, "name": label,
                    "equipment": str(item.get("equipment") or "Bodyweight"),
                    "movement_mode": "", "primary_muscle": str(item.get("primary_muscle") or ""),
                    "meta": {"name_zh": label, "name_en": item.get("name_en") or ""}}
    return None


def _apply_today_operations(
    db: Session, user: User, day: dt.date, text: str, tasks: list[dict],
    trace: list[dict],
) -> dict:
    current = effective_day(db, user, day, fallback_next=False)
    actions = [dict(item) for item in (current.get("actions") or [])]
    if not actions:
        return {"reply": "今天还没有可调整的训练动作。", "changes": [],
                "day": current, "task_results": [
                    {"task_id": t["task_id"], "status": "failed",
                     "message": "今天没有可调整的动作"} for t in tasks]}
    visible = [item for item in actions if item.get("slot") != "warmup"]
    changes: list[str] = []
    task_results: list[dict] = []
    fallback_indexes = _ordinal_indexes(text)
    for order, task in enumerate(tasks):
        slots = task.get("slots") or {}
        operation = str(task.get("operation") or "")
        if operation == "add_action":
            goal = str(task.get("user_goal") or "")
            if "拉伸" in goal and not slots.get("target_category"):
                slots = {**slots, "target_category": "stretching"}
            if "蹲" in goal and not slots.get("target_action"):
                slots = {**slots, "target_action": "深蹲"}
            task = {**task, "slots": slots}
            muscle = str(slots.get("target_muscle") or "").strip().lower()
            category = str(slots.get("target_category") or "")
            target_action = str(slots.get("target_action") or "").strip()
            if not muscle and not category and not target_action:
                task_results.append({"task_id": task["task_id"],
                                     "status": "clarification_required",
                                     "message": "想新增哪个部位的动作？练胸、练背还是练腿？"})
                continue
            used = {str(item.get("action_ref") or "") for item in actions}
            category = str(slots.get("target_category") or "")
            target_action = str(slots.get("target_action") or "").strip()
            if category == "stretching":
                picked = _pick_stretch_replacement()
            elif target_action:
                picked = _pick_action_by_name(target_action)
                if picked is None and muscle:
                    profile = db.get(Profile, user.id) if db is not None else None
                    picked = _pick_action_for_muscle(profile, muscle, used)
            else:
                profile = db.get(Profile, user.id) if db is not None else None
                picked = _pick_action_for_muscle(profile, muscle, used)
            if picked is None:
                task_results.append({"task_id": task["task_id"], "status": "failed",
                                     "message": f"当前器械条件下没有找到适合练{muscle}的动作。"})
                continue
            actions.append({
                "slot": "mobility" if category == "stretching" else "strength",
                "action_ref": picked["ref"], "name": picked["name"],
                "sets": None if category == "stretching" else 2,
                "reps": None if category == "stretching" else 10,
                "duration_min": 5 if category == "stretching" else 8,
                "equipment": picked["equipment"], "movement_mode": picked["movement_mode"],
                "primary_muscle": picked["primary_muscle"], "meta": picked["meta"],
                "source": "agent",
                "reason": (f"按你的要求新增{target_action or ('拉伸' if category == 'stretching' else '练' + {'chest':'胸','back':'背','legs':'腿','shoulders':'肩','arms':'手臂','core':'核心','glutes':'臀'}.get(muscle, muscle))}动作"),
            })
            label = target_action or ("拉伸" if category == "stretching" else f"练{muscle}")
            muscle_cn = {"chest": "胸", "back": "背", "legs": "腿", "shoulders": "肩",
                         "arms": "手臂", "core": "核心", "glutes": "臀"}.get(muscle, muscle)
            if category == "stretching":
                label_cn = label
            elif target_action:
                label_cn = target_action
            else:
                label_cn = f"练{muscle_cn}"
            changes.append(f"今天新增「{picked['name']}」（{label_cn}）")
            task_results.append({"task_id": task["task_id"], "status": "success",
                                 "message": changes[-1], "changes": [changes[-1]]})
            continue
        try:
            index = int(slots.get("target_index") or 0)
        except (TypeError, ValueError):
            index = 0
        if not index and order < len(fallback_indexes):
            index = fallback_indexes[order]
        if index == -1:
            index = len(visible)
        target = visible[index - 1] if 1 <= index <= len(visible) else None
        hint = str(slots.get("target_action") or slots.get("exercise")
                   or (slots.get("avoid_actions") or [""])[0] or "").strip()
        target_muscle = str(slots.get("target_muscle") or "").strip().lower()
        if target is None and target_muscle:
            muscle_map = {
                "chest": {"chest"}, "back": {"back", "lats", "upper back"},
                "legs": {"quads", "hamstrings", "legs", "glutes"},
                "shoulders": {"shoulders", "rear delts"}, "arms": {"biceps", "triceps"},
                "core": {"core"}, "glutes": {"glutes"},
            }
            wanted = muscle_map.get(target_muscle, {target_muscle})
            target = next((item for item in visible
                           if str(item.get("primary_muscle") or "").lower() in wanted), None)
        if target is None and hint:
            lowered = hint.lower()
            target = next((item for item in visible
                           if lowered in str(item.get("name") or "").lower()
                           or lowered in str(item.get("action_ref") or "").lower()), None)
        if target is None:
            message = "你想调整的是第几个动作？可以说“第一个/最后一个”，或直接说动作名。"
            if hint:
                message = f"今天的计划里没有找到「{hint}」，所以没有改动。"
            task_results.append({
                "task_id": task["task_id"], "status": "clarification_required",
                "message": message,
            })
            continue
        old = str(target.get("name") or target.get("action_ref") or "这个动作")
        if operation in ("skip_action", "remove_action"):
            actions = [item for item in actions if item is not target]
            changes.append(f"今天跳过「{old}」")
        elif operation == "replace_action":
            category = str(slots.get("replacement_category") or "")
            requested = str(slots.get("replacement_exercise") or "")
            replacement = (_pick_action_by_name(requested) if requested else
                           _pick_stretch_replacement() if category == "stretching" else None)
            if requested and replacement is None:
                task_results.append({"task_id": task["task_id"], "status": "failed",
                                     "message": "没有找到指定的替换动作。"})
                continue
            if replacement is None:
                replacement = pick_alternative(
                    target, allowed=allowed_equipment(
                        db.get(Profile, user.id) if db is not None else None, {}),
                    injured=[], used={str(item.get("action_ref") or "") for item in actions},
                    prefer_bodyweight=True,
                )
            if replacement is None:
                task_results.append({"task_id": task["task_id"], "status": "failed",
                                     "message": f"没有找到能替换「{old}」的动作。"})
                continue
            for cursor, item in enumerate(actions):
                if item is target:
                    actions[cursor] = {
                        **item, "replaced_from": item.get("action_ref"),
                        "action_ref": replacement["ref"], "name": replacement["name"],
                        "slot": "mobility" if category == "stretching" else item.get("slot"),
                        "sets": None if category == "stretching" else item.get("sets"),
                        "reps": None if category == "stretching" else item.get("reps"),
                        "duration_min": 5 if category == "stretching" else item.get("duration_min"),
                        "equipment": replacement.get("equipment") or "bodyweight",
                        "movement_mode": replacement.get("movement_mode") or "",
                        "primary_muscle": replacement.get("primary_muscle") or "",
                        "meta": replacement.get("meta") or {},
                        "source": "agent", "reason": "按你的指代替换",
                    }
                    break
            changes.append(f"今天把「{old}」换成「{replacement['name']}」")
        if task_results and task_results[-1]["task_id"] == task["task_id"]:
            if task_results[-1]["status"] != "failed":
                task_results[-1].update({"status": "success", "message": changes[-1],
                                         "changes": [changes[-1]]})
        else:
            task_results.append({"task_id": task["task_id"], "status": "success",
                                 "message": changes[-1], "changes": [changes[-1]]})
    if not changes:
        return {"reply": "这次没有改动今天计划；你想调整第几个动作？",
                "changes": [], "day": current, "task_results": task_results}
    summary = "今天：" + "；".join(changes)
    override = save_override(
        db, user, day, actions=actions, meals=current.get("meals") or [], summary=summary,
        constraints={**(current.get("constraints") or {}),
                     "operations": [t.get("operation") for t in tasks]},
        reason=text, base_version=current.get("plan_version"), expected_adjustment_id=current.get("adjustment_id"),
        borrowed_from=current.get("borrowed_from"), changes=changes,
    )
    _push(trace, "task", "today_operations", changes=len(changes), tasks=len(tasks))
    return {"reply": summary + "。长期计划没有变。", "changes": changes,
            "day": effective_day(db, user, day, fallback_next=False),
            "actions": [{"type": "reset_day", "label": "恢复原计划", "date": day.isoformat()}],
            "task_results": task_results, "override": override}


def _execute_task_plan(
    db: Session, user: User, day: dt.date, text: str, tasks: list[dict],
    trace: list[dict], active: WorkoutSession | None, client, recent: list[dict],
    plan=None,
) -> dict:
    from app.agent.legacy_task_executor import execute_tasks
    return execute_tasks(db, user, day, text, tasks, trace, active, client, recent, plan)


def _legacy_run(
    db: Session,
    user: User,
    text: str,
    *,
    page: str = "today",
    day: dt.date | None = None,
    session_id: str | None = None,
    page_context: dict | None = None,
    thread: str = "coach",
    client=None,
) -> dict:
    started = time.monotonic()
    day = day or user_today(db.get(Profile, user.id))
    text = (text or "").strip()
    thread = normalize_thread(thread)
    db.info["coach_thread"] = thread
    trace: list[dict] = []
    _push(trace, "context", "loading", page=page, date=day.isoformat(),
          context_keys=sorted((page_context or {}).keys()))
    active = workout_service.active_session(db, user, day)
    if session_id:
        candidate = db.get(WorkoutSession, session_id)
        if candidate is not None and candidate.user_id == user.id \
                and candidate.status in ("active", "paused", "completed"):
            active = candidate
    plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    profile = db.get(Profile, user.id)

    memory = load_memory_context(db, user)

    training_state = recent_training_state(db, user)
    recent = recent_messages(db, user, thread)

    rule_decision = classify(
        text, has_plan=plan is not None, in_workout=active is not None, page=page,
        recent=recent,
    )
    decision = rule_decision
    result: dict | None = None
    semantic_used = False

    if rule_decision.intent != "safety" and client is not None:
        context = _intent_context(db, user, day, plan, active, profile, page, recent)
        # 任务列表
        tasks, raw1, raw2, task_error = understand_tasks(
            client, text, context=context, fallback_intent=rule_decision.intent,
        )
        _push(
            trace, "task_decomposition",
            "ok" if tasks else "fallback",
            tasks=[{"task_id": t["task_id"], "intent": t["intent"],
                    "operation": t["operation"], "scope": t["scope"]}
                   for t in tasks],
            error=task_error[:160],
            semantic_count=len(tasks),
        )
        if tasks:
            decision = _decision_from_task(tasks[0], text, active=active, plan=plan)
            decision.secondary_intents = [t["intent"] for t in tasks[1:]]
            semantic_used = True
            if decision.intent == "update_preference" and "确认更新档案" not in text:
                food_only = bool(decision.candidates) and all(
                    item.get("category") in ("food_like", "food_dislike", "food_allergy")
                    for item in decision.candidates
                )
                # 饮食喜欢/过敏是低风险事实，直接保存；“以后别安排深蹲”这类训练偏好仍需确认。
                decision.needs_confirmation = not food_only
            # 一次输入统一生成 TaskPlan；单任务也走同一个执行器，保证
            # task_plan / task_results / 后置条件对所有入口一致。
            _push(trace, "task", "orchestrator_start", tasks=len(tasks))
            try:
                result = _execute_task_plan(
                    db, user, day, text, tasks, trace, active, client, recent, plan,
                )
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                _push(trace, "task", "orchestrator_failed",
                      error=f"{type(exc).__name__}: {str(exc)[:200]}")
                result = _base_result(decision, trace)
                result.update(reply="任务执行失败，请查看任务记录后重试。", error="execution_failed")
        else:
            decision = rule_decision
            decision.understanding_error = task_error
            # A failed semantic parse must never fall through to a mutation rule.
            result = _base_result(decision, trace)
            result.update(reply="这次未能可靠解析任务，请明确要修改的对象和范围。",
                          error=task_error, overall_status="clarification_required")

    if result is None:
        result = _dispatch_decision(
            db, user, day, text, decision, trace, active, client, recent,
        )

    _push(trace, "intent", "ok",
          intent=decision.intent, scope=decision.scope,
          constraints={key: value for key, value in decision.constraints.items()
                       if value not in (None, [], {}, False)})
    if decision.intent == "update_preference" and "确认更新档案" not in text:
        food_only = bool(decision.candidates) and all(
            item.get("category") in ("food_like", "food_dislike", "food_allergy")
            for item in decision.candidates
        )
        decision.needs_confirmation = not food_only

    saved_memories = (_persist_candidates(db, user, decision, text, trace)
                      if result.get("execution_mode", "execute") == "execute" and not result.get("error") else [])
    if saved_memories:
        result["memory_saved"] = [memory_payload(row) for row in saved_memories]
    elif result.get("memory_saved"):
        result["memory_saved"] = list(result["memory_saved"])
    else:
        result["memory_saved"] = []
    if result["memory_saved"] and not result.get("memories"):
        result["memories"] = [memory_payload(row) for row in list_memories(db, user)]

    result["page_context"] = page_context or {}
    result["training_state"] = training_state
    memory_after = load_memory_context(db, user)
    result["memory_context"] = {
        "preferred_windows": memory_after.get("preferred_windows") or [],
        "location": memory_after.get("location") or "",
        "dislikes": memory_after.get("dislikes") or [],
        "long_term_count": len(memory_after.get("long_term") or []),
    }
    result["reply"] = _polish_reply(result, decision, text, saved_memories)
    result["latency_ms"] = round((time.monotonic() - started) * 1000)
    result["trace"] = trace
    result["run_id"] = _record_run(
        db, user, text, page, day, decision, result, trace,
        page_context=page_context, thread=thread,
    )
    _record_messages(db, user, text, result, thread=thread)
    db.commit()
    return result


def _base_result(decision: Decision, trace: list[dict]) -> dict:
    return {
        "intent": decision.intent,
        "intent_label": INTENT_LABELS.get(decision.intent, decision.intent),
        "scope": decision.scope,
        "reason": decision.reason,
        "understanding": {
            "confidence": decision.confidence,
            "missing_slots": decision.missing_slots,
            "clarification_needed": decision.clarification_needed,
            "secondary_intents": decision.secondary_intents,
            "user_goal": decision.user_goal,
            "action": decision.action,
            "source": decision.source,
            "error": decision.understanding_error,
        },
        "reply": "",
        "changes": [],
        "actions": [],
        "day": None,
        "session": None,
        "plan_draft": None,
        "patch_request": None,
        "today_brief": None,
        "today_brief_run_id": "",
        "mcd_order": None,
        "memories": None,
        "memory_saved": [],
        "guidance": [],
        "warnings": [],
        "blocked": False,
        "trace": trace,
    }


def _push(trace: list[dict], node: str, status: str, **detail) -> None:
    trace.append({"node": node, "status": status, "ts": _now(), "detail": detail})


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 各意图处理


def _safety_reply(trace: list[dict]) -> dict:
    _push(trace, "safety", "blocked")
    return {
        "reply": "你提到的症状需要先做医疗评估，今天先不安排训练。等症状明确后再恢复。",
        "blocked": True,
        "warnings": ["本内容不构成医疗建议；如身体不适请立即就医。"],
        "actions": [{"type": "open_knowledge", "label": "查看安全建议",
                     "question": "出现这些症状时能训练吗"}],
    }


def _goal_label(code: str) -> str:
    return {
        "fat_loss": "减脂", "muscle_gain": "增肌",
        "cardio": "心肺", "health": "综合健康",
    }.get(str(code or ""), str(code or "未设置"))


def _handle_query_memory(db: Session, user: User, text: str = "") -> dict:
    """回答"你记得我什么"，要报具体值，而不是只报条数（评审 P-014）。"""
    profile = db.get(Profile, user.id)
    rows = list_memories(db, user)
    long_term = [memory_payload(row) for row in rows if row.kind != SHORT_TERM]
    short_term = [memory_payload(row) for row in rows if row.kind == SHORT_TERM]

    facts: list[str] = []
    if profile is not None:
        if profile.weight_kg:
            facts.append(f"体重 {profile.weight_kg:g} kg")
        if profile.height_cm:
            facts.append(f"身高 {profile.height_cm:g} cm")
        facts.append(f"目标 {_goal_label(profile.goal)}")
        if profile.weekly_days:
            facts.append(f"每周 {profile.weekly_days} 天")
        if profile.session_minutes:
            facts.append(f"每次 {profile.session_minutes} 分钟")
        if profile.preferred_time_window:
            facts.append(f"训练时间 {profile.preferred_time_window}")
        if profile.equipment:
            labels = {"dumbbell": "哑铃", "band": "弹力带", "bench": "训练凳",
                      "treadmill": "跑步机", "mat": "瑜伽垫", "barbell": "杠铃"}
            facts.append("器械 " + "、".join(labels.get(item, item) for item in profile.equipment))
        if profile.goal_note and "公斤" in profile.goal_note:
            facts.append(f"目标备注：{profile.goal_note[:40]}")

    reply = "你档案里记着：" + "；".join(facts) + "。"
    if long_term:
        values = [str(item.get("value") or "") for item in long_term[:5] if item.get("value")]
        if values:
            reply += "长期记忆里还有：" + "；".join(values) + "。"
    if short_term:
        values = []
        for item in short_term[:3]:
            value = str(item.get("value") or "")
            if item.get("category") == "training_state" and value.isdigit():
                value = "最近训练完成度正常" if value == "0" else f"连续 {value} 次未完成"
            if value:
                values.append(value)
        if values:
            reply += "近期状态：" + "；".join(values) + "。"
    if not long_term and not short_term:
        reply += "目前有 0 条长期偏好和 0 条近期状态。"
    reply += "体重、身高来自档案，不在长期记忆里；这些都会直接影响计划和热量。"
    return {
        "reply": reply,
        "memories": [memory_payload(row) for row in rows],
        "actions": [{"type": "open_memory", "label": "管理我的健身记忆"}],
    }


def _handle_edit_memory(db: Session, user: User, text: str, trace: list[dict]) -> dict:
    hits = find_memories(db, user, text)
    if not hits:
        return {
            "reply": "我找到不到要删除的那条记忆。可以说得更具体一点，比如「忘掉午休训练那条」。",
            "actions": [{"type": "open_memory", "label": "打开记忆列表"}],
        }
    removed = [memory_payload(archive_memory(db, user, row.id)) for row in hits]
    _push(trace, "memory", "deleted", count=len(removed))
    return {
        "reply": "已经删掉 " + "、".join(item["value"] for item in removed) + "。之后的计划不会再参考它。",
        "changes": [f"删除记忆：{item['value']}" for item in removed],
        "memories": [memory_payload(row) for row in list_memories(db, user)],
    }


INJURY_LABELS = {
    "knee": "膝盖", "lower_back": "腰", "shoulder": "肩", "wrist": "手腕",
    "ankle": "脚踝", "elbow": "肘", "neck": "颈", "hip": "髋",
}


def _handle_injury(db: Session, user: User, text: str, decision: Decision,
                   trace: list[dict]) -> dict:
    """伤病/不适：先安全，再给可执行的替代；不诊断、不承诺。"""
    regions = [str(item) for item in (decision.constraints.get("injured_regions") or [])]
    if not regions:
        regions = _injury_regions(text)
    labels = [INJURY_LABELS.get(item, item) for item in regions] or ["身体"]
    where = "、".join(labels)

    # 记为近期状态：30 天后自动归档；计划生成时会用它过滤风险动作。
    if regions:
        upsert_memory(
            db, user, kind=SHORT_TERM, category="injury",
            key=f"{regions[0]}_discomfort",
            value=f"{where}不适（{user_today(db.get(Profile, user.id))}）",
            structured={"regions": regions},
            source="chat", evidence=text, reason="用户报告不适，先按近期状态处理",
        )
        set_temporary(
            db, user, user_today(db.get(Profile, user.id)), "injury",
            {"injured_regions": regions}, text=text,
        )
    _push(trace, "safety", "injury", regions=regions)

    # 按不适部位匹配需要避开的动作；不再用一段“跳跃/深蹲/弓步/腿举”写死所有部位。
    avoid_map = {
        "knee": (["跳跃", "深蹲", "弓步", "腿举"], ["臀桥", "上肢", "核心", "拉伸"], "今天只练上肢"),
        "lower_back": (["硬拉", "深蹲", "弯腰划船", "跑步"], ["臀桥", "胸推", "核心", "拉伸"], "今天练上肢和核心"),
        "shoulder": (["推举", "卧推", "引体向上", "侧平举"], ["下肢", "核心", "拉伸"], "今天只练下肢"),
        "wrist": (["俯卧撑", "弯举", "划船", "推举"], ["下肢", "核心", "拉伸"], "今天只练下肢"),
        "ankle": (["跳跃", "跑步", "弓步"], ["上肢", "核心", "拉伸"], "今天只练上肢"),
        "neck": (["推举", "耸肩", "仰卧起坐"], ["下肢", "核心", "拉伸"], "今天只练下肢"),
        "hip": (["深蹲", "弓步", "硬拉"], ["上肢", "核心", "拉伸"], "今天只练上肢"),
    }
    avoid: list[str] = []
    safe: list[str] = []
    cta = "今天只练上肢"
    for region in regions:
        item = avoid_map.get(region)
        if not item:
            continue
        for word in item[0]:
            if word not in avoid:
                avoid.append(word)
        for word in item[1]:
            if word not in safe:
                safe.append(word)
        if region in ("shoulder", "wrist", "neck"):
            cta = "今天只练下肢"
    avoid_text = "、".join(avoid) if avoid else "当前会明显加重不适的动作"
    safe_text = "、".join(safe[:4]) if safe else "休息、散步和轻度拉伸"
    reply = (
        f"先别按原计划硬练。{where}不舒服时，今天先避开{avoid_text}，"
        f"改成低冲击版：{safe_text}。"
        "\n先确认：是单侧还是双侧？有没有肿、发热、卡住或明显活动受限？"
        "如果出现这些情况，先做医疗评估，不要靠训练硬扛。"
        "\n我可以把计划里相关动作换成低冲击替代；确认后才会改版本。"
        "以上仅为运动建议，不构成医疗诊断。"
    )
    return {
        "reply": reply,
        "warnings": ["已按近期状态记录不适；涉及疼痛加重时请先就医。"],
        "actions": [
            {"type": "quick_reply", "label": "把计划改成低冲击版",
             "text": f"{where}不舒服，帮我把计划里的{where}相关动作换成低冲击版"},
            {"type": "quick_reply", "label": cta,
             "text": "把今天会加重不适的动作换掉，只做安全的部位"},
        ],
        "changes": [f"记录近期不适：{where}"] if regions else [],
        "memories": [memory_payload(row) for row in list_memories(db, user)],
    }


DISH_ADVICE = {
    "黄焖鸡": ("黄焖鸡米饭通常 800–1000 kcal、油盐偏高。晚餐建议：蛋白 30–40g"
              "（鸡胸/虾/豆腐/鸡蛋），主食减半，加两份蔬菜；少喝汤、去皮。"),
    "麻辣烫": ("麻辣烫的坑在汤底和丸子。选清汤/番茄，少丸子和油炸，多点绿叶菜和豆腐，"
              "主食选粉丝/土豆当一份，别再加麻酱。"),
    "汉堡": ("汉堡不是不能吃：选单层肉饼、不加芝士/酱，配无糖饮料；晚餐补蛋白和蔬菜，"
             "主食减半。"),
    "炸鸡": ("炸鸡脂肪偏高：去皮、控制在 2–3 块，配蔬菜；当天不再加主食和含糖饮料。"),
    "奶茶": ("奶茶按“少糖/去奶盖/不加料”点，一杯约 200–350 kcal；当天主食减一份。"),
    "面条": ("面条容易蛋白不足：加一份鸡蛋/牛肉/豆腐，面量减 1/3，配一份青菜。"),
    "米饭": ("米饭本身没问题，先保证蛋白和蔬菜，再按饥饿感决定半份还是一份。"),
}


def _meal_summary(db: Session, user: User, day: dt.date) -> tuple[list[str], float, float]:
    payload = effective_day(db, user, day)
    meals = payload.get("meals") or []
    lines = []
    total = protein = 0.0
    for meal in meals:
        kcal = float(meal.get("kcal") or 0)
        protein_g = float(meal.get("protein_g") or 0)
        total += kcal
        protein += protein_g
        names = "、".join(
            f"{item.get('name')}{item.get('grams')}g" for item in (meal.get("items") or [])[:3]
        )
        lines.append(f"{meal.get('title') or meal.get('slot')} {int(kcal)} kcal（{names}）")
    return lines, total, protein


NUTRITION_OPERATIONS = {
    "get_meal_plan", "review_meal_plan", "modify_meal_plan",
    "replace_meal_item", "remove_meal_item", "record_meal",
}

# 仅用于"替换食物"时估算宏量；真实营养仍以餐单数据为准，不假装精确。
FOOD_CATEGORY = {
    "protein": ("鸡胸", "牛肉", "虾", "三文鱼", "鸡蛋", "豆腐", "酸奶", "牛奶", "鱼", "肉"),
    "staple": ("米饭", "红薯", "燕麦", "面条", "面包", "土豆", "玉米", "粥", "馒头"),
    "vegetable": ("西兰花", "菠菜", "青菜", "蔬菜", "生菜", "番茄", "黄瓜"),
    "fruit": ("香蕉", "苹果", "橙", "莓", "水果"),
}


def _food_category(name: str) -> str:
    raw = str(name or "")
    for category, words in FOOD_CATEGORY.items():
        if any(word in raw for word in words):
            return category
    return ""


from app.domain.nutrition_catalog import FOOD_MACROS_PER_100G, ingredient_key


def _food_name(item: dict) -> str:
    return str(item.get("name") or "").strip()


def _food_macros(name: str, grams: float) -> tuple[float, float, float, float] | None:
    key = ingredient_key(name)
    base = FOOD_MACROS_PER_100G.get(key) if key else None
    if base is None:
        return None
    kcal, protein, fat, carb = base
    return (kcal * grams / 100, protein * grams / 100,
            fat * grams / 100, carb * grams / 100)


def _recalc_meal(meal: dict) -> dict:
    from app.domain.day_plan import ensure_object_ids
    items = ensure_object_ids(list(meal.get("items") or []))
    meal["items"] = items
    meal["kcal"] = round(sum(float(item.get("kcal") or 0) for item in items), 1)
    meal["protein_g"] = round(sum(float(item.get("protein") or 0) for item in items), 1)
    meal["fat_g"] = round(sum(float(item.get("fat") or 0) for item in items), 1)
    meal["carb_g"] = round(sum(float(item.get("carb") or 0) for item in items), 1)
    return meal


def _meal_snapshot(day_payload: dict) -> list[dict]:
    return [
        {
            "id": meal.get("id"), "slot": meal.get("slot"), "title": meal.get("title"),
            "items": [dict(item) for item in (meal.get("items") or [])],
            "kcal": meal.get("kcal"), "protein_g": meal.get("protein_g"),
            "fat_g": meal.get("fat_g"), "carb_g": meal.get("carb_g"),
        }
        for meal in (day_payload.get("meals") or [])
    ]


def _handle_nutrition_operation(
    db: Session, user: User, day: dt.date, text: str, decision: Decision,
    trace: list[dict], client=None,
) -> dict:
    """饮食领域的可执行操作：查看/分析/改餐单/替换/移除/记录。"""
    operation = decision.operation or "answer_nutrition"
    current = effective_day(db, user, day, fallback_next=False)
    meals = _meal_snapshot(current)
    slots = decision.constraints or {}
    avoid = [str(item).strip() for item in (slots.get("avoid_foods") or []) if str(item).strip()]
    source = str(slots.get("from_food") or (slots.get("food") if operation == "remove_meal_item" else "") or "").strip()
    target = str(slots.get("to_food") or slots.get("replacement_food") or "").strip()
    meal_slot = str(slots.get("meal_slot") or "all")
    if meal_slot == "all":
        for word, slot in (("早餐", "breakfast"), ("午餐", "lunch"),
                           ("晚餐", "dinner"), ("加餐", "snack")):
            if word in text:
                meal_slot = slot
                break
    if source and source not in avoid:
        avoid.append(source)

    if operation in ("get_meal_plan", "review_meal_plan"):
        if operation == "get_meal_plan":
            lines = [f"{meal.get('title')} {round(float(meal.get('kcal') or 0))} kcal"
                     for meal in meals]
            return {"reply": "今天餐单：" + "；".join(lines) + "。",
                    "task_status": "success", "changes": [], "meals": meals}
        return _handle_nutrition(db, user, day, text, decision, trace, client)

    if operation == "record_meal":
        foods = [str(item) for item in (slots.get("foods") or []) if str(item).strip()]
        title = "、".join(foods) or text[:40] or "记录一餐"
        row = MealLog(
            id=new_id(), user_id=user.id, date=day, slot=str(slots.get("meal_slot") or "snack"),
            title=title[:80], kcal=int(slots.get("kcal") or 0),
            protein_g=float(slots.get("protein_g") or 0),
            note=str(slots.get("note") or "")[:200], source="agent",
        )
        db.add(row)
        db.flush()
        return {"reply": f"已记录：{title}，{row.kcal} kcal。", "task_status": "success",
                "changes": [f"记录饮食：{title}"]}

    if operation not in ("modify_meal_plan", "replace_meal_item", "remove_meal_item"):
        return _handle_nutrition(db, user, day, text, decision, trace, client)

    changed: list[str] = []
    if not avoid and not target:
        if meal_slot != "all":
            label = {"breakfast": "早餐", "lunch": "午餐",
                     "dinner": "晚餐", "snack": "加餐"}.get(meal_slot, meal_slot)
            return {"reply": f"{label}要改成什么？可以说“{label}换成牛肉”。",
                    "task_status": "clarification_required", "changes": []}
        return {"reply": "你想改哪一餐/哪种食物？可以说“午餐鸡胸肉换成牛肉”或“今天不吃鸡胸肉”。",
                "task_status": "clarification_required", "changes": []}
    selected = [meal for meal in meals
                if meal_slot == "all" or meal.get("slot") == meal_slot]
    if not selected:
        return {"reply": f"今天的餐单里没有「{meal_slot}」，所以没有改动。",
                "task_status": "clarification_required", "changes": []}

    for meal in selected:
        items = list(meal.get("items") or [])
        main_item = None
        if target and not avoid:
            target_category = _food_category(target)
            same_category = [
                item for item in items
                if _food_category(_food_name(item)) == target_category
            ] if target_category else []
            if same_category:
                # 用户说“想吃米饭”时应替换主食（红薯/米饭），不是蛋白（牛肉）。
                main_item = max(
                    same_category, key=lambda item: float(item.get("grams") or 0),
                )
            elif target_category in ("staple", "protein", "vegetable", "fruit"):
                grams = 150.0
                macros = _food_macros(target, grams) or (0, 0, 0, 0)
                kcal, protein, fat, carb = macros
                new_item = {
                    "name": target, "grams": grams, "kcal": round(kcal, 1),
                    "protein": round(protein, 1), "fat": round(fat, 1),
                    "carb": round(carb, 1),
                }
                items.append(new_item)
                meal["items"] = items
                _recalc_meal(meal)
                changed.append(f"{meal.get('title')}：新增{target}")
                continue
            else:
                main_item = max(
                    items, key=lambda item: float(item.get("protein") or 0), default=None,
                )
        kept = []
        for item in items:
            name = _food_name(item)
            should_change = any(word and word in name for word in avoid) or item is main_item
            if not should_change or (target and name == target):
                kept.append(item)
                continue
            grams = float(item.get("grams") or 100)
            if target:
                macros = _food_macros(target, grams)
                if macros is None:
                    kept.append({**item, "name": target})
                    changed.append(f"{meal.get('title')}：{name} → {target}")
                    continue
                kcal, protein, fat, carb = macros
                kept.append({**item, "name": target, "kcal": round(kcal, 1),
                             "protein": round(protein, 1), "fat": round(fat, 1),
                             "carb": round(carb, 1)})
                changed.append(f"{meal.get('title')}：{name} → {target}")
            else:
                changed.append(f"{meal.get('title')}：移除{name}")
        meal["items"] = kept
        _recalc_meal(meal)

    if not changed:
        if target and operation in ("replace_meal_item", "modify_meal_plan"):
            return {
                "reply": f"这餐已经是「{target}」了，你想换成别的什么？",
                "task_status": "clarification_required", "changes": [],
            }
        if source and operation == "replace_meal_item":
            return {
                "reply": (
                    f"今天的{meal_slot if meal_slot != 'all' else '餐单'}里没有找到「{source}」，"
                    "所以没有替换；你想换成哪一项？"
                ),
                "task_status": "clarification_required", "changes": [],
            }
        if meal_slot == "all":
            set_temporary(db, user, day, "nutrition", {"avoid_foods": avoid}, text=text)
        return {
            "reply": (
                f"今天先避开「{'、'.join(avoid)}」；今天餐单里目前没有它，"
                "本次目标餐次已满足忌口要求。"
            ),
            "task_status": "success",
            "changes": [f"记录今日忌口：{'、'.join(avoid)}"],
        }
    summary = "今天饮食：" + "；".join(changed)
    if meal_slot == "all":
        set_temporary(db, user, day, "nutrition", {"avoid_foods": avoid}, text=text)
    override = save_override(
        db, user, day, actions=current.get("actions") or [], meals=meals,
        summary=summary,
        constraints={**(current.get("constraints") or {}),
                     **({"avoid_foods": avoid} if meal_slot == "all" else
                        {"meal_constraints": {**(current.get("constraints", {}).get("meal_constraints") or {}),
                                              meal_slot: {"avoid_foods": avoid, **({"replace_with": target} if target else {})}}}),
                     **({"replace_with": target} if target and meal_slot == "all" else {})},
        reason=text, base_version=current.get("plan_version"), expected_adjustment_id=current.get("adjustment_id"),
        borrowed_from=current.get("borrowed_from"), changes=changed,
    )
    _push(trace, "nutrition", "modified", changes=len(changed),
          avoid=avoid, replacement=target)
    # 后置条件：改完后餐单里不能再出现 avoid_foods。
    remaining = [
        _food_name(item) for meal in selected for item in (meal.get("items") or [])
        if any(word and word in _food_name(item) for word in avoid)
    ]
    status = "success" if not remaining else "failed"
    return {"reply": summary + "。已按今天的餐单保存，长期计划不变。",
            "task_status": status, "changes": changed, "meals": meals,
            "day": effective_day(db, user, day, fallback_next=False),
            "warnings": ["替换食物的宏量按常见食材估算，不是精确营养计算。"] if target else [],
            "override": override}


def _handle_nutrition(    db: Session, user: User, day: dt.date, text: str, decision: Decision,
    trace: list[dict], client=None,
) -> dict:
    """饮食建议：优先用模型结合当天计划回答；模型不可用时用确定性数据兜底。"""
    profile = db.get(Profile, user.id)
    plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    nutrition = (plan.nutrition if plan is not None else {}) or {}
    target_kcal = float(nutrition.get("target_kcal") or 0)
    protein_band = (nutrition.get("macros") or {}).get("protein_g") or {}
    target_protein = float(protein_band.get("low_g") or 0)
    meal_lines, meal_kcal, meal_protein = _meal_summary(db, user, day)
    logs = db.scalars(
        select(MealLog)
        .where(MealLog.user_id == user.id, MealLog.date == day)
        .order_by(MealLog.created_at.asc())
    ).all()
    logged_lines = [
        f"{row.title} {row.kcal} kcal（蛋白 {row.protein_g:g}g）" for row in logs
    ]
    logged_kcal = sum(int(row.kcal or 0) for row in logs)
    logged_protein = sum(float(row.protein_g or 0) for row in logs)
    remaining = max(target_kcal - logged_kcal, 0) if target_kcal else 0
    dish = next((name for name in DISH_ADVICE if name in text), "")
    avoid = [str(item).strip() for item in (decision.constraints.get("avoid_foods") or [])
             if str(item).strip()]
    food_context = load_memory_context(db, user)
    liked_foods = list(food_context.get("food_likes") or [])
    for food in list(food_context.get("food_dislikes") or []) + list(
            food_context.get("food_allergies") or []):
        food = str(food).strip()
        if food and food not in avoid:
            avoid.append(food)
    temporary = load_temporary(db, user, day)
    for item in (temporary.get("nutrition") or {}).get("avoid_foods") or []:
        if str(item).strip() and str(item).strip() not in avoid:
            avoid.append(str(item).strip())

    # 模型可用：把"数字 + 今天吃了什么 + 限制"交给它，要求短、可执行、不说教。
    if client is not None and getattr(client, "available", lambda: False)():
        context = {
            "用户": {
                "体重_kg": getattr(profile, "weight_kg", None),
                "目标": _goal_label(getattr(profile, "goal", "")),
                "过敏": getattr(profile, "allergies", None) or [],
                "忌口": getattr(profile, "diet_preferences", None) or [],
                "常点外卖": True,
            },
            "今日目标": {"热量_kcal": target_kcal, "蛋白_g": target_protein},
            "今日计划餐": {
                "餐食": meal_lines,
                "合计_kcal": round(meal_kcal),
                "蛋白_g": round(meal_protein, 1),
            },
            "今日已记录": {
                "餐食": logged_lines,
                "合计_kcal": logged_kcal,
                "蛋白_g": round(logged_protein, 1),
                "剩余_kcal": round(remaining),
            },
            "今日饮食约束": {"不想吃 / 忌口": avoid, "喜欢": liked_foods},
            "用户问题": text,
        }
        prompt = (
            "你是健身教练的饮食助手。基于下面的事实回答，中文，4 句以内，给可执行动作。\n"
            "硬性规则：不诊断疾病；不建议低于 1200 kcal/天或极端断食；不推荐药物/补剂；"
            "不编造精确热量，范围就说范围；优先外卖可执行的调整；"
            "如果用户提到过敏原、不想吃、不喜欢或忌口，必须避开，不能出现在推荐里。\n"
            f"事实：{json.dumps(context, ensure_ascii=False)}"
        )
        try:
            reply = client.complete(
                [{"role": "user", "content": prompt}],
                tools=None, temperature=0.2, max_tokens=420,
            )
            content = str((reply.get("message") or {}).get("content") or "").strip()
            if content:
                _push(trace, "nutrition", "model", chars=len(content))
                return {
                    "reply": content,
                    "warnings": ["饮食建议仅作参考，不构成医疗或营养处方。"],
                    "actions": [
                        {"type": "quick_reply", "label": "按这个调整今天的餐",
                         "text": "帮我按这个思路调整今天的饮食安排"},
                    ],
                }
        except Exception as exc:  # noqa: BLE001 - 模型失败必须还能回答
            _push(trace, "nutrition", "fallback", error=type(exc).__name__)

    # 确定性兜底：数据来自当前计划和档案，不是编的。
    parts: list[str] = []
    if target_kcal:
        if logs:
            parts.append(
                f"你今天已记录 {int(logged_kcal)} / {int(target_kcal)} kcal，"
                f"蛋白 {round(logged_protein)} / {round(target_protein)} g"
                + (f"，还剩 {int(remaining)} kcal 可以安排。" if remaining > 80 else "。")
            )
        else:
            parts.append(
                f"今天计划餐 {int(meal_kcal)} kcal，尚未记录；"
                f"目标 {int(target_kcal)} kcal、蛋白 {round(target_protein)} g。"
            )
    if avoid:
        alternatives = [item for item in (liked_foods + ["牛肉", "虾", "豆腐", "鸡蛋", "三文鱼"])
                        if not any(word in item for word in avoid)]
        parts.append(
            f"你提到不想吃「{'、'.join(avoid)}」，今天先避开它；"
            f"蛋白可以选{'、'.join(alternatives[:3]) or '其他瘦肉/豆制品'}，"
            "蔬菜和主食按原计划。"
        )
    elif dish:
        parts.append(DISH_ADVICE[dish])
    else:
        parts.append(
            "外卖按“蛋白先行”点：一份鸡胸/牛肉/虾/豆腐，两份蔬菜，主食半份到一份；"
            "酱汁分装、少喝汤。"
        )
    parts.append("不用每天称重，按 2–4 周体重趋势和腰围调整；饿到影响工作就加一份蛋白或蔬菜。")
    parts.append("以上仅为运动营养参考，不构成医疗建议。")
    _push(trace, "nutrition", "fallback")
    return {
        "reply": "\n".join(parts),
        "warnings": ["饮食建议仅作参考；如有肾病/代谢疾病/进食障碍请咨询专业人员。"],
        "actions": [],
    }


def _handle_plan_overview(db: Session, user: User, trace: list[dict]) -> dict:
    """把当前长期版本整理成"每天做什么"的只读安排。"""
    plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    if plan is None:
        return {"reply": "你还没有生效的计划。先告诉我目标和条件，我生成一份给你确认。"}
    version = db.scalar(select(PlanVersion).where(
        PlanVersion.plan_id == plan.id, PlanVersion.version == plan.current_version))
    if version is None:
        return {"reply": "当前计划版本读不到，先去计划页刷新一下。"}
    actions = db.scalars(
        select(PlanAction)
        .where(PlanAction.plan_version_id == version.id)
        .order_by(PlanAction.day_index, PlanAction.slot, PlanAction.seq)
    ).all()
    weekday_cn = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    by_day: dict[int, list[PlanAction]] = {}
    for action in actions:
        if action.action_type == "exercise":
            by_day.setdefault(action.day_index, []).append(action)
    if not by_day:
        return {"reply": "当前版本里还没有训练动作，可以让我重新生成一份。"}
    lines = []
    for day_index in sorted(by_day):
        items = by_day[day_index]
        names = "、".join(
            f"{((item.meta or {}).get('name_zh') or item.action_ref)}"
            f"{' ' + str(item.sets) + '组' if item.sets else ''}"
            for item in items[:4]
        )
        label = weekday_cn[day_index - 1] if 1 <= day_index <= 7 else f"第{day_index}天"
        lines.append(f"{label}：{names}")
    nutrition = plan.nutrition or {}
    kcal = nutrition.get("target_kcal") or version.calorie_target
    reply = "这是当前 v%d 的每周安排：%s。每天约 %s；饮食目标 %s kcal。想改哪一天，直接说“周三换成 20 分钟”就行。" % (
        version.version, "；".join(lines),
        (plan.program or {}).get("session_minutes") or "按计划",
        int(kcal) if kcal else "按档案",
    )
    _push(trace, "plan", "overview", days=len(by_day))
    return {"reply": reply, "actions": []}


def _handle_create_plan(
    db: Session, user: User, text: str, decision: Decision, trace: list[dict], client=None,
) -> dict:
    profile = db.get(Profile, user.id)
    if profile is None:
        return {"reply": "先到「我的」页填一下性别、身高、体重，再生成计划。"}
    # 新账号不再预填 178/78 这类示例值；缺身高体重时先生成计划会用错 TDEE。
    if (float(profile.height_cm or 0) < 120 or float(profile.weight_kg or 0) < 30
            or int(profile.birth_year or 0) < 1940):
        return {
            "reply": "还差基础信息：身高、体重、出生年。补全后我才能算热量和训练量，先去「我的」页填一下。",
            "actions": [{"type": "open_profile", "label": "去补基础信息"}],
        }

    # 用户在这句话里明确给出的目标/天数/时长属于长期信息：
    # Profile 是唯一事实来源，先更新 Profile，再同步 canonical Memory。
    preference = apply_preference_candidates(
        db, user, decision.candidates, text, evidence=text,
    )
    if preference.needs_confirmation:
        return {
            "reply": preference.question,
            "preference_conflicts": preference.conflicts,
            "actions": [
                {"type": "quick_reply", "label": "这是长期改变",
                 "text": text + "，以后都按这个来"},
                {"type": "quick_reply", "label": "只改今天",
                 "text": text.replace("以后", "今天")},
            ],
        }
    # 偏好已经落库。先提交并释放写事务，再调用可能持续十几秒的计划生成；
    # 否则 SQLite 会在整个模型调用期间持有写锁，其他请求只能等到超时后 500。
    db.commit()
    memory = load_memory_context(db, user)
    instruction = text
    if memory.get("preferred_windows"):
        windows = "、".join(f"{item['label']}({item['window']})"
                            for item in memory["preferred_windows"])
        instruction += f"\n长期时间偏好：{windows}"
    _push(trace, "plan", "generating", source="skill")
    draft = coach_plans.create_draft(db, user, instruction, client=client)
    payload = coach_plans.draft_payload(draft)
    _push(trace, "plan", payload.get("status") or "ready", draft_id=draft.id)

    if payload.get("status") == "ready" and not payload.get("days"):
        # skill 不可用时确定性引擎可能给不出训练日：不能给一个"空计划"让用户确认
        return {
            "reply": "训练引擎这次没有返回可执行的训练日，现有计划没有被改动。可以稍后再试，或先检查基础信息。",
            "warnings": ["检测到计划草案没有训练日，已阻止确认。"],
            "plan_draft": payload,
            "actions": [],
        }
    if payload.get("status") == "ready":
        reply = payload.get("summary") or "计划已经生成，确认后生效。"
        guard_changes = payload.get("guard_changes") or []
        if guard_changes:
            reply += "已按你的器械/时长校正：" + "；".join(str(item) for item in guard_changes[:3]) + "。"
        reply += "（还没有改动现有计划，点确认才会生成新版本。）"
        actions = [
            {"type": "confirm_plan", "label": "确认并启用这份计划",
             "draft_id": draft.id},
            {"type": "reject_plan", "label": "先不要", "draft_id": draft.id},
        ]
    else:
        reply = payload.get("question") or "这次没能生成计划，需要补充一句目标或身体情况。"
        actions = [{"type": "quick_reply", "label": "补充信息", "text": "我的目标是减脂，每周 3 次，每次 30 分钟"}]
    return {
        "reply": reply,
        "plan_draft": payload,
        "actions": actions,
        "changes": [
            "已按目标 / 经验 / 时间 / 器械重新生成训练与食谱",
        ] + preference.changes,
        "memory_saved": [memory_payload(row) for row in preference.saved],
        "memories": [memory_payload(row) for row in list_memories(db, user)],
    }


def _handle_update_preference(
    db: Session, user: User, text: str, decision: Decision, trace: list[dict]
) -> dict:
    if not decision.candidates:
        return {"reply": "我没听出要记住什么。可以说「以后训练安排在午休」「我喜欢吃牛肉」。",
                "task_status": "clarification_required"}
    profile = db.get(Profile, user.id)
    if decision.needs_confirmation:
        proposed = []
        for item in decision.candidates:
            category = item.get("category")
            if category in ("like", "dislike"):
                proposed.append(str(item.get("value") or ""))
            else:
                proposed.append(str(item.get("value") or item.get("structured") or ""))
        detail = "；".join([item for item in proposed if item]) or "更新长期档案"
        return {
            "reply": f"我理解你想改：{detail}。确认后我再写入档案和记忆。",
            "task_status": "confirmation_required",
            "actions": [{
                "type": "quick_reply",
                "label": "确认更新档案",
                "text": text + "；确认更新档案",
            }],
        }
    conflicts = detect_conflicts(profile, decision.candidates, text) if profile is not None else []
    preference = apply_preference_candidates(
        db, user, decision.candidates, text, evidence=text,
    )
    if preference.needs_confirmation:
        _push(trace, "memory", "needs_confirmation", conflicts=preference.conflicts)
        return {
            "reply": preference.question,
            "task_status": "confirmation_required",
            "preference_conflicts": conflicts,
            "actions": [
                {"type": "quick_reply", "label": "以后都按新的来",
                 "text": text + "，以后都按这个来"},
                {"type": "quick_reply", "label": "只改今天",
                 "text": text.replace("以后", "今天")},
            ],
        }
    saved = preference.saved
    _push(trace, "memory", "saved", count=len(saved))
    changes = preference.changes or [f"记住：{row.value}" for row in saved]
    food_only = bool(decision.candidates) and all(
        item.get("category") in ("food_like", "food_dislike", "food_allergy")
        for item in decision.candidates
    )
    if food_only:
        reply = "已记住：" + "；".join(
            str(row.value or "").replace("喜欢吃", "").replace("不喜欢吃", "不喜欢")
            for row in saved
        ) + "。"
    else:
        reply = "已更新档案和记忆：" + "；".join(changes) + "。"
    reply += "之后的计划会按新偏好生成；今天的执行安排不会被这句话自动改掉。"
    actions = [
        {"type": "quick_reply", "label": "按这个重排计划", "text": "按我的长期偏好重新生成计划"},
        {"type": "open_memory", "label": "查看 / 编辑记忆"},
    ]
    return {
        "reply": reply,
        "task_status": "success" if saved or preference.changes else "failed",
        "changes": changes,
        "memory_saved": [memory_payload(row) for row in saved],
        "memories": [memory_payload(row) for row in list_memories(db, user)],
        "actions": actions,
    }


def _handle_adjust_plan(
    db: Session, user: User, text: str, decision: Decision, trace: list[dict],
    client=None,
) -> dict:
    """计划结构变更：复用原有补丁链路（草案 → 安全门 → 确认 → 新版本）。"""
    plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    if plan is None:
        return _handle_create_plan(db, user, text, decision, trace)
    try:
        req = patch_requests.create_request(
            db, user, plan.id, text, quick_action="", client=client,
        )
    except Exception as exc:  # noqa: BLE001 - 补丁失败要明确告诉用户
        return {
            "reply": f"这个改法没通过：{str(exc)[:160]}。原计划没有变化。",
            "warnings": ["计划级改动必须通过安全门；被拦下时不会写入任何版本。"],
        }
    payload = patch_requests.request_payload(db, req)
    _push(trace, "plan", req.status, request_id=req.id)
    if req.status == "ready" and payload.get("draft"):
        draft = payload["draft"]
        changes = [
            f"{item.get('before_name') or item.get('before_action_ref')} → "
            f"{item.get('after_name') or '删除'}"
            for item in (draft.get("operations") or [])
        ]
        return {
            "reply": (
                f"我准备这样改 {len(draft.get('operations') or [])} 处："
                + ("；".join(changes[:3]) if changes else "调整计划结构")
                + "。确认后才会生成新版本；现在计划还没变。"
            ),
            "patch_request": payload,
            "changes": changes,
            "actions": [
                {"type": "approve_patch", "label": "确认应用这个改动",
                 "request_id": req.id},
                {"type": "reject_patch", "label": "先不改", "request_id": req.id},
            ],
        }
    body_focus = next(
        (label for label, words in (
            ("背", ("练背", "背部")),
            ("胸", ("练胸", "胸部")),
            ("腿", ("练腿", "腿部")),
            ("肩", ("练肩", "肩部")),
            ("手臂", ("练手臂", "手臂")),
            ("臀", ("练臀", "臀部")),
        ) if any(word in text for word in words)),
        "",
    )
    if body_focus and "没太理解" in str(req.question or ""):
        reply = (
            f"下周想重点练{body_focus}可以。你现在是每周固定训练，"
            f"想让我把其中一天换成{body_focus}，还是额外加一天{body_focus}？"
            "告诉我星期几或“加一天”，我再生成计划补丁，确认后才会生效。"
        )
    else:
        reply = req.question or "这个调整还需要一点信息。"
    return {
        "reply": reply,
        "patch_request": payload,
        "actions": [],
    }


def _stop_session_for_rest(
    db: Session, user: User, session: WorkoutSession, reason: str,
) -> dict | None:
    """把还没结束的训练按"已完成部分"收尾，避免休息日仍挂着进行中的会话。"""
    if session is None or session.status == "finished":
        return None
    progress = workout_service.session_payload(session).get("progress") or {}
    percent = int(progress.get("percent") or 0)
    return workout_service.finish_session(
        db, user, session.id,
        completed=False,
        completion_pct=percent,
        difficulty=2,
        fatigue=4,
        note=(reason or "身体不适，提前结束")[:200],
    )


def _save_rest_override(
    db: Session, user: User, day: dt.date, text: str,
) -> tuple[dict, object]:
    """把今天改成恢复日（拉伸），返回 (day_payload, override)。"""
    current = effective_day(db, user, day, fallback_next=False)
    adjusted = apply_constraints(
        db, user, current.get("actions") or [], {"rest": True, "minutes": 15}, day=day,
    )
    summary = "今天改成恢复日：训练换成 15 分钟拉伸，饮食结构不变"
    override = save_override(
        db, user, day,
        actions=adjusted.actions,
        meals=current.get("meals") or [],
        summary=summary,
        constraints={"rest": True},
        reason=text,
        base_version=current.get("plan_version"),
        borrowed_from=current.get("borrowed_from"),
        changes=adjusted.changes,
    )
    return effective_day(db, user, day, fallback_next=False), override


def _replace_today_exercise(
    db: Session, user: User, day: dt.date, text: str, decision: Decision,
    current: dict,
) -> dict | None:
    slots = {
        "target_index": (decision.context_reference or {}).get("referenced_index"),
        "target_action": (decision.constraints.get("exercise")
                          or (decision.constraints.get("avoid_actions") or [None])[0]),
    }
    if not slots["target_index"] and not slots["target_action"]:
        return None
    task = {
        "task_id": "local_replace", "intent": "adjust_today",
        "operation": "replace_action", "scope": "today",
        "slots": slots, "action_type": "replace",
    }
    result = _apply_today_operations(db, user, day, text, [task], [])
    result.pop("task_results", None)
    return result


def _handle_adjust_today(    db: Session, user: User, day: dt.date, text: str, decision: Decision,
    trace: list[dict], active: WorkoutSession | None = None,
) -> dict:
    constraints = {key: value for key, value in decision.constraints.items()
                   if value not in (None, [], {}, False)}
    if not constraints:
        return {
            "reply": (
                "好，只改今天。你希望今天怎么调？时间变短、想在家练、"
                "器械被占用，或者身体状态不太好，直接说一句就行。"
            ),
            "actions": [
                {"type": "quick_reply", "label": "今天只有 20 分钟",
                 "text": "今天只有 20 分钟，不去健身房了"},
                {"type": "quick_reply", "label": "今天想休息",
                 "text": "今天想休息，把训练换成拉伸"},
            ],
        }
    if constraints.get("rest"):
        # 用户说"今天不练了"：先把进行中的训练按已完成部分收尾，再把今天改成恢复日。
        finished = None
        if active is not None and active.status in ("active", "paused", "completed"):
            finished = _stop_session_for_rest(db, user, active, text)
        day_payload, override = _save_rest_override(db, user, day, text)
        _push(trace, "day", "rest", session_id=active.id if active else "")
        return {
            "reply": (
                "收到。今天不练了，我把今天改成恢复日：15 分钟拉伸，饮食结构不变。"
                + ("刚才已经完成的动作和组数已经记下来了，不会清空。" if finished else "")
                + "明天会回到原来的计划。"
            ),
            "changes": ["今天改成恢复日：训练换成拉伸，饮食结构不变"]
                       + (["已结束进行中的训练，完成部分已记录"] if finished else []),
            "day": day_payload,
            "session": (finished or {}).get("session"),
            "actions": [{"type": "reset_day", "label": "恢复原计划", "date": day.isoformat()}],
            "warnings": ["如果出现胸痛、晕厥、明显气短或进行性麻木，请立即就医，不要只休息。"],
            "override": override,
        }

    current = effective_day(db, user, day, fallback_next=True)
    if not current.get("actions"):
        return {
            "reply": "现在还没有可执行的训练计划。先说一句目标（比如「减脂，每周 3 次，每次 30 分钟」），我先给你排一份。",
            "actions": [{"type": "quick_reply", "label": "建立计划",
                         "text": "我想减脂，每周训练三次，每次 30 分钟"}],
        }
    if decision.constraints.get("exercise") or decision.constraints.get("avoid_actions"):
        replaced = _replace_today_exercise(db, user, day, text, decision, current)
        if replaced is not None:
            return replaced

    adjusted = apply_constraints(db, user, current["actions"], constraints, day=day)
    if not adjusted.actions:
        return {
            "reply": "按今天的条件没有剩下可做的动作，先休息也合理；要不要把下一次训练提前？",
            "actions": [{"type": "quick_reply", "label": "今天休息", "text": "今天想休息"}],
        }
    # 「今天晚上有空」只记录当天时间窗口，不构成计划内容变更：
    # 不创建 PlanAdjustment，避免计划页出现"无实际改动的调整"。
    plan_change = set(constraints) - {"preferred_window"}
    if not plan_change:
        _store_temp_context(db, user, day, constraints, text)
        append_event(
            db, f"user:{user.id}", "day_adjusted",
            {"date": day.isoformat(), "constraints": constraints,
             "actions": len(current["actions"]), "changes": []},
            actor_type="agent", actor_id="coach",
        )
        window = constraints.get("preferred_window") or {}
        label = window.get("label") or "今天"
        day_payload = effective_day(db, user, day, fallback_next=True)
        session_payload = None
        active = workout_service.active_session(db, user, day)
        if active is not None:
            session_payload = workout_service.session_payload(active)
            actions = [{"type": "open_workout", "label": "继续训练", "session_id": active.id}]
        else:
            actions = [{"type": "start_workout", "label": "开始训练", "date": day.isoformat()}]
        return {
            "reply": f"收到，今天把训练安排在{label}（{window.get('window')}）；长期偏好不变。",
            "changes": [f"记录今天训练时间：{label}（{window.get('window')}）"],
            "day": day_payload,
            "session": session_payload,
            "actions": actions,
            "warnings": ["这是当天时间安排，不会写进长期训练时间偏好"],
        }

    summary = summarise_adjustment(adjusted.actions, constraints, adjusted.changes)
    override = save_override(
        db, user, day,
        actions=adjusted.actions,
        meals=current.get("meals") or [],
        summary=summary,
        constraints=constraints,
        reason=text,
        base_version=current.get("plan_version"),
        borrowed_from=current.get("borrowed_from"),
        changes=adjusted.changes,
    )
    _store_temp_context(db, user, day, constraints, text)
    append_event(
        db, f"user:{user.id}", "day_adjusted",
        {"date": day.isoformat(), "constraints": constraints,
         "actions": len(adjusted.actions), "changes": adjusted.changes[:6]},
        actor_type="agent", actor_id="coach",
    )
    _push(trace, "day", "adjusted", actions=len(adjusted.actions))
    day_payload = effective_day(db, user, day, fallback_next=True)
    session_payload = None
    active = workout_service.active_session(db, user, day)
    if active is not None:
        session_payload = workout_service.session_payload(active)
        actions = [{"type": "open_workout", "label": "继续训练", "session_id": active.id}]
    else:
        actions = [{"type": "start_workout", "label": "开始训练", "date": day.isoformat()}]
    actions.append({"type": "reset_day", "label": "恢复原计划", "date": day.isoformat()})
    notes = [
        "只改今天，不动长期计划和原始版本",
        "训练内容已按时间 / 地点 / 器械重新安排",
    ]
    if constraints.get("minutes"):
        notes.append("明天不会默认变成短训练")
    return {
        "reply": (
            "收到，" + summary + "。已完成的训练和长期目标都不受影响；"
            "明天会回到原来的计划。"
        ),
        "changes": adjusted.changes + [f"今日临时调整：{summary}"],
        "day": day_payload,
        "session": session_payload,
        "actions": actions,
        "warnings": notes,
        "override": override,
    }


def _handle_adjust_workout(
    db: Session, user: User, day: dt.date, text: str,
    decision: Decision, active: WorkoutSession | None, trace: list[dict],
) -> dict:
    session = active
    if session is None:
        try:
            session = workout_service.start_session(db, user, day, source="agent")
        except ValueError as exc:
            return {"reply": str(exc), "actions": []}
    if session.status in ("completed", "finished"):
        # 已完成但还没提交反馈：先走反馈，而不是重新生成
        return _handle_feedback(db, user, day, text, decision, session, trace)

    constraints = decision.constraints
    changes: list[str] = []
    payload = workout_service.session_payload(session)

    if constraints.get("rest"):
        # 身体不适/不想继续：直接收尾，不再"减一组继续练"。
        finished = _stop_session_for_rest(db, user, session, text)
        day_payload, override = _save_rest_override(db, user, day, text)
        _push(trace, "workout", "rest", session_id=session.id)
        return {
            "reply": (
                "收到，今天先不练了。已结束进行中的训练，完成的部分已经记下来；"
                "今天改成恢复日：15 分钟拉伸，饮食结构不变。"
            ),
            "changes": ["结束今天的训练，完成部分已记录", "今天改成恢复日：训练换成拉伸"],
            "session": (finished or {}).get("session"),
            "day": day_payload,
            "actions": [
                {"type": "reset_day", "label": "恢复原计划", "date": day.isoformat()},
            ],
            "warnings": ["如果出现胸痛、晕厥、明显气短或进行性麻木，请立即就医。"],
            "override": override,
        }

    if constraints.get("equipment_busy"):
        payload, changes = workout_service.replace_action(
            db, user, session.id,
            reason="器械被占用，换成不依赖器械的动作",
            equipment_busy=True,
        )
        workout_service.unavailable_equipment_context(db, user, day)
    elif constraints.get("injured_regions"):
        payload, changes = workout_service.adjust_session(
            db, user, session.id,
            injured_regions=constraints["injured_regions"],
            easier=bool(constraints.get("easier")),
            harder=bool(constraints.get("harder")),
            minutes_remaining=constraints.get("minutes"),
        )
    elif constraints.get("harder"):
        payload, changes = workout_service.adjust_session(
            db, user, session.id, harder=True,
        )
    elif constraints.get("easier") or constraints.get("minutes"):
        payload, changes = workout_service.adjust_session(
            db, user, session.id,
            easier=bool(constraints.get("easier")),
            minutes_remaining=constraints.get("minutes"),
        )
    elif constraints.get("exercise"):
        # 上下文指代（如"第一个换掉"）：模型已结合编号/当前动作解析出目标，
        # 但"换成什么"还需要用户给方向；先确认目标，不擅自替换。
        current = payload.get("current_item") or {}
        name = str(current.get("name") or constraints.get("exercise") or "当前动作")
        index = int(payload.get("current_index") or 0) + 1
        return {
            "reply": (
                f"你说的应该是第 {index} 个「{name}」。想换成什么？"
                "可以说具体动作，或者说换一个不伤膝的 / 练背的；我按你说的换。"
            ),
            "actions": [
                {"type": "quick_reply", "label": "换个不伤膝的",
                 "text": "换个不伤膝的动作"},
                {"type": "quick_reply", "label": "换个练背的",
                 "text": "换个练背的动作"},
                {"type": "quick_reply", "label": "先跳过",
                 "text": "跳过这个动作"},
            ],
            "session": payload,
            "day": effective_day(db, user, day, fallback_next=False),
        }
    elif "跳过" in text:
        payload = workout_service.skip_action(db, user, session.id, reason="用户跳过")
        changes = ["跳过当前动作，已完成的组数保留"]
    else:
        # 没有可执行条件时不要擅自减组：问清用户想休息、减量、换动作还是加量。
        return {
            "reply": (
                "你想怎么调？身体不舒服就直接说「今天不练了」；"
                "也可以说「减一组」「换个动作」「加一点强度」。"
            ),
            "actions": [
                {"type": "quick_reply", "label": "今天不练了", "text": "我不舒服，今天就别练了"},
                {"type": "quick_reply", "label": "减一组", "text": "今天有点累，减一组"},
                {"type": "quick_reply", "label": "加一点强度", "text": "我状态很好，可以加一点强度"},
            ],
            "session": payload,
            "day": effective_day(db, user, day, fallback_next=False),
        }

    day_payload = effective_day(db, user, day, fallback_next=False)
    append_event(
        db, f"user:{user.id}", "workout_adjusted",
        {"session_id": session.id, "changes": changes[:8],
         "intent": decision.intent},
        actor_type="agent", actor_id="coach",
    )
    _push(trace, "workout", "adjusted", changes=len(changes))
    return {
        "reply": (
            "已按当前训练进度调整：" + ("；".join(changes) if changes else "保留原任务")
            + "。前面已经完成的动作和组数都没有清空。"
        ),
        "changes": changes,
        "session": payload,
        "day": day_payload,
        "actions": [
            {"type": "open_workout", "label": "回到训练", "session_id": session.id},
        ],
    }


def _handle_guidance(
    db: Session, user: User, day: dt.date, text: str,
    decision: Decision, active: WorkoutSession | None, trace: list[dict],
) -> dict:
    session = active
    items = (session.items or []) if session is not None else []
    index = session.current_index if session is not None else None
    current = items[index] if session is not None and index is not None and index < len(items) else None
    current_name = str((current or {}).get("name") or "")
    current_ref = str((current or {}).get("action_ref") or "")

    # 用户明确说了要学哪个动作时，以用户说的为准；
    # 只有“这个动作怎么做”这类没点名的情况，才回退到当前训练动作。
    requested: tuple[str | None, str] | None = None
    model_exercise = str((decision.constraints or {}).get("exercise") or "").strip()
    if model_exercise:
        requested = _resolve_exercise(model_exercise)
        if requested is None:
            requested = (None, model_exercise[:40])
    if requested is None:
        requested = _resolve_exercise(text)
    if requested is None:
        phrase = _requested_exercise_phrase(text)
        if phrase:
            requested = (None, phrase)

    explicit = requested is not None
    if requested is None and not current_name:
        _push(trace, "knowledge", "clarify")
        return {
            "reply": (
                "你现在没有正在进行的动作。想学哪个动作直接说名字，"
                "比如卧推、深蹲、划船；不在当前计划里的也可以教。"
            ),
            "guidance": [],
            "session": None,
            "actions": [],
        }

    name = requested[1] if requested else current_name
    same_as_current = False
    if current is not None:
        if requested is None:
            same_as_current = True
        else:
            requested_ref, requested_name = requested
            same_as_current = bool(
                (requested_ref and _same_exercise_ref(requested_ref, current_ref))
                or _normalize_exercise_text(requested_name)
                == _normalize_exercise_text(current_name)
            )

    guidance, source_ids, sources = _guidance_material(db, name)

    # 只有要学的动作就是当前训练动作时，才把指导写进当前任务；
    # 否则只讲解，绝不把卧推的提示挂到腿举上。
    session_payload = None
    attached = session is not None and current is not None and same_as_current
    if attached:
        try:
            session_payload = workout_service.update_guidance(
                db, user, session.id, guidance=guidance, source_ids=source_ids
            )
        except ValueError:
            session_payload = None
            attached = False
    if attached and any(word in text for word in REGRESSION_HINTS):
        try:
            session_payload, changes = workout_service.replace_action(
                db, user, session.id,
                reason="按你的情况换成更简单的动作",
                easier=True,
            )
        except ValueError:
            changes = []
        if changes:
            guidance.append("已顺便把当前动作换成了更简单的版本。")
    _push(trace, "knowledge", "ok", hits=len(source_ids))

    if attached:
        tail = "这些提示已经挂到当前动作上，训练页能直接看到。"
    elif session is not None and current_name:
        tail = (
            f"这次只讲「{name}」的动作要点；你正在练的是「{current_name}」，"
            "不会把两个动作的提示混在一起。"
        )
    else:
        tail = "这次先讲动作本身；需要的话我可以把它加进今天的训练。"

    return {
        "reply": (
            f"「{name}」要点：" + "；".join(
                cue.rstrip("。；") for cue in guidance[:2]
            )
            + "。" + tail
        ),
        "guidance": [{
            "name": name,
            "cues": guidance[:4],
            "source_ids": source_ids,
            "sources": sources,
        }],
        "session": session_payload,
        "actions": ([{"type": "open_workout", "label": "回到训练", "session_id": session.id}]
                    if session is not None else []),
    }


def _handle_feedback(
    db: Session, user: User, day: dt.date, text: str,
    decision: Decision, active: WorkoutSession | None, trace: list[dict],
) -> dict:
    completed = not any(word in text for word in INCOMPLETE_HINTS)
    difficulty = 4 if any(word in text for word in ("很累", "太累", "累", "吃力")) else 3
    fatigue = 5 if "很累" in text or "太累" in text else (
        4 if "累" in text or "疲劳" in text else 3
    )
    if any(word in text for word in ("轻松", "不难", "还行")):
        difficulty = 2
    session = active
    if session is None:
        # 所有动作都点完了（status=completed）时 active_session 查不到；
        # 这时应该提交这一场反馈，而不是再新建一场重复记录。
        latest = workout_service.latest_session(db, user, day)
        if latest is not None and latest.status == "completed":
            session = latest
    if session is not None and session.status not in ("finished",):
        payload = workout_service.finish_session(
            db, user, session.id,
            completed=completed,
            difficulty=difficulty,
            fatigue=fatigue,
            note=text,
        )
    else:
        payload = workout_service.record_manual_feedback(
            db, user, day,
            completed=completed,
            difficulty=difficulty,
            fatigue=fatigue,
            note=text,
        )
    summary = payload["summary"]
    state = payload.get("training_state") or {}
    extra = ""
    if state.get("load_reduced"):
        extra = " 你已经连续几次没完成，下一次生成计划时我会主动降低负担。"
    _push(trace, "workout", "finished", completion=summary.get("completion_pct"))
    return {
        "reply": (
            f"记下了：本次完成 {summary.get('completion_pct', 0):.0f}%，"
            f"实际 {summary.get('actual_minutes', 0)} 分钟，"
            f"难度 {summary.get('difficulty')}/5，消耗约 {summary.get('kcal', 0):.0f} kcal。"
            + extra
        ),
        "session": payload["session"],
        "feedback": summary,
        "training_state": state,
        "changes": [
            f"训练记录已保存（{summary.get('done_actions', 0)}/{summary.get('total_actions', 0)} 个动作）",
        ],
        "actions": [
            {"type": "open_progress", "label": "查看进展"},
            {"type": "quick_reply", "label": "按最近状态重排计划",
             "text": "根据我最近的完成情况重新生成计划"},
        ],
    }


def _next_training(db: Session, user: User, day: dt.date) -> dict | None:
    """今天休息时，把下一次训练也放进今日规划，避免只看到"0 个动作"。"""
    plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    if plan is None:
        return None
    version = db.scalar(select(PlanVersion).where(
        PlanVersion.plan_id == plan.id, PlanVersion.version == plan.current_version,
    ))
    if version is None:
        return None
    for offset in range(1, 8):
        target = day + dt.timedelta(days=offset)
        weekday = target.isoweekday()
        rows = db.scalars(
            select(PlanAction)
            .where(PlanAction.plan_version_id == version.id,
                   PlanAction.day_index == weekday,
                   PlanAction.action_type == "exercise")
            .order_by(PlanAction.seq)
        ).all()
        if rows and not any(str(row.slot) == "strength" for row in rows):
            continue
        if not rows:
            continue
        actions = []
        cursor = 18 * 60 + 30
        for row in rows:
            duration = int(row.duration_min or 0) or max(5, int(row.sets or 3) * 2 + 2)
            actions.append({
                "id": row.id,
                "slot_key": row.slot_key,
                "day_index": row.day_index,
                "slot": row.slot,
                "seq": row.seq,
                "action_type": row.action_type,
                "name": (row.meta or {}).get("name_zh") or row.action_ref,
                "action_ref": row.action_ref,
                "sets": row.sets,
                "reps": row.reps,
                "duration_min": row.duration_min,
                "meta": row.meta or {},
                "time": f"{cursor // 60:02d}:{cursor % 60:02d}",
            })
            cursor += duration
        minutes = sum(item.get("duration_min") or 8 for item in actions)
        label = f"{WEEKDAY_NAMES[weekday - 1]} {target.month}/{target.day}"
        names = "、".join(item["name"] for item in actions)
        return {
            "date": target.isoformat(),
            "label": label,
            "day_index": weekday,
            "plan_version": version.version,
            "summary": f"{label} · {names} · 约 {minutes} 分钟",
            "actions": actions,
        }
    return None


def _detailed_schedule(
    db: Session, user: User, text: str, actions: list[dict],
) -> list[dict]:
    """训练日兜底：把动作拆成带时间的逐条安排，而不是只给一行动作名。"""
    memory = load_memory_context(db, user)
    windows = [str(item.get("window")) for item in (memory.get("preferred_windows") or [])
               if item.get("window")]
    match = re.search(r"(\d{1,2}):(\d{2})", text or "")
    start_label = ""
    if match:
        start_minutes = int(match.group(1)) * 60 + int(match.group(2))
    elif windows:
        start_label = windows[0]
        clock = re.search(r"(\d{1,2}):(\d{2})", windows[0])
        start_minutes = (
            int(clock.group(1)) * 60 + int(clock.group(2))
            if clock else 18 * 60 + 30
        )
    else:
        start_minutes = 18 * 60 + 30

    def _duration(action: dict) -> int:
        raw = int(action.get("duration_min") or 0)
        if raw:
            return max(1, raw)
        if action.get("slot") in ("warmup", "mobility"):
            return 10
        return max(5, int(action.get("sets") or 3) * 2 + 2)

    def _clock(minutes: int) -> str:
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    rows = []
    cursor = start_minutes
    for index, action in enumerate(actions):
        duration = _duration(action)
        name = action.get("name") or action.get("action_ref") or "训练"
        if action.get("sets"):
            activity = f"{name} {action.get('sets')} 组，约 {duration} 分钟"
        else:
            activity = f"{name}，约 {duration} 分钟"
        rows.append({
            "time": start_label if index == 0 and start_label else _clock(cursor),
            "activity": activity,
            "why": "按当前计划版本执行；动作要领见下方提示。",
        })
        cursor += duration
    rows.append({
        "time": _clock(cursor),
        "activity": "冷身拉伸 5-8 分钟",
        "why": "训练后做温和静态拉伸，帮助恢复。",
    })
    return rows


def _handle_today_brief(
    db: Session, user: User, day: dt.date, text: str, trace: list[dict]
) -> dict:
    from app.agent import today_agent

    today_actions = effective_day(db, user, day, fallback_next=False).get("actions") or []
    recovery_only = bool(today_actions) and all(
        (item.get("slot") in ("mobility", "stretch")
         or "stretch" in str(item.get("action_ref") or "")
         or "拉伸" in str(item.get("name") or ""))
        for item in today_actions
    )
    if wants_training_today(text) and (not today_actions or recovery_only):
        next_training = _next_training(db, user, day)
        borrowed_actions = (next_training or {}).get("actions") or []
        if borrowed_actions:
            # 用户明确今天要练：把下一次真正力量训练临时提前到今天，覆盖之前的休息/拉伸调整
            save_override(
                db, user, day,
                actions=borrowed_actions,
                summary="今天改为训练日，按下一次力量训练提前安排",
                constraints={"train_today": True},
                reason=text,
                base_version=(next_training or {}).get("plan_version"),
                borrowed_from=(next_training or {}).get("day_index"),
                changes=["今天改为训练日", f"借用第 {(next_training or {}).get('day_index')} 天训练"],
            )
            _push(trace, "day", "train_today", borrowed_from=(next_training or {}).get("day_index"))
            today_actions = borrowed_actions
    result = today_agent.run(db, user, day, text, client=None)
    output = result.final or {}
    today_actions = effective_day(db, user, day, fallback_next=False).get("actions") or []
    if output and today_actions:
        schedule = output.get("schedule") or []
        # 模型只给了一行/没给时间时，用真实动作补成逐条时间安排
        if len(schedule) <= 1:
            output["schedule"] = _detailed_schedule(db, user, text, today_actions)
    if output and not today_actions:
        next_training = _next_training(db, user, day)
        output["next_training"] = next_training
        if next_training:
            output["summary"] = f"今天按计划休息；下一次训练是{next_training['summary']}。"
            output["schedule"] = []
        else:
            output["summary"] = "今天按计划休息；当前版本没有安排后续训练。"
            output["schedule"] = []
    if output:
        from app.domain.today_snapshot import save_today_plan

        # 今日餐食建议也留一条 run：前端的"应用选中的餐食"需要 run_id 做归属校验，
        # 不能因为入口换成了统一 Agent 就丢掉这个能力。
        today_run = AgentRun(
            id=new_id(),
            user_id=user.id,
            kind="today_plan",
            mode="agent",
            status="succeeded",
            input={"text": text, "date": day.isoformat(), "source": "coach_chat"},
            output=output,
            trace=result.trace,
            finished_at=dt.datetime.now(dt.timezone.utc),
        )
        db.add(today_run)
        db.flush()
        save_today_plan(db, user, day, request_text=text, output=output,
                        run_id=today_run.id)
    summary = output.get("summary") or "今天的安排已经整理好。"
    _push(trace, "today_brief", "ok", steps=len(result.trace))
    return {
        "reply": summary,
        "today_brief": output,
        "today_brief_run_id": today_run.id,
        "changes": ["已重新读取计划、营养目标与餐食选项"],
        "actions": [],
        "warnings": [] if output.get("meals") else ["这次没取到餐食选项，训练部分不受影响"],
    }


def _handle_review_followup(
    db: Session, user: User, day: dt.date, recent: list[dict], trace: list[dict],
) -> dict:
    """用户接受上一条综合建议时，直接执行建议里的第一步，而不是再问一遍。"""
    last_agent = next(
        (item for item in reversed(recent or []) if item.get("role") == "agent"), {},
    )
    reference = str((last_agent or {}).get("text") or "")
    if any(word in reference for word in ("最小训练", "15-20 分钟", "20 分钟")):
        decision = Decision(
            "adjust_today", "today", {"minutes": 20}, [],
            reason="执行上一条建议：20 分钟最小训练",
        )
        result = _handle_adjust_today(
            db, user, day, "今天只做 20 分钟的最小训练", decision, trace,
        )
        result["reply"] = (
            "好，按上一条建议，我先给你排一个 20 分钟的最小训练，"
            "不需要你自己删计划。\n" + str(result.get("reply") or "")
        )
        result["changes"] = ["按上一条建议缩短今天训练"] + list(result.get("changes") or [])
        return result
    if any(word in reference for word in ("下一周", "训练量降一档")):
        decision = Decision(
            "create_plan", "long_term", {}, [],
            reason="执行上一条建议：调整下一周训练量",
        )
        result = _handle_create_plan(
            db, user, "根据上一条建议，调整下一周训练量", decision, trace,
        )
        result["reply"] = "好，按上一条建议处理下一周。\n" + str(result.get("reply") or "")
        return result
    # 没有可识别的具体建议时，仍然先给今天一个可执行安排，不回到 clarify
    return _handle_today_brief(
        db, user, day, "帮我安排今天，先给我一个可以马上执行的最小训练", trace,
    )


def _handle_coach_review(db: Session, user: User, day: dt.date, trace: list[dict]) -> dict:
    """结合档案、长期记忆、当前计划和最近真实训练，给出下一步建议。

    用户不需要先说“今天/长期/某个动作”才能得到建议；这段是综合评估，
    不是澄清，也不是直接改计划。
    """
    from app.domain.memory import EQUIPMENT_LABELS
    from app.domain.progress import build_progress_insights

    memory = load_memory_context(db, user)
    insights = build_progress_insights(db, user, days=7)
    weekly = insights.get("weekly") or {}
    findings = insights.get("findings") or []
    plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    effective = effective_day(db, user, day, fallback_next=False)
    day_actions = effective.get("actions") or []
    day_minutes = sum(int(item.get("duration_min") or 0) for item in day_actions)

    goal = str(memory.get("goal") or "未设置")
    weekly_days = memory.get("weekly_days")
    session_minutes = memory.get("session_minutes")
    windows = memory.get("preferred_windows") or []
    window_text = "、".join(
        f"{item.get('label') or '训练'}（{item.get('window')}）" for item in windows
    )
    equipment = memory.get("equipment") or []
    injuries = memory.get("injuries") or []
    dislikes = memory.get("dislikes") or []

    profile_line = f"目标：{goal}；频率：每周 {weekly_days or '—'} 次；单次 {session_minutes or '—'} 分钟"
    if window_text:
        profile_line += f"；偏好时间：{window_text}"
    if equipment:
        profile_line += "；可用器械：" + "、".join(
            EQUIPMENT_LABELS.get(str(item), str(item)) for item in equipment[:4]
        )

    sessions = int(weekly.get("sessions") or 0)
    completion = float(weekly.get("avg_completion_pct") or 0)
    fatigue = weekly.get("avg_fatigue")
    if sessions:
        recent_line = (
            f"最近 7 天完成 {sessions} 次，平均完成率 {completion:.0f}%，"
            f"累计 {int(weekly.get('total_minutes') or 0)} 分钟。"
        )
    else:
        recent_line = "最近 7 天还没有完成训练记录。"

    suggestions: list[str] = []
    if sessions == 0:
        suggestions.append(
            "先别改计划，本周先完成一场最小训练：从今天安排里选 2-3 个动作，"
            "做 15-20 分钟也算完成；先建立连续记录。"
        )
        if day_actions:
            suggestions.append(
                f"今天计划有 {len(day_actions)} 个动作、约 {day_minutes or 30} 分钟。"
                "时间不够就直接告诉我实际分钟数，我帮你缩，不需要自己删计划。"
            )
        else:
            suggestions.append("当前今天没有训练安排；如果你愿意，我可以按你的时间生成一份可执行的计划。")
    elif completion < 70 or int(weekly.get("consecutive_incomplete") or 0) >= 2:
        suggestions.append(
            "下一周先把训练量降一档：单次少 1 个动作或每组少 1 组，"
            "连续完成 2 次后再加回来。"
        )
        suggestions.append("如果是时间不够，优先缩短单次时长，不要靠跳过动作硬撑。")
    elif fatigue is not None and float(fatigue) >= 4:
        suggestions.append("下一次训练先减 1 组，保证睡眠和恢复；疲劳回落后再加量。")
        suggestions.append("这周不加新动作，先把已有动作做稳。")
    else:
        suggestions.append(
            "保持现在的频率，下一次只加一个变量：多 1 组或多 2 次，"
            "不要同时加重量、加动作和加频率。"
        )
        suggestions.append("连续两周都能完成，再考虑把重量提高 2.5%-5%。")

    goal_code = str(memory.get("goal_code") or "")
    if goal_code == "fat_loss":
        suggestions.append("饮食先守住蛋白质和温和热量缺口，不用靠额外加有氧来补。")
    elif goal_code in ("muscle_gain", "gain_muscle"):
        suggestions.append("热量小幅盈余，蛋白质按体重分配，训练后 1-2 小时补一餐。")
    else:
        suggestions.append("先把每周训练频率稳定下来，再谈强度和体型变化。")

    if injuries:
        suggestions.append(f"训练时避开或替换涉及 {('、'.join(str(item) for item in injuries[:3]))} 的动作。")
    elif dislikes:
        suggestions.append(f"计划里会继续避开 {'、'.join(str(item) for item in dislikes[:3])}。")

    finding_text = ""
    if findings:
        finding_text = f"我注意到：{findings[0].get('title')}。{findings[0].get('detail')}"

    body = [
        f"我先按你的档案和最近记录对齐：",
        f"· {profile_line}",
        f"· {recent_line}",
    ]
    if plan is not None:
        body.append(f"· 当前计划 v{plan.current_version}；今天 {len(day_actions)} 个动作。")
    if finding_text:
        body.append(f"· {finding_text}")
    body.append("")
    body.append("下一步建议：")
    for index, item in enumerate(suggestions[:4], 1):
        body.append(f"{index}. {item}")

    _push(trace, "coach_review", "ok", sessions=sessions, completion=completion,
          plan_version=getattr(plan, "current_version", None))
    return {
        "reply": "\n".join(body),
        "actions": [{"type": "open_progress", "label": "看真实训练记录"}],
    }


def _handle_progress_review(db: Session, user: User, trace: list[dict]) -> dict:
    """进展页问答：读取真实训练统计，而不是让模型凭空安慰用户。"""
    from app.domain.progress import build_progress_insights

    insights = build_progress_insights(db, user, days=7)
    findings = insights.get("findings") or []
    if findings:
        summary = "；".join(item["title"] for item in findings[:2])
        detail = " ".join(item["detail"] for item in findings[:2])
        reply = f"根据最近的真实训练记录：{summary}。{detail}"
    else:
        weekly = insights.get("weekly") or {}
        reply = (
            f"最近 7 天完成 {weekly.get('sessions', 0)} 次训练，"
            f"平均完成率 {weekly.get('avg_completion_pct', 0):.0f}%，"
            f"累计 {weekly.get('total_minutes', 0)} 分钟。目前没有明显异常。"
        )
    _push(trace, "progress", "ok", findings=len(findings))
    return {
        "reply": reply,
        "progress_insights": insights,
        "actions": [
            {"type": "quick_reply", "label": "根据这些发现调整下一周",
             "text": "根据我最近的完成情况，帮我调整下一周的训练量"},
            {"type": "open_progress", "label": "回到进展页"},
        ],
    }


def _handle_mcd_order(text: str, decision: Decision, trace: list[dict]) -> dict:
    """把麦当劳点餐交给聊天里的内联点餐面板；token 已由前端脱敏保存在本机。"""
    entities = {
        "instruction": text,
        "city": str(decision.constraints.get("mcd_city") or ""),
        "store": str(decision.constraints.get("mcd_store") or ""),
        "foods": list(decision.constraints.get("mcd_foods") or []),
    }
    _push(trace, "mcd", "open", instruction=text, city=entities["city"],
          store=entities["store"], foods=entities["foods"])
    return {
        "reply": (
            "好，我帮你走麦当劳官方 MCP，停在付款前。"
            "先确认门店；没给门店就从附近门店里选，选了店再选餐品。"
        ),
        "mcd_order": entities,
    }


def _handle_clarify(
    db: Session, user: User, day: dt.date, text: str,
    decision: Decision, trace: list[dict],
) -> dict:
    _push(trace, "intent", "clarify")
    return {
        "reply": (
            "我还没听出你要改哪一层。可以直接说「今天只有 30 分钟」、"
            "「以后都改到 20:00 之后训练」或「下周我想练背」，我按内容判断，不用选菜单。"
        ),
        "actions": [],
    }


# ---------------------------------------------------------------- 小工具


def _persist_candidates(
    db: Session, user: User, decision: Decision, text: str, trace: list[dict]
) -> list:
    """长期候选只在"长期 / 稳定"语境下写入；临时要求永远不落长期记忆。"""
    if not decision.candidates:
        return []
    # create_plan / update_preference 在各自 handler 里已经用 Profile+Memory 同步逻辑处理
    if decision.intent in ("create_plan", "update_preference"):
        return []
    if decision.intent == "adjust_plan" and "这周" in text and not any(
            word in text for word in ("以后", "今后", "长期")):
        _push(trace, "memory", "skipped", reason="本周临时变更不写长期记忆")
        return []
    if decision.intent == "adjust_today" and not any(
            word in text for word in LONG_TERM_HINTS):
        # 双保险：今天只有 20 分钟 → 绝不写长期记忆
        _push(trace, "memory", "skipped", reason="临时上下文不写长期记忆")
        return []
    preference = apply_preference_candidates(
        db, user, decision.candidates, text, evidence=text,
    )
    if preference.needs_confirmation:
        _push(trace, "memory", "needs_confirmation", conflicts=preference.conflicts)
        return []
    saved = preference.saved
    if saved:
        _push(trace, "memory", "saved", count=len(saved))
    return saved


def _store_temp_context(db: Session, user: User, day: dt.date, constraints: dict,
                        text: str) -> None:
    if constraints.get("minutes"):
        set_temporary(db, user, day, "available_minutes",
                      {"minutes": int(constraints["minutes"])}, text=text)
    if constraints.get("location") or constraints.get("no_gym"):
        set_temporary(db, user, day, "location",
                      {"location": constraints.get("location") or "home",
                       "no_gym": bool(constraints.get("no_gym"))}, text=text)
    if constraints.get("equipment_busy"):
        set_temporary(db, user, day, "equipment_busy", {"busy": True}, text=text)
    if constraints.get("injured_regions"):
        set_temporary(db, user, day, "discomfort",
                      {"regions": constraints["injured_regions"]}, text=text)
    if constraints.get("preferred_window"):
        set_temporary(db, user, day, "time_window",
                      constraints["preferred_window"], text=text)


_BASE_EXERCISE_REFS = {
    "bench-press", "squat", "deadlift", "barbell-row", "overhead-press",
    "leg-press", "pull-up", "plank", "bicep-curl",
}


def _normalize_exercise_text(value: str) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _exercise_name_candidates() -> list[tuple[str, str]]:
    """动作目录 + 旧模板里的中文名，供“卧推/深蹲”这类口语短词解析。"""
    from app.domain.catalog import EXTRA_ZH
    from app.domain.exercise_catalog import EXERCISE_CATALOG
    from app.domain.planning import EXERCISES

    rows: dict[tuple[str, str], None] = {}

    def add(ref: str, name: str) -> None:
        ref = str(ref or "").strip()
        name = str(name or "").strip()
        if ref and name:
            rows[(ref, name)] = None

    for ref, item in EXERCISE_CATALOG.items():
        add(ref, item.get("name_zh"))
        add(ref, item.get("name_en"))
    for ref, item in EXERCISES.items():
        add(ref, item.get("name"))
    for ref, name in EXTRA_ZH.items():
        add(ref, name)
    return list(rows)


def _resolve_exercise(text: str) -> tuple[str, str] | None:
    """把用户口语里的动作短词对到动作目录。

    不依赖某个固定句式：
    - “杠铃卧推”这种正式名直接命中；
    - “卧推”这种短词用最长公共子串命中最短的正式动作名，
      例如 卧推 -> 杠铃卧推，深蹲 -> 杠铃深蹲。
    """
    normalized = _normalize_exercise_text(text)
    if not normalized:
        return None

    exact: tuple[tuple, str, str] | None = None
    fuzzy: tuple[tuple, str, str] | None = None
    for ref, name in _exercise_name_candidates():
        candidate = _normalize_exercise_text(name)
        if len(candidate) < 2:
            continue
        if candidate in normalized:
            rank = (len(candidate), ref in _BASE_EXERCISE_REFS, ref)
            if exact is None or rank > exact[0]:
                exact = (rank, ref, name)
            continue
        for term_index, term in enumerate(_exercise_query_terms(name)[1:], 1):
            core = _normalize_exercise_text(term)
            if len(core) < 2 or core not in normalized:
                continue
            # 只认动作目录里真实存在的核心词，不拿任意两个相同的字去硬凑。
            rank = (
                len(core), -term_index, -len(candidate),
                ref in _BASE_EXERCISE_REFS, ref,
            )
            if fuzzy is None or rank > fuzzy[0]:
                fuzzy = (rank, ref, name)
    chosen = exact or fuzzy
    return (chosen[1], chosen[2]) if chosen else None


def _same_exercise_ref(left: str, right: str) -> bool:
    def normalize(ref: str) -> str:
        return str(ref or "").replace("-", "_").lower()

    return bool(left and right and normalize(left) == normalize(right))


_EXERCISE_PREFIXES = (
    "史密斯", "杠铃", "哑铃", "绳索", "器械", "坐姿", "站姿", "单臂", "双臂",
    "上斜", "下斜", "窄握", "宽握", "俯卧", "仰卧", "传统", "直臂", "锤式",
    "阿诺德", "地板", "颈后", "反手", "正手",
)


def _exercise_query_terms(name: str) -> list[str]:
    """生成动作名和去掉器械/角度前缀后的核心词，用于找对应要领。"""
    current = str(name or "").strip()
    terms: list[str] = []
    while current and current not in terms:
        terms.append(current)
        stripped = False
        for prefix in _EXERCISE_PREFIXES:
            if current.startswith(prefix) and len(current) - len(prefix) >= 2:
                current = current[len(prefix):]
                stripped = True
                break
        if not stripped:
            break
    return terms


def _requested_exercise_phrase(text: str) -> str | None:
    """动作不在目录里时，仍从“教我 XX / XX 怎么做”里取出动作名。"""
    generic = {
        "这个动作", "动作", "一下", "我", "怎么", "如何", "训练", "健身",
        "这个", "那个", "它", "什么",
    }
    patterns = (
        r"(?:教我|教一下|想学|要学|学习|讲解|讲讲|说说)"
        r"([\u4e00-\u9fa5A-Za-z0-9·\-]{2,24})",
        r"([\u4e00-\u9fa5A-Za-z0-9·\-]{2,24})"
        r"(?:怎么做|怎么练|动作要领|姿势|教程)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        phrase = match.group(1).strip()
        phrase = re.sub(r"^(?:怎么练|怎么做|如何练|如何做|练|做)", "", phrase)
        phrase = re.sub(r"(?:怎么做|怎么练|动作要领|姿势|教程|吧|呀|啊)+$", "", phrase)
        phrase = phrase.strip(" ，,。.!！?？")
        if len(phrase) >= 2 and phrase not in generic and not any(
            word in phrase for word in
            ("不知道", "不会", "今天", "明天", "现在", "怎么", "如何", "这个动作")
        ):
            return phrase[:24]
    return None


def _action_name_in_text(text: str) -> str | None:
    resolved = _resolve_exercise(text)
    return resolved[1] if resolved else None


def _rag_exercise_hit(name: str) -> dict | None:
    """在动作语料里找“这个动作本身”的要领，而不是泛泛的部位清单。"""
    from app.knowledge.rag import search as rag_search

    terms = _exercise_query_terms(name)
    try:
        hits = rag_search(f"{name} 动作要领", limit=30)
    except Exception:  # noqa: BLE001 - 语料不可用就回退到内置知识
        return None
    best: dict | None = None
    best_rank: tuple | None = None
    for hit in hits:
        title = str(hit.get("title") or "")
        main = re.split(r"[（(]", title, maxsplit=1)[0].strip()
        normalized_main = _normalize_exercise_text(main)
        for index, term in enumerate(terms):
            normalized_term = _normalize_exercise_text(term)
            if not normalized_term:
                continue
            if normalized_main == normalized_term:
                rank = (4, -index, -len(normalized_main))
            elif normalized_main.startswith(normalized_term):
                rank = (3, -index, -len(normalized_main))
            elif normalized_term in normalized_main:
                rank = (2, -index, -len(normalized_main))
            else:
                continue
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best = hit
    return best


def _split_rag_cues(content: str) -> list[str]:
    marker = "动作要领："
    body = content.split(marker, 1)[1] if marker in content else content
    cues: list[str] = []
    for part in body.split("。"):
        text = part.strip(" 。；")
        if text:
            cues.append(text[:220])
    return cues[:4]


def _guidance_material(db: Session, name: str) -> tuple[list[str], list[str], list[dict]]:
    # 1) 外部动作语料优先：302 个动作都有单独的动作要领
    hit = _rag_exercise_hit(name)
    if hit is not None:
        cues = _split_rag_cues(str(hit.get("content") or ""))
        if cues:
            source_id = str(hit.get("id") or "")
            return cues, [source_id], [{
                "id": source_id,
                "title": hit.get("title") or name,
                "source_url": hit.get("source_url") or "",
            }]

    # 2) 内置知识库：只接受标题/标签明确指向这个动作的条目
    terms = _exercise_query_terms(name)
    hits = kb_search(db, f"{name} 动作要领", limit=12)
    cues: list[str] = []
    source_ids: list[str] = []
    sources: list[dict] = []
    for hit in hits:
        chunk = hit.chunk
        if str(chunk.topic or "") != "technique":
            continue
        haystack = f"{chunk.title or ''} {' '.join(str(tag) for tag in (chunk.tags or []))}"
        if not any(term and term in haystack for term in terms):
            continue
        content = str(chunk.content or "").strip()
        if not content:
            continue
        cues.append(content[:200])
        source_ids.append(chunk.id)
        sources.append({
            "id": chunk.id, "title": chunk.title,
            "source_url": chunk.source_url,
        })
        if len(cues) >= 3:
            break
    if cues:
        return cues, source_ids, sources

    # 3) 语料里没有这个动作：给通用执行要点，也不要拿别的动作顶替
    return _default_cues(name), [], []


def _default_cues(name: str) -> list[str]:
    # 动作不在语料里时只给通用执行原则，不编造“下放几秒/用多重”。
    return [
        "先用低强度做 1-2 组，把动作路线走顺，全程保持核心稳定、呼吸顺畅。",
        "动作幅度以可控为准，宁可减少次数或负重，也不要借力、憋气或硬顶。",
        "如果关节出现刺痛、头晕或明显气短，立刻停下来；持续不适请就医。",
    ]


def _polish_reply(result: dict, decision: Decision, text: str, saved: list) -> str:
    reply = str(result.get("reply") or "").strip()
    if reply:
        return reply
    if result.get("intent") == "update_preference" and saved:
        return "记住了：" + "；".join(row.value for row in saved)
    return "收到。"


def _record_run(
    db: Session, user: User, text: str, page: str, day: dt.date,
    decision: Decision, result: dict, trace: list[dict],
    *, page_context: dict | None = None, thread: str = "coach",
) -> str:
    run = AgentRun(
        id=new_id(),
        user_id=user.id,
        kind="coach_chat",
        mode="agent",
        status=result.get("overall_status") or ("succeeded" if not result.get("error") else "failed"),
        input={"text": text, "page": page, "date": day.isoformat(),
               "thread": thread, "context": page_context or {}},
        output={
            "intent": decision.intent,
            "scope": decision.scope,
            "reply": result.get("reply"),
            "changes": result.get("changes") or [],
            "task_results": result.get("task_results") or [],
            "task_plan": result.get("task_plan") or [],
        },
        trace=trace,
        latency_ms=result.get("latency_ms", 0),
        finished_at=dt.datetime.now(dt.timezone.utc),
    )
    db.add(run)
    db.flush()
    return run.id


_STORED_RESULT_KEYS = (
    "intent", "intent_label", "scope", "reason", "reply",
    "changes", "actions", "day", "session", "plan_draft", "patch_request",
    "today_brief", "today_brief_run_id", "mcd_order", "memories", "memory_saved",
    "guidance", "feedback", "progress_insights", "warnings", "blocked",
    "task_plan", "task_results", "execution_history", "overall_status",
    "pending_confirmation", "context_used",
)


def _stored_result(result: dict) -> dict:
    """只落历史卡片需要的结果，避免把 trace/页面上下文写进消息表。"""
    return {
        key: result[key]
        for key in _STORED_RESULT_KEYS
        if result.get(key) not in (None, [], {}, "")
    }


def _record_messages(
    db: Session, user: User, text: str, result: dict, *, thread: str = "coach",
) -> None:
    stamp = dt.datetime.now(dt.timezone.utc)
    db.add(AgentMessage(
        id=new_id(), user_id=user.id, thread=thread, role="user", text=text[:2000],
        intent={"intent": result.get("intent"), "scope": result.get("scope")},
        run_id=result.get("run_id"), created_at=stamp,
    ))
    db.add(AgentMessage(
        id=new_id(), user_id=user.id, thread=thread, role="agent",
        text=str(result.get("reply") or "")[:2000],
        intent={"intent": result.get("intent"), "scope": result.get("scope")},
        actions=result.get("actions") or [],
        result=_stored_result(result),
        run_id=result.get("run_id"), created_at=stamp + dt.timedelta(microseconds=1),
    ))


def recent_messages(db: Session, user: User, thread: str, limit: int = 8) -> list[dict]:
    rows = db.scalars(
        select(AgentMessage)
        .where(AgentMessage.user_id == user.id, AgentMessage.thread == thread)
        .order_by(AgentMessage.created_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "role": row.role,
            "text": row.text,
            "intent": (row.intent or {}).get("intent"),
            "scope": (row.intent or {}).get("scope"),
            "actions": [
                {"type": action.get("type"), "label": action.get("label")}
                for action in (row.actions or [])
                if isinstance(action, dict)
            ],
            "result_kind": str((row.result or {}).get("kind") or ""),
            "task_plan": (row.result or {}).get("task_plan") or [],
            "task_results": (row.result or {}).get("task_results") or [],
        }
        for row in reversed(rows)
    ]


def mark_stored_plan_result(
    db: Session, user: User, *, status: str,
    draft_id: str | None = None, request_id: str | None = None,
    version: int = 0,
) -> None:
    """计划确认/废弃后，把历史消息里的草案状态同步掉，避免刷新后重复确认。"""
    rows = db.scalars(
        select(AgentMessage).where(
            AgentMessage.user_id == user.id,
            AgentMessage.role == "agent",
        )
    ).all()
    for row in rows:
        saved = dict(row.result or {})
        changed = False
        draft = saved.get("plan_draft") or {}
        if draft_id and str(draft.get("draft_id") or "") == draft_id:
            next_draft = dict(draft)
            next_draft["status"] = status
            if version:
                next_draft["committed_version"] = version
            saved["plan_draft"] = next_draft
            saved["actions"] = []
            changed = True
        patch = saved.get("patch_request") or {}
        if request_id and str(patch.get("request_id") or "") == request_id:
            next_patch = dict(patch)
            next_patch["status"] = status
            if version:
                next_patch["committed_version"] = version
            saved["patch_request"] = next_patch
            saved["actions"] = []
            changed = True
        if changed:
            row.result = saved
    db.commit()


def normalize_thread(thread: str | None) -> str:
    """对话线程 ID 只允许短标识；空值回退到旧的单线程。"""
    value = re.sub(r"[^a-zA-Z0-9_-]", "", (thread or "").strip())[:32]
    return value or "coach"


def _iso_utc(value: dt.datetime | None) -> str:
    """SQLite 读出来常常是 naive datetime；按 UTC 补时区，前端才会显示本地时间。"""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.isoformat()


def conversations_payload(db: Session, user: User, limit: int = 30) -> list[dict]:
    """按 thread 聚合对话列表：标题取第一条用户消息，预览取最后一条。"""
    rows = db.scalars(
        select(AgentMessage)
        .where(AgentMessage.user_id == user.id)
        .order_by(AgentMessage.created_at.asc())
    ).all()
    grouped: dict[str, list[AgentMessage]] = {}
    for row in rows:
        grouped.setdefault(normalize_thread(row.thread), []).append(row)
    items: list[dict] = []
    for thread, thread_rows in grouped.items():
        first_user = next((r for r in thread_rows if r.role == "user"), thread_rows[0])
        last = thread_rows[-1]
        title = " ".join((first_user.text or "").split())[:30] or "新的对话"
        preview = " ".join((last.text or "").split())[:48]
        updated = _iso_utc(last.created_at)
        items.append({
            "thread": thread,
            "title": title,
            "preview": preview,
            "message_count": len(thread_rows),
            "updated_at": updated,
        })
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    return items[:max(1, min(limit, 50))]


def messages_payload(
    db: Session, user: User, limit: int = 20, thread: str = "coach",
) -> list[dict]:
    thread = normalize_thread(thread)
    rows = db.scalars(
        select(AgentMessage)
        .where(AgentMessage.user_id == user.id, AgentMessage.thread == thread)
        .order_by(AgentMessage.created_at.desc())
        .limit(max(1, min(limit, 100)))
    ).all()
    return [
        {
            "id": row.id,
            "role": row.role,
            "text": row.text,
            "intent": row.intent or {},
            "actions": row.actions or [],
            "result": row.result or {},
            "created_at": _iso_utc(row.created_at),
        }
        for row in reversed(rows)
    ]


def _run(db, user, text, **kwargs):
    kwargs.pop("confirmation", None)
    kwargs.pop("checkout_action", None)
    kwargs.pop("commerce_action", None)
    return _legacy_run(db, user, text, **kwargs)


def run(db, user, text, *, request_id=None, **kwargs):
    """Claim an optional request key before execution; replay only completed results.

    Incomplete claims deliberately require reconciliation, never blind re-execution.
    """
    from hashlib import sha256
    from uuid import NAMESPACE_URL, uuid5
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    db.info["coach_request_key"] = request_id or new_id()
    if not request_id:
        return _run(db, user, text, **kwargs)
    key = str(uuid5(NAMESPACE_URL, f"coach:{user.id}:{request_id}"))
    # Clients are runtime objects, not part of request identity.
    fingerprint = sha256(json.dumps({"text": text, **{k: v for k, v in kwargs.items() if k != "client"}},
                                   sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()

    def replay(row):
        if row.input.get("fingerprint") != fingerprint:
            raise HTTPException(409, "request_id 已用于不同请求")
        if row.status != "succeeded":
            raise HTTPException(409, "请求执行中或需要核对任务记录，请勿重复执行")
        return row.output

    previous = db.get(AgentRun, key)
    if previous:
        return replay(previous)
    claim = AgentRun(id=key, user_id=user.id, kind="coach_request", mode="agent",
                     status="running", input={"fingerprint": fingerprint,
                     "request_id": request_id, "thread": kwargs.get("thread", "coach")})
    db.add(claim)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return replay(db.get(AgentRun, key))
    try:
        result = _run(db, user, text, **kwargs)
        claim = db.get(AgentRun, key)
        claim.output = result
        claim.status = "succeeded"
        claim.finished_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        return result
    except Exception:
        db.rollback()
        claim = db.get(AgentRun, key)
        claim.status = "needs_reconciliation"
        db.commit()
        raise
