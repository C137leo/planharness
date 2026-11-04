"""计划编排 Agent：模型调用 skill 的工具，产出「今日训练 + 食谱 + 推荐摄入」。

分工（这是整个 harness 的核心主张）：

- **skill 负责算**：训练结构、动作选择、RIR、营养目标、配餐、过敏排除，全部由
  healthy-fitness-coach 的确定性引擎给出，模型不改这些数字。
- **模型负责编排与解释**：按 skill 的入口工作流决定调用顺序，读工具结果，
  产出给用户看的摘要与风险提示。
- **本地安全门负责否决**：模型给出的任何计划，提交前还要过一遍确定性校验
  （过敏原、训练日上限、目标热量区间、红旗症状）。

没有 API Key、超时、或模型返回不可解析时，走**确定性编排**：按同样顺序直接调用同样
的工具，产出同样结构的结果，只是摘要由模板拼。两条路径共用同一套安全门与落库逻辑，
所以"模型在不在"不会改变计划是否安全。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.domain.allergens import describe_tags, resolve_tags
from app.skill.bridge import SkillBridge

MAX_STEPS = 6
MAX_TOOL_CALLS = 8

# 这些工具允许模型直接调用；其余（例如读任意文件）不暴露
ALLOWED_TOOLS = {
    "coach_safety_gate",
    "coach_plan_program",
    "coach_today_session",
    "coach_nutrition_targets",
    "coach_review_week",
    "coach_read_reference",
}

OUTPUT_CONTRACT = """你要为学生产出一份「先给他看、再让他确认」的计划草案。

