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
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import append_event
from app.domain import coach as coach_plans
from app.domain import requests as patch_requests
from app.domain import workout as workout_service
from app.domain.day_plan import (
    apply_constraints,
    constraint_from_text,
    constraint_summary,
    effective_day,
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
from app.domain.timeutil import user_today
from app.knowledge.retriever import search as kb_search
from app.knowledge.service import RED_FLAGS
from app.models import (
    AgentMessage,
    AgentRun,
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
    "mcd_order": "麦当劳点餐",
    "clarify": "需要补充信息",
    "safety": "安全阻断",
}

LONG_TERM_HINTS = ("以后", "今后", "通常", "平时", "一直", "长期", "每次", "每天",
                   "每周", "习惯", "总是")
TEMP_HINTS = ("今天", "今晚", "这次", "现在", "临时", "只剩", "只有")
GOAL_HINTS = ("减脂", "减重", "增肌", "增重", "心肺", "耐力", "体态", "综合健康",
              "想瘦", "降体脂", "长肌肉")
CREATE_HINTS = ("生成计划", "制定计划", "重新制定", "重新生成", "换一份计划", "换一个计划",
                "重新规划", "做一个计划", "给我一份计划", "帮我安排计划", "开始健身",
                "从头开始", "重排计划", "按这个重新生成")
BRIEF_HINTS = ("规划今天", "安排今天", "今天吃什么", "今日安排", "今日规划",
               "帮我规划", "再规划", "重新规划今天", "今天怎么练",
               "今天的训练计划", "今日训练计划", "生成今天", "生成今日",
               "今天练什么", "今天训练什么")
MEMORY_QUERY_HINTS = ("你记得", "记住了什么", "我的记忆", "查看记忆", "我的偏好",
                      "你了解我", "记忆里")
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
    for key in ("injured_regions", "avoid_actions"):
        value = raw.get(key)
        if isinstance(value, list):
            cleaned = [str(item).strip() for item in value if str(item).strip()]
            if cleaned:
                out[key] = cleaned
    return out


def classify_with_model(client, text: str, *, has_plan: bool, in_workout: bool,
                        page: str, recent: list[dict] | None = None) -> Decision | None:
    """规则没识别出来时，让模型做一次轻量语义解析。

    规则负责高频、确定性的场景；这里专门补“用户换了说法”的情况，
    避免把「我想吃牛肉堡，在北京三里屯帮我点餐」这类话写成又一个关键词。
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
- 训练中换动作/器械被占/太累/只剩几分钟 => adjust_workout
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
    try:
        reply = client.complete(
            [{"role": "user", "content": prompt}],
            tools=None, temperature=0, max_tokens=260,
        )
        content = (reply.get("message") or {}).get("content") or ""
        data = _extract_json_object(content)
    except Exception:  # 模型不可用/超时：保留规则结果
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
            detail = client.complete(
                [{"role": "user", "content": detail_prompt}],
                tools=None, temperature=0, max_tokens=220,
            )
            constraints = _clean_model_constraints(
                _extract_json_object((detail.get("message") or {}).get("content") or "")
            )
        except Exception:
            constraints = {}
    return Decision(intent, scope, constraints, [],
                    reason=str(data.get("reason") or "模型语义解析")[:120])


# ---------------------------------------------------------------- 执行


def run(
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
    day = day or user_today(db.get(Profile, user.id))
    text = (text or "").strip()
    thread = normalize_thread(thread)
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
    memory = load_memory_context(db, user)
    training_state = recent_training_state(db, user)
    recent = recent_messages(db, user, thread)

    decision = classify(
        text, has_plan=plan is not None, in_workout=active is not None, page=page,
        recent=recent,
    )
    rule_decision = decision
    # 安全红线始终由确定性规则先拦住；其余意图优先让模型理解用户原话。
    # 规则只在模型不可用/返回不合法时兜底，避免继续堆关键词。
    semantic_used = False
    if decision.intent != "safety" and client is not None:
        semantic = classify_with_model(
            client, text, has_plan=plan is not None,
            in_workout=active is not None, page=page, recent=recent,
        )
        if semantic is not None:
            # 模型负责理解意图，规则抽取仍可补结构化候选/约束（时间/目标/休息等）
            if semantic.intent in ("update_preference", "create_plan") and not semantic.candidates:
                semantic.candidates = extract_candidates(text)
            decision = semantic
            semantic_used = True
            trace.append({"node": "intent_model", "status": "ok", "ts": _now(),
                          "detail": {"intent": semantic.intent,
                                     "reason": semantic.reason,
                                     "candidates": [c.get("category") for c in semantic.candidates]}})
        # 模型偶发把点餐判成 clarify：用“点餐动作 + 食物/地点”的结构兜底。
        # 不是写死品牌或菜名，点餐动词本身足够明确。
        if decision.intent == "clarify" and any(
                word in text for word in ("点餐", "订餐", "外卖", "帮我点", "点一份", "点个", "下单")):
            decision = Decision("mcd_order", "meal", {}, [],
                                reason="用户表达点餐请求（语义兜底）")
        # 模型偶尔误判 clarify 时，规则已经能确定的意图优先兜底
        if decision.intent == "clarify" and rule_decision.intent != "clarify":
            decision = rule_decision
        if decision.intent in ("clarify", "adjust_today") and wants_training_today(text) and plan is not None:
            decision = Decision("today_brief", "today", {}, [],
                                reason="用户明确今天要训练（语义兜底）")
    if recent and _is_followup_text(text):
        last_agent = next(
            (item for item in reversed(recent) if item.get("role") == "agent"), None,
        )
        if (last_agent or {}).get("intent") == "coach_review":
            decision = Decision(
                "review_followup", "today", {}, [],
                reason="用户接受并执行上一条综合建议",
            )

    if (
        semantic_used
        and decision.intent == "update_preference"
        and "确认更新档案" not in text
        and "以后都按这个来" not in text
        and not any(word in text for word in ("以后都", "一直", "长期"))
    ):
        decision.needs_confirmation = True
    trace.append({
        "node": "intent",
        "status": "ok",
        "ts": _now(),
        "detail": {
            "intent": decision.intent,
            "scope": decision.scope,
            "reason": decision.reason,
            "constraints": {key: value for key, value in decision.constraints.items()
                            if value not in (None, [], {}, False)},
            "memory_candidates": [item["category"] for item in decision.candidates],
        },
    })

    result = _base_result(decision, trace)
    try:
        if decision.intent == "safety":
            result.update(_safety_reply(trace))
        elif decision.intent == "query_memory":
            result.update(_handle_query_memory(db, user))
        elif decision.intent == "edit_memory":
            result.update(_handle_edit_memory(db, user, text, trace))
        elif decision.intent == "create_plan":
            result.update(_handle_create_plan(db, user, text, decision, trace, client))
        elif decision.intent == "adjust_plan":
            result.update(_handle_adjust_plan(db, user, text, decision, trace))
        elif decision.intent == "update_preference":
            result.update(_handle_update_preference(db, user, text, decision, trace))
        elif decision.intent == "adjust_today":
            result.update(_handle_adjust_today(db, user, day, text, decision, trace))
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

    # 记忆抽取：只有长期信号 / 稳定信息才写入；临时要求已经在 adjust_today 里处理
    saved_memories = _persist_candidates(db, user, decision, text, trace)
    if saved_memories:
        result["memory_saved"] = [memory_payload(row) for row in saved_memories]
    elif result.get("memory_saved"):
        result["memory_saved"] = list(result["memory_saved"])
    else:
        result["memory_saved"] = []
    if result["memory_saved"] and not result.get("memories"):
        result["memories"] = [
            memory_payload(row) for row in list_memories(db, user)
        ]

    result["page_context"] = page_context or {}
    result["training_state"] = training_state
    # 处理完偏好变更后再读一次，保证返回给前端的上下文是更新后的档案
    memory_after = load_memory_context(db, user)
    result["memory_context"] = {
        "preferred_windows": memory_after.get("preferred_windows") or [],
        "location": memory_after.get("location") or "",
        "dislikes": memory_after.get("dislikes") or [],
        "long_term_count": len(memory_after.get("long_term") or []),
    }
    result["reply"] = _polish_reply(result, decision, text, saved_memories)
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


def _handle_query_memory(db: Session, user: User) -> dict:
    rows = list_memories(db, user)
    long_term = [memory_payload(row) for row in rows if row.kind != SHORT_TERM]
    short_term = [memory_payload(row) for row in rows if row.kind == SHORT_TERM]
    return {
        "reply": (
            f"我目前记得 {len(long_term)} 条长期偏好和 {len(short_term)} 条近期状态。"
            "可以在「我的 → 我的健身记忆」里查看、编辑或删除。"
        ),
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


def _handle_create_plan(
    db: Session, user: User, text: str, decision: Decision, trace: list[dict], client=None,
) -> dict:
    profile = db.get(Profile, user.id)
    if profile is None:
        return {"reply": "先到「我的」页填一下性别、身高、体重，再生成计划。"}

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
        return {"reply": "我没听出要记住什么。可以说「以后训练安排在午休」「我不喜欢深蹲」。"}
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
    reply = "已更新档案和记忆：" + "；".join(changes) + "。"
    reply += "之后的计划会按新档案生成；今天的执行安排不会被这句话自动改掉。"
    actions = [
        {"type": "quick_reply", "label": "按这个重排计划", "text": "按我的长期偏好重新生成计划"},
        {"type": "open_memory", "label": "查看 / 编辑记忆"},
    ]
    return {
        "reply": reply,
        "changes": changes,
        "memory_saved": [memory_payload(row) for row in saved],
        "memories": [memory_payload(row) for row in list_memories(db, user)],
        "actions": actions,
    }


def _handle_adjust_plan(
    db: Session, user: User, text: str, decision: Decision, trace: list[dict]
) -> dict:
    """计划结构变更：复用原有补丁链路（草案 → 安全门 → 确认 → 新版本）。"""
    plan = db.scalar(select(Plan).where(Plan.user_id == user.id, Plan.status == "active"))
    if plan is None:
        return _handle_create_plan(db, user, text, decision, trace)
    try:
        req = patch_requests.create_request(
            db, user, plan.id, text, quick_action="", client=None,
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


def _handle_adjust_today(
    db: Session, user: User, day: dt.date, text: str, decision: Decision, trace: list[dict]
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
    current = effective_day(db, user, day, fallback_next=True)
    if not current.get("actions"):
        return {
            "reply": "现在还没有可执行的训练计划。先说一句目标（比如「减脂，每周 3 次，每次 30 分钟」），我先给你排一份。",
            "actions": [{"type": "quick_reply", "label": "建立计划",
                         "text": "我想减脂，每周训练三次，每次 30 分钟"}],
        }

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
            minutes_remaining=constraints.get("minutes"),
        )
    elif constraints.get("easier") or constraints.get("minutes"):
        payload, changes = workout_service.adjust_session(
            db, user, session.id,
            easier=bool(constraints.get("easier")),
            minutes_remaining=constraints.get("minutes"),
        )
    elif "跳过" in text:
        payload = workout_service.skip_action(db, user, session.id, reason="用户跳过")
        changes = ["跳过当前动作，已完成的组数保留"]
    else:
        # 没给具体条件时的兜底：减轻当前和剩余动作
        payload, changes = workout_service.adjust_session(
            db, user, session.id, easier=True,
        )

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
        status="succeeded" if not result.get("error") else "failed",
        input={"text": text, "page": page, "date": day.isoformat(),
               "thread": thread, "context": page_context or {}},
        output={
            "intent": decision.intent,
            "scope": decision.scope,
            "reply": result.get("reply"),
            "changes": result.get("changes") or [],
        },
        trace=trace,
        latency_ms=0,
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
        updated = last.created_at.isoformat() if last.created_at else ""
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
            "created_at": row.created_at.isoformat() if row.created_at else "",
        }
        for row in reversed(rows)
    ]