必须遵守：
1. 先调 coach_safety_gate 判断原话里有没有红旗症状；blocked=true 时**停止**，只做安全分流。
2. 再调 coach_plan_program 拿训练结构；缺字段就如实说明缺什么，不要自己编。
3. 再调 coach_nutrition_targets（需要 sex/age/height_cm/weight_kg）算摄入目标。
4. 只允许调用给你列出的工具；不要编造工具返回里没有的数字。
5. 最后一条消息**只输出 JSON**（不要 Markdown 代码围栏），格式：
{
  "status": "ready" | "blocked" | "needs_input",
  "summary": "两句话以内，说明这份计划怎么安排、为什么这么排",
  "highlights": ["最多 3 条，用户最该注意的事"],
  "cautions": ["最多 3 条风险或前提，没有就空数组"],
  "question": "status 不是 ready 时，必须问用户的那一句话"
}
"""


@dataclass
class PlanProposal:
    """一次规划的结果，既给接口返回也算落库依据。"""

    status: str
    payload: dict[str, Any]
    trace: list[dict] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    fallback: bool = False


def skill_profile(profile: Any) -> dict:
    """把数据库画像翻译成 skill 认的字段名（athlete-state.js 白名单）。

    goal/skill 用的是中文枚举，这里做一次显式映射，不做模糊匹配——
    映射不上就退到"综合健康"，并在 payload 里标出来。
    """
    goal_map = {
        "fat_loss": "减脂",
        "muscle_gain": "增肌",
        "cardio": "心肺",
        "health": "综合健康",
    }
    equipment_names = {
        "dumbbell": "哑铃",
        "band": "弹力带",
        "bench": "训练凳",
        "treadmill": "跑步机",
        "barbell": "杠铃",
        "mat": "瑜伽垫",
    }
    equipment = [equipment_names.get(item, item) for item in (getattr(profile, "equipment", None) or [])]
    if not equipment:
        equipment = ["瑜伽垫"]

    goal = goal_map.get(getattr(profile, "goal", "") or "", "综合健康")
    return {
        "goal": goal,
        "experience_level": getattr(profile, "training_experience", "beginner") or "beginner",
        "training_days_per_week": int(getattr(profile, "weekly_days", 3) or 3),
        "available_time_min": int(getattr(profile, "session_minutes", 40) or 40),
        "training_venue": "home" if "瑜伽垫" in equipment or "哑铃" in equipment else "gym",
        "available_equipment": equipment,
        "injury_or_medical_constraints": (getattr(profile, "goal_note", "") or "").strip() or "无",
        "long_term_preferences": [str(item) for item in (getattr(profile, "dislikes", None) or [])],
        # 营养层需要体型字段与饮食偏好
        "sex": getattr(profile, "sex", "male") or "male",
        "age": _age(profile),
        "height_cm": float(getattr(profile, "height_cm", 178) or 178),
        "weight_kg": float(getattr(profile, "weight_kg", 78) or 78),
        "allergies": [str(item) for item in (getattr(profile, "allergies", None) or [])],
        "diet_preferences": [str(item) for item in (getattr(profile, "diet_preferences", None) or [])],
    }


def _age(profile: Any) -> int:
    birth_year = int(getattr(profile, "birth_year", 1997) or 1997)
    return max(14, min(80, time.gmtime().tm_year - birth_year))


class PlanningAgent:
    """一次规划 = 一次模型编排（或确定性编排）+ 一套确定性校验。"""

    def __init__(self, bridge: SkillBridge, client=None) -> None:
        self.bridge = bridge
        self.client = client

    # ---- 对外入口 ----

    def propose(self, profile: Any, instruction: str, *, previous_feedback: list[str] | None = None) -> PlanProposal:
        profile_payload = skill_profile(profile)
        feedback = [text for text in (previous_feedback or []) if text]
        if feedback:
            instruction = instruction + "\n用户上一轮的修改意见：" + "；".join(feedback)

        if self.client is not None and getattr(self.client, "available", lambda: False)():
            try:
                return self._llm_flow(profile_payload, instruction)
            except Exception as exc:  # noqa: BLE001 - 模型失败必须能退回确定性编排
                proposal = self._deterministic_flow(profile_payload, instruction)
                proposal.error = f"{type(exc).__name__}: {str(exc)[:160]}"
                proposal.fallback = True
                proposal.payload.setdefault("meta", {})["fallback_reason"] = proposal.error
                return proposal
        return self._deterministic_flow(profile_payload, instruction)

    def propose_deterministic(self, profile: Any, instruction: str) -> PlanProposal:
        """不走模型，直接按 skill 的顺序跑一遍（用于"这个我们自己能做，别问用户"）。"""
        return self._deterministic_flow(skill_profile(profile), instruction)

    # ---- 模型编排 ----

    def _llm_flow(self, profile_payload: dict, instruction: str) -> PlanProposal:
        tools = [tool for tool in self.bridge.tools()
                 if (tool.get("function") or {}).get("name") in ALLOWED_TOOLS]
        if not tools:
            raise RuntimeError("skill returned no tools")

        messages: list[dict] = [
            {"role": "system", "content": self._system_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {"instruction": instruction, "profile": profile_payload},
                    ensure_ascii=False,
                ),
            },
        ]
        trace: list[dict] = []
        tool_results: dict[str, Any] = {}
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
        started = time.perf_counter()

        for step in range(MAX_STEPS):
            reply = self.client.complete(messages, tools=tools)
            message = reply["message"]
            usage = reply.get("usage") or {}
            usage_total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            usage_total["completion_tokens"] += int(usage.get("completion_tokens") or 0)

            calls = message.get("tool_calls") or []
            trace.append({
                "step": step + 1,
                "node": "model",
                "status": "tool_calls" if calls else "final",
                "detail": {
                    "tool_calls": [((c.get("function") or {}).get("name")) for c in calls],
                    "latency_ms": reply.get("latency_ms"),
                },
            })

            if not calls:
                content = (message.get("content") or "").strip()
                parsed = _parse_contract(content)
                return self._assemble(
                    profile_payload, instruction, parsed, tool_results, trace,
                    usage_total, stripped=content, fallback=False,
                )

            if step == MAX_STEPS - 1:
                raise RuntimeError("tool loop exceeded MAX_STEPS")

            messages.append({"role": "assistant", "content": message.get("content") or "",
                             "tool_calls": calls})
            for call in calls:
                name = (call.get("function") or {}).get("name") or ""
                raw_args = (call.get("function") or {}).get("arguments") or "{}"
                args = _safe_json(raw_args)
                result = self._dispatch(name, args, profile_payload)
                tool_results[name] = result
                trace.append({
                    "node": name,
                    "status": str(result.get("status") or "ok"),
                    "detail": {"args": _redact(args)},
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or name,
                    "content": json.dumps(result, ensure_ascii=False)[:6000],
                })
                if name == "coach_safety_gate" and result.get("blocked"):
                    # 红旗：立刻停，不再让模型继续编排
                    return self._assemble(
                        profile_payload, instruction,
                        {"status": "blocked", "summary": "检测到红旗症状，本次不生成训练计划。",
                         "highlights": [], "cautions": [str(result.get("required_action") or "")],
                         "question": str(result.get("required_action") or "建议先线下评估。")},
                        tool_results, trace, usage_total, stripped="", fallback=False,
                    )

        raise RuntimeError("tool loop ended without final message")

    def _system_prompt(self) -> str:
        skill_md = self.bridge.skill_md()
        # 入口说明很长，只取前 2400 字：模型需要的是工作流与边界，不是全部参考材料
        excerpt = skill_md[:2400]
        return (
            "你是 PlanHarness 的计划编排器，背后挂了 healthy-fitness-coach skill 的工具。\n"
            "你只做编排与解释，所有训练/营养数字都来自工具返回，不许自己编。\n\n"
            "== skill 入口说明（节选） ==\n" + excerpt + "\n\n== 输出契约 ==\n" + OUTPUT_CONTRACT
        )

    def _dispatch(self, name: str, args: dict, profile_payload: dict) -> dict:
        if name not in ALLOWED_TOOLS:
            return {"status": "error", "error": f"tool_not_allowed:{name}"}
        try:
            if name == "coach_safety_gate":
                return self.bridge.safety_gate(
                    str(args.get("instruction") or ""),
                    args.get("profile") or profile_payload,
                    args.get("currentState") or {},
                )
            if name == "coach_plan_program":
                return self.bridge.plan_program(args.get("profile") or profile_payload,
                                                str(args.get("instruction") or ""))
            if name == "coach_today_session":
                return self.bridge.today_session(
                    args.get("profile") or profile_payload,
                    args.get("program") or {},
                    args.get("currentState") or {},
                    args.get("sessionId"),
                    str(args.get("instruction") or ""),
                )
            if name == "coach_nutrition_targets":
                return self.bridge.nutrition_targets(
                    args.get("profile") or profile_payload,
                    args.get("body") or _body(profile_payload),
                    str(args.get("mode") or "simplified"),
                )
            if name == "coach_read_reference":
                return self.bridge.read_reference(str(args.get("name") or ""))
            if name == "coach_review_week":
                return self.bridge.review_week(list(args.get("records") or []))
        except Exception as exc:  # noqa: BLE001 - 单个工具失败要回给模型，而不是炸掉整轮
            return {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        return {"status": "error", "error": f"unknown_tool:{name}"}

    # ---- 确定性编排（无模型 / 模型失败） ----

    def _deterministic_flow(self, profile_payload: dict, instruction: str) -> PlanProposal:
        trace: list[dict] = []
        tool_results: dict[str, Any] = {}

        gate = self.bridge.safety_gate(instruction or "生成训练与饮食计划", profile_payload)
        tool_results["coach_safety_gate"] = gate
        trace.append({"node": "coach_safety_gate", "status": str(gate.get("status")), "detail": {}})

        if gate.get("blocked"):
            return self._assemble(
                profile_payload, instruction,
                {"status": "blocked",
                 "summary": "检测到红旗症状，本次不生成训练计划。",
                 "highlights": [],
                 "cautions": [str(gate.get("required_action") or "")],
                 "question": str(gate.get("required_action") or "建议先线下评估。")},
                tool_results, trace, {}, stripped="", fallback=True,
            )

        program = self.bridge.plan_program(profile_payload, instruction)
        tool_results["coach_plan_program"] = program
        trace.append({"node": "coach_plan_program", "status": str(program.get("status")), "detail": {}})

        targets = self.bridge.nutrition_targets(profile_payload, _body(profile_payload))
        tool_results["coach_nutrition_targets"] = targets
        trace.append({"node": "coach_nutrition_targets", "status": str(targets.get("status")), "detail": {}})

        screen = self.bridge.screen_profile(profile_payload, instruction=instruction)
        tool_results["screen_profile"] = screen
        trace.append({"node": "screen_profile", "status": str(screen.get("level")), "detail": {}})

        parsed = {"status": "ready", "summary": "", "highlights": [], "cautions": [], "question": ""}
        return self._assemble(profile_payload, instruction, parsed, tool_results, trace, {},
                              stripped="", fallback=True)

    # ---- 组装：把工具结果变成落库用的 payload，并跑本地安全门 ----

    def _assemble(self, profile_payload: dict, instruction: str, parsed: dict,
                  tool_results: dict[str, Any], trace: list[dict], usage: dict,
                  *, stripped: str, fallback: bool) -> PlanProposal:
        program_result = tool_results.get("coach_plan_program") or {}
        targets = tool_results.get("coach_nutrition_targets") or {}
        screen = tool_results.get("screen_profile") or {}
        gate = tool_results.get("coach_safety_gate") or {}

        status = str(parsed.get("status") or "ready")
        if gate.get("blocked") or program_result.get("status") == "blocked":
            status = "blocked"
        if program_result.get("status") == "needs_input" or targets.get("status") == "needs_input":
            status = "needs_input"

        payload: dict[str, Any] = {
            "status": status,
            "summary": str(parsed.get("summary") or ""),
            "highlights": [str(item) for item in (parsed.get("highlights") or [])][:3],
            "cautions": [str(item) for item in (parsed.get("cautions") or [])][:3],
            "question": str(parsed.get("question") or ""),
            "program": (program_result.get("program") or {}),
            "prescription": (program_result.get("program") or {}).get("prescription") or {},
            "nutrition": targets if targets.get("status") == "ready" else {},
            "screen": {
                "level": screen.get("level"),
                "must_avoid": screen.get("must_avoid") or [],
                "diet": (screen.get("diet") or {}),
                "reasons": screen.get("reasons") or [],
            },
            "days": [],
            "meals": [],
            "warnings": [],
            "instruction": instruction,
            "meta": {
                "skill_available": self.bridge.available,
                "skill_version": self.bridge.meta().get("skill_version", ""),
                "tools_called": [entry.get("node") for entry in trace if entry.get("node") != "model"],
                "model_fallback": fallback,
                "usage": usage,
                "cost_cny": _cost_cny(usage),
                "model_output_chars": len(stripped),
            },
        }

        if status != "ready":
            if not payload["question"]:
                missing = program_result.get("missing_fields") or targets.get("missing_fields") or []
                payload["question"] = (
                    "还需要这些信息才能生成：" + "、".join(missing)
                    if missing else "这次没能生成计划，请补充一句你的目标或身体情况。"
                )
            return PlanProposal(status=status, payload=payload, trace=trace, usage=usage,
                                fallback=fallback)

        payload["days"] = _build_days(program_result.get("program") or {})
        payload["meals"] = _build_meals(profile_payload, payload["nutrition"])
        payload["warnings"] = _local_gate(profile_payload, payload)

        if payload["warnings"]:
            # 本地安全门发现硬问题：不提交，直接要求改
            status = "invalid"
            payload["status"] = "invalid"
            payload["question"] = "；".join(payload["warnings"])

        if not payload["summary"]:
            payload["summary"] = _template_summary(payload)

        return PlanProposal(status=payload["status"], payload=payload, trace=trace,
                            usage=usage, fallback=fallback)


# 默认价格（元 / 百万 token），与 LlmBudget 的默认值保持一致
PRICE_IN_CNY_PER_MTOK = 2.0
PRICE_OUT_CNY_PER_MTOK = 8.0


def _cost_cny(usage: dict) -> float:
    """按服务端返回的 token 数算成本；没有 usage 就是 0，不估。"""
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    return round(
        tokens_in / 1_000_000 * PRICE_IN_CNY_PER_MTOK
        + tokens_out / 1_000_000 * PRICE_OUT_CNY_PER_MTOK,
        6,
    )


def _today_weekday() -> int:
    import datetime

    return datetime.date.today().isoweekday()


def _body(profile_payload: dict) -> dict:
    return {
        "sex": profile_payload.get("sex", "male"),
        "age": int(profile_payload.get("age", 30)),
        "height_cm": float(profile_payload.get("height_cm", 175)),
        "weight_kg": float(profile_payload.get("weight_kg", 75)),
    }


def _build_days(program: dict, start_weekday: int | None = None) -> list[dict]:
    """把 skill 的 sessions 摊平成"第几天做什么"。

    训练日从**今天**开始排：以前固定从周一开始，用户周四生成计划，
    当天就会看到"今天休息"，很容易误以为生成没生效。
    从今天起连续排，既符合"现在开始练"的直觉，也保持每周同样几天。
    """
    start = start_weekday or _today_weekday()
    days: list[dict] = []
    used_slugs: set[str] = set()
    for offset, session in enumerate(program.get("sessions") or []):
        day_index = ((start - 1 + offset) % 7) + 1
        focus = str(session.get("focus") or "")
        slot = "cardio" if "cardio" in focus.lower() else "strength"
        actions = []
        for order, exercise in enumerate(session.get("exercises") or [], start=1):
            if not exercise:
                continue
            # 第一场保留 skill 原本的编排，后面几场换同肌群的不同动作，
            # 避免"每次打开都一模一样"，也不至于一上来就偏离骨架。
            varied = exercise if offset == 0 else _varied_exercise(exercise, used_slugs)
            used_slugs.add(str(varied.get("slug") or ""))
            actions.append({
                "slot": slot,
                "seq": order,
                "action_type": "exercise",
                "action_ref": varied.get("slug") or "unknown",
                "sets": 3,
                "reps": None,
                "duration_min": 12 if slot == "strength" else 30,
                "meta": {
                    "name_zh": varied.get("name_zh"),
                    "name_en": varied.get("name_en"),
                    "movement_mode": varied.get("movement_mode") or exercise.get("movement_mode"),
                    "equipment": varied.get("equipment") or exercise.get("equipment"),
                    "primary_muscle": varied.get("primary_muscle") or exercise.get("primary_muscle"),
                    "degraded_from": varied.get("degraded_from"),
                },
            })
        days.append({
            "day_index": day_index,
            "session_id": session.get("session_id"),
            "focus": focus,
            "minimum_version": session.get("minimum_version"),
            "actions": actions,
        })
    return days


MEAL_TEMPLATES = [
    # slot, 标题, 热量占比, 蛋白源偏好, 主食偏好, 蔬菜偏好
    ("breakfast", "早餐", 0.25, ["鸡蛋", "希腊酸奶"], ["燕麦", "红薯"], []),
    ("lunch", "午餐", 0.35, ["鸡胸肉", "三文鱼", "虾", "瘦牛肉"], ["米饭", "红薯"], ["西兰花"]),
    ("dinner", "晚餐", 0.30, ["北豆腐", "虾", "三文鱼", "瘦牛肉", "鸡胸肉"], ["红薯", "米饭"], ["菠菜"]),
    ("snack", "加餐", 0.10, ["希腊酸奶", "鸡蛋"], [], ["苹果"]),
]

# 同一天里优先用没出现过的蛋白源，避免"早中晚都是鸡胸肉"
PROTEIN_ORDER = ["鸡蛋", "希腊酸奶", "鸡胸肉", "北豆腐", "瘦牛肉", "三文鱼", "虾"]


def _varied_exercise(exercise: dict, used: set[str]) -> dict:
    """按场次轮换同肌群动作：A/B 两天不该是同一串动作。

    只在 skill 给的模式内换（膝主导/髋主导/水平推），结构和安全边界都不变。
    """
    from app.domain.catalog import name_of
    from app.domain.exercise_catalog import EXERCISE_CATALOG
    from app.domain.revisions import MODE_ALTERNATIVES

    mode = str(exercise.get("movement_mode") or "")
    options = MODE_ALTERNATIVES.get(mode) or []
    if not options:
        return exercise
    # 第 n 场用第 n 个候选，循环使用；同时保证与 skill 原始动作不同
    original = str(exercise.get("slug") or "")
    # 挑一个"不是原动作、也没在本计划里出现过"的候选，全用过才回退
    candidates = [item for item in options if item != original and item not in used]
    if not candidates:
        candidates = [item for item in options if item != original] or options
    candidate = candidates[0]
    item = EXERCISE_CATALOG.get(candidate) or {}
    if not item:
        return exercise
    return {
        "slug": candidate,
        "name_zh": name_of(candidate),
        "name_en": item.get("name_en"),
        "equipment": item.get("equipment"),
        "primary_muscle": item.get("primary_muscle"),
        "degraded_from": None,
        "movement_mode": mode,
    }


def _build_meals(profile_payload: dict, nutrition: dict) -> list[dict]:
    """营养目标 → 7 天餐食（按天轮换，避免每天吃一样）。

    口径：热量与蛋白按餐次占比拆分（25/35/30/10），蛋白源优先选该餐的常见项，
    克数按"蛋白目标 / 每 100g 蛋白含量"算，主食按剩余热量补。
    数值都是估算，返回里带 note 说明，不假装精确。
    """
    target_kcal = int(nutrition.get("target_kcal") or 0)
    macros = nutrition.get("macros") or {}
    protein_band = macros.get("protein_g") or {}
    protein_target = int(protein_band.get("high_g") or protein_band.get("low_g") or 0)
    if target_kcal <= 0:
        return []

    # 过敏原用关键词匹配（"海鲜过敏""甲壳类"都要能认出来），不再精确查字典
    terms = list(profile_payload.get("allergies") or []) + list(
        profile_payload.get("diet_preferences") or []
    )
    avoid, _unmatched = resolve_tags(terms)
    # 像「不吃红薯」这种具体食材，只靠标签过滤不掉，需要按名字排除
    excluded_names = {term for term in map(str, terms) if term in {f["name"] for f in FOODS}}

    # 加餐只给增肌：减脂/心肺目标是控制总热量，多一顿加餐反而更难执行
    goal_text = str(profile_payload.get("goal") or "")
    with_snack = "增肌" in goal_text
    templates = [t for t in MEAL_TEMPLATES if with_snack or t[0] != "snack"]
    if with_snack:
        shares = {slot: share for slot, _title, share, *_rest in MEAL_TEMPLATES}
    else:
        # 没有加餐时把 10% 摊回正餐，避免总热量凭空少一截
        shares = {"breakfast": 0.30, "lunch": 0.40, "dinner": 0.30}

    meals: list[dict] = []
    for day_index in range(1, 8):
        # 这一天已经用过的蛋白源：同一天内尽量不重复
        used_today: set[str] = set()
        for slot, title, _share, protein_pref, carb_pref, veg_pref in templates:
            share = shares.get(slot, _share)
            meal_kcal = round(target_kcal * share)
            meal_protein = round(protein_target * share)
            # 按天轮换偏好顺序：第 1 天用第一项，第 2 天用第二项……
            def rotate(names: list[str]) -> list[str]:
                if not names:
                    return []
                shift = (day_index - 1) % len(names)
                return names[shift:] + names[:shift]

            items = _pick_items(
                meal_kcal, meal_protein, avoid,
                protein_pref=rotate(protein_pref),
                carb_pref=rotate(carb_pref),
                veg_pref=rotate(veg_pref),
                used_today=used_today,
                excluded=excluded_names,
            )
            if not items:
                continue
            for item in items:
                if item["name"] in PROTEIN_ORDER:
                    used_today.add(item["name"])
            meals.append({
                "day_index": day_index,
                "slot": slot,
                "title": title,
                "kcal": sum(item["kcal"] for item in items),
                "protein_g": round(sum(item["protein"] for item in items), 1),
                "fat_g": round(sum(item["fat"] for item in items), 1),
                "carb_g": round(sum(item["carb"] for item in items), 1),
                "items": items,
            })
    return meals


# 本地食物库（每 100g 的近似值）。和 skill 的 web/lib/foods.js 同源口径，
# 但只保留少量常用项：这里的目标是"能算、能复算"，不是做营养数据库。
FOODS = [
    {"name": "鸡胸肉", "group": "protein", "kcal": 165, "protein": 31, "fat": 3.6, "carb": 0,
     "tags": {"meat"}},
    {"name": "鸡蛋", "group": "protein", "kcal": 143, "protein": 13, "fat": 9.5, "carb": 1.1,
     "tags": {"egg"}},
    {"name": "北豆腐", "group": "protein", "kcal": 76, "protein": 8, "fat": 4.8, "carb": 1.9,
     "tags": {"soy"}},
    {"name": "三文鱼", "group": "protein", "kcal": 208, "protein": 20, "fat": 13, "carb": 0,
     "tags": {"fish", "seafood"}},
    {"name": "虾", "group": "protein", "kcal": 99, "protein": 24, "fat": 0.3, "carb": 0,
     "tags": {"seafood"}},
    {"name": "希腊酸奶", "group": "protein", "kcal": 59, "protein": 10, "fat": 0.4, "carb": 3.6,
     "tags": {"dairy"}},
    {"name": "瘦牛肉", "group": "protein", "kcal": 250, "protein": 26, "fat": 15, "carb": 0,
     "tags": {"meat", "beef"}},
    {"name": "米饭", "group": "carb", "kcal": 130, "protein": 2.7, "fat": 0.3, "carb": 28,
     "tags": set()},
    {"name": "红薯", "group": "carb", "kcal": 86, "protein": 1.6, "fat": 0.1, "carb": 20,
     "tags": set()},
    {"name": "土豆", "group": "carb", "kcal": 77, "protein": 2.0, "fat": 0.1, "carb": 17,
     "tags": set()},
    {"name": "玉米", "group": "carb", "kcal": 96, "protein": 3.4, "fat": 1.5, "carb": 21,
     "tags": set()},
    {"name": "燕麦", "group": "carb", "kcal": 389, "protein": 16.9, "fat": 6.9, "carb": 66,
     "tags": {"gluten"}},
    {"name": "西兰花", "group": "veg", "kcal": 34, "protein": 2.8, "fat": 0.4, "carb": 7,
     "tags": set()},
    {"name": "菠菜", "group": "veg", "kcal": 23, "protein": 2.9, "fat": 0.4, "carb": 3.6,
     "tags": set()},
    {"name": "苹果", "group": "fruit", "kcal": 52, "protein": 0.3, "fat": 0.2, "carb": 14,
     "tags": set()},
]


def _pick_items(meal_kcal: int, meal_protein: int, avoid: set[str], *,
                protein_pref: list[str] | None = None,
                carb_pref: list[str] | None = None,
                veg_pref: list[str] | None = None,
                used_today: set[str] | None = None,
                excluded: set[str] | None = None) -> list[dict]:
    """按"蛋白源 → 蔬菜 → 主食"的顺序凑这一餐，避开过敏/忌口。

    `used_today` 是这个训练日已经用过的蛋白源：优先挑没用过的，
    否则一天三顿会都变成同一种肉。
    """
    excluded = excluded or set()
    allowed = [food for food in FOODS if not (food["tags"] & avoid) and food["name"] not in excluded]
    used = used_today or set()

    def choose(group: str, preferred: list[str] | None, *, skip_used: bool = False):
        candidates = [name for name in (preferred or []) if not (skip_used and name in used)]
        for name in candidates:
            found = next((food for food in allowed if food["name"] == name), None)
            if found is not None:
                return found
        if skip_used:
            # 偏好里全是今天用过的：换一个同组、今天没用过的
            fresh = [food for food in allowed if food["group"] == group and food["name"] not in used]
            if fresh:
                return fresh[0]
        return next((food for food in allowed if food["group"] == group), None)

    items: list[dict] = []
    protein = choose("protein", protein_pref, skip_used=True)
    if protein is not None and meal_protein > 0:
        grams = max(50, min(250, round((meal_protein / protein["protein"]) * 100 / 5) * 5))
        items.append(_item(protein, grams))

    veg = choose("veg", veg_pref)
    if veg is not None:
        items.append(_item(veg, 150))

    used = sum(item["kcal"] for item in items)
    # 加餐没给主食偏好时不要硬塞米饭/红薯——那看起来就像"零食吃了一碗饭"
    carb = choose("carb", carb_pref) if carb_pref else None
    if carb is not None and meal_kcal - used > 60:
        grams = max(20, min(300, round(((meal_kcal - used) / carb["kcal"]) * 100 / 5) * 5))
        items.append(_item(carb, grams))
    return items


def _item(food: dict, grams: int) -> dict:
    ratio = grams / 100
    return {
        "name": food["name"],
        "grams": grams,
        "kcal": round(food["kcal"] * ratio),
        "protein": round(food["protein"] * ratio, 1),
        "fat": round(food["fat"] * ratio, 1),
        "carb": round(food["carb"] * ratio, 1),
    }


def _local_gate(profile_payload: dict, payload: dict) -> list[str]:
    """本地确定性安全门（第三道）：即使模型/skill 都放行，这里还要再验一次。"""
    problems: list[str] = []

    # 1) 训练日不能超过用户说的天数
    days = payload.get("days") or []
    weekly = int(profile_payload.get("training_days_per_week") or 0)
    if weekly and len(days) > weekly:
        problems.append(f"训练日 {len(days)} 天，超过你填的每周 {weekly} 天")

    # 2) 过敏原/忌口不能出现在食谱里（与生成时用同一套解析，避免两处口径不一致）
    avoid, unmatched = resolve_tags(
        list(profile_payload.get("allergies") or [])
        + list(profile_payload.get("diet_preferences") or [])
    )
    if unmatched:
        # 识别不了就不假装安全：提示人工确认，而不是当没填
        payload.setdefault("cautions", []).append(
            "这些忌口不在食谱库里，暂时无法自动排除，请人工确认：" + "、".join(unmatched)
        )
    explicit_avoid = {
        str(term)
        for term in list(profile_payload.get("allergies") or [])
        + list(profile_payload.get("diet_preferences") or [])
    }
    for meal in payload.get("meals") or []:
        for food in meal.get("items") or []:
            if food.get("name") in explicit_avoid:
                problems.append(f"{meal['title']} 里有你说不吃的「{food['name']}」")
    for meal in payload.get("meals") or []:
        for food in meal.get("items") or []:
            tags = next((f["tags"] for f in FOODS if f["name"] == food["name"]), set())
            hit = tags & avoid
            if hit:
                problems.append(f"{meal['title']} 里的「{food['name']}」与你的忌口冲突：{'、'.join(sorted(hit))}")

    # 3) 目标热量必须落在 skill 自己给的区间内（防止模型改数字）
    nutrition = payload.get("nutrition") or {}
    target = int(nutrition.get("target_kcal") or 0)
    bmr = int(nutrition.get("bmr") or 0)
    if target and bmr and target < bmr * 0.8:
        problems.append(f"目标热量 {target} kcal 低于基础代谢 {bmr} 的 80%，不安全")
    if target and target > 4200:
        problems.append(f"目标热量 {target} kcal 超出常见范围，请检查身体数据")

    # 4) 红旗症状不能出现在训练计划里（有的话就该 blocked）
    screen = payload.get("screen") or {}
    if screen.get("level") == "stop":
        problems.append("安全筛查为 stop，不能生成训练计划")

    return problems


def _template_summary(payload: dict) -> str:
    program = payload.get("program") or {}
    nutrition = payload.get("nutrition") or {}
    days = len(payload.get("days") or [])
    minutes = program.get("session_minutes")
    parts = [f"每周 {days} 天"]
    if minutes:
        parts.append(f"每次约 {minutes} 分钟")
    if program.get("goal"):
        parts.append(f"目标 {program['goal']}")
    head = "，".join(parts) + "。"
    if nutrition.get("target_kcal"):
        head += f" 每天约 {int(nutrition['target_kcal'])} kcal，" \
                f"蛋白 {int((nutrition.get('macros') or {}).get('protein_g', {}).get('high_g', 0))} g 起。"
    return head + " 先按这版练两周，用体感和体重趋势再调。"


def _parse_contract(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        left, right = text.find("{"), text.rfind("}")
        if left == -1 or right <= left:
            raise ValueError(f"model did not return json: {text[:120]!r}")
        data = json.loads(text[left:right + 1])
    if not isinstance(data, dict):
        raise ValueError("model json must be an object")
    return data


def _safe_json(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _redact(args: dict) -> dict:
    """trace 里只留参数名与长度，不把整段画像写进日志。"""
    return {key: (f"<{len(str(value))} chars>" if isinstance(value, (dict, list)) else value)
            for key, value in (args or {}).items()}
