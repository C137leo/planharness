"""TodayAgent：模型自己决定调哪个工具、按什么顺序、要不要多查一次。

和"代码里按顺序调 RAG → 外卖 → 恢复"的区别：
- 代码只负责白名单、步数上限、工具结果格式；
- 顺序由模型决定（先查知识还是先找外卖都允许）；
- 最终答案过一遍确定性校验：引用真实、过敏原排除、链接白名单、红旗阻断。

没有 API Key / 模型失败 / 超过步数时，走**确定性编排**：按合理顺序调同样几个工具，
组装出同结构答案，并明确标记 fallback。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from app.agent.today_schema import TodayAnswer
from app.agent.today_tools import TOOLS, dispatch, links_allowed, load_context
from app.knowledge.service import FOOTER, MEDICAL
from pydantic import ValidationError

from app.models import KnowledgeChunk, MealPlan

MAX_STEPS = 6
DEFAULT_DISCLAIMER = "本内容不构成医疗建议；如身体不适请立即就医。"

SYSTEM_PROMPT = """你是今日活动规划 Agent。你可以按任意顺序调用工具，但必须遵守：

1. 给出训练/健康建议前，先调用 search_knowledge，并引用来源（source_ids）。
2. 推荐餐食前，调用 search_food_options；不要自己编价格或链接。
3. 需要当前计划/营养目标时，调用 get_today_plan / get_nutrition_targets。
4. 用户提到胸痛、晕厥、严重肿胀、怀孕、进食障碍等，先调用 safety_screen；
   blocked=true 时停止规划，只给就医建议。
5. 只推荐，不代下单；只返回平台链接。
6. 不做诊断，不给药物建议。
7. 最终只输出 JSON，不要 Markdown。
8. 必须为今天的每个动作给出「动作要领」tips（调用 search_knowledge 取依据），
   并把依据 id 写进这条 tip 的 source_ids。
9. meals 必须覆盖工具返回的每一餐（早/午/晚），每餐 options 原样复制工具结果的 3 个选项，
   不要省略、不要改写标题和链接；citations 里放上所有被引用条目的 id/title/source_url。

输出结构（**不要输出 meals**，餐食由程序用工具结果拼好）：
{
  "summary": "今天怎么安排（两句话内）",
  "schedule": [{"time": "18:30", "activity": "力量训练 35 分钟", "why": "..."}],
  "tips": [{"text": "...", "source_ids": ["recovery-01"]}],
  "nutrition_notes": [{"text": "...", "source_ids": ["protein-basics"]}]
}
"""


@dataclass
class TodayResult:
    final: dict | None
    trace: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    fallback: bool = False
    error: str = ""
    refused: bool = False


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _step(node: str, status: str, **detail) -> dict:
    """trace 一步：和 /runs 的 TraceEntryOut 对齐（node/status/ts/detail）。"""
    return {"node": node, "status": status, "ts": _now(), "detail": detail}


def _tool_observation(result: dict) -> dict:
    return result


def _parse_answer(content: str) -> dict:
    """JSON + Pydantic 双重校验：先保证是 JSON，再保证字段类型对。"""
    data = _parse_json(content)
    return TodayAnswer.model_validate(data).model_dump()


def _repair(client, messages: list[dict], error: str) -> dict:
    """截断/格式错时让模型只重发一次 JSON（不带工具）。"""
    retry_messages = messages + [
        {"role": "user", "content": (
            "上一条输出不合法（" + error[:120] + "）。只输出符合以下结构的 JSON，"
            "不要解释、不要 Markdown、不要 meals："
            '{"summary":"","schedule":[],"tips":[],"nutrition_notes":[]}'
        )},
    ]
    reply = client.complete(retry_messages, tools=None, max_tokens=1200)
    return _parse_answer((reply.get("message") or {}).get("content") or "")


def _run_loop(db, ctx: dict, text: str, client) -> TodayResult:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(
            {"request": text, "date": str(ctx["day"])}, ensure_ascii=False)},
    ]
    trace: list[dict] = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    food_meals: dict[str, dict] = {}
    tool_citations: dict[str, dict] = {}
    mcd_data: dict | None = None

    for step in range(MAX_STEPS):
        # 最终 JSON 含 3 餐 × 3 选项，1200 token 容易被截断成非法 JSON；
        # 给足预算，仍然失败就降级到确定性编排
        reply = client.complete(messages, tools=TOOLS, max_tokens=3000)
        message = reply.get("message") or {}
        usage = reply.get("usage") or {}
        usage_total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        usage_total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        calls = message.get("tool_calls") or []
        trace.append(_step(
            "agent", "tools" if calls else "final",
            tool_calls=[(call.get("function") or {}).get("name") for call in calls],
        ))
        if not calls:
            content = (message.get("content") or "").strip()
            truncated = reply.get("finish_reason") == "length"
            try:
                if truncated:
                    raise ValueError("output_truncated")
                answer = _parse_answer(content)
            except (ValueError, ValidationError) as exc:
                trace.append(_step("parse", "failed", error=f"{type(exc).__name__}: {str(exc)[:100]}"))
                # 修复一次：只让模型重发文字部分，餐食本来就不在模型输出里
                answer = _repair(client, messages, f"{type(exc).__name__}: {str(exc)[:100]}")
                trace.append(_step("parse", "repaired"))
            final = _assemble(answer, food_meals, tool_citations, mcd_data)
            return TodayResult(final=final, trace=trace, usage=usage_total)

        messages.append({"role": "assistant", "content": message.get("content") or "",
                         "tool_calls": calls})
        blocked = False
        for call in calls:
            name = (call.get("function") or {}).get("name") or ""
            args = _parse_args((call.get("function") or {}).get("arguments"))
            result = _tool_observation(dispatch(name, args, ctx, db))
            trace.append(_step(name, str(result.get("status"))))
            for citation in result.get("citations") or []:
                tool_citations[citation["id"]] = citation
            if name == "search_mcdonalds_menu" and result.get("status") == "ok":
                mcd_data = result.get("data")
            if name == "search_food_options" and result.get("status") == "ok":
                data = result.get("data") or {}
                if data.get("options"):
                    food_meals[data["meal_slot"]] = {
                        "slot": data["meal_slot"],
                        "target_kcal": data.get("target_kcal"),
                        "options": data["options"],
                    }
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or name,
                "content": json.dumps(result, ensure_ascii=False)[:6000],
            })
            if name == "safety_screen" and (result.get("data") or {}).get("blocked"):
                blocked = True
        if blocked:
            return TodayResult(final=_blocked_answer(trace), trace=trace,
                               usage=usage_total, refused=True)

    raise RuntimeError("MAX_STEPS exceeded")


def _assemble(answer: dict, food_meals: dict[str, dict], tool_citations: dict[str, dict],
              mcd_data: dict | None = None) -> dict:
    """模型给文字，代码给餐食与引用：输出小、校验确定。"""
    meals = [food_meals[slot] for slot in ("breakfast", "lunch", "dinner", "snack")
             if slot in food_meals]
    referenced = {
        source_id
        for item in (answer.get("tips") or []) + (answer.get("nutrition_notes") or [])
        for source_id in item.get("source_ids") or []
    }
    citations = [tool_citations[cid] for cid in referenced if cid in tool_citations]
    if not citations:
        citations = list(tool_citations.values())[:3]
    return {
        "summary": answer["summary"],
        "schedule": answer.get("schedule") or [],
        "meals": meals,
        "tips": answer.get("tips") or [],
        "nutrition_notes": answer.get("nutrition_notes") or [],
        "citations": citations,
        "disclaimer": DEFAULT_DISCLAIMER,
        "mcdonalds": mcd_data,
    }


def _blocked_answer(trace: list[dict]) -> dict:
    return {
        "summary": "检测到红旗症状，今天不做训练规划。",
        "schedule": [],
        "meals": [],
        "tips": [{"text": "请先就医评估，等症状明确后再恢复训练。", "source_ids": []}],
        "nutrition_notes": [],
        "citations": [],
        "blocked": True,
        "disclaimer": DEFAULT_DISCLAIMER,
    }


def _parse_json(content: str) -> dict:
    text = content.strip()
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
        raise ValueError("model json must be object")
    return data


def _parse_args(raw) -> dict:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------- 确定性编排

def _deterministic(db, ctx: dict, text: str) -> TodayResult:
    trace: list[dict] = []
    safety = dispatch("safety_screen", {"text": text}, ctx, db)
    trace.append(_step("safety_screen", str(safety.get("status"))))
    if (safety.get("data") or {}).get("blocked"):
        return TodayResult(final=_blocked_answer(trace), trace=trace,
                           fallback=True, refused=True)

    plan = dispatch("get_today_plan", {}, ctx, db)
    trace.append(_step("get_today_plan", str(plan.get("status"))))
    nutrition = dispatch("get_nutrition_targets", {}, ctx, db)
    trace.append(_step("get_nutrition_targets", str(nutrition.get("status"))))

    body_part = "knee" if "膝" in text else ("back" if "腰" in text else None)
    knowledge = dispatch("search_knowledge",
                         {"query": "训练安排 热身 恢复", "body_part": body_part}, ctx, db)
    trace.append(_step("search_knowledge", str(knowledge.get("status"))))

    meals = []
    for slot in [slot for slot in (ctx.get("meals") or {}) if slot != "snack"][:3]:
        food = dispatch("search_food_options", {"meal_slot": slot}, ctx, db)
        trace.append(_step("search_food_options", str(food.get("status")), meal_slot=slot))
        if food.get("status") != "ok":
            continue
        meals.append({
            "slot": slot,
            "target_kcal": food["data"]["target_kcal"],
            "options": food["data"]["options"],
        })

    # 每次规划后额外补一次麦当劳（可直接点餐的替代选项）
    mcd = dispatch("search_mcdonalds_menu", {}, ctx, db)
    trace.append(_step("search_mcdonalds_menu", str(mcd.get("status"))))

    recovery = dispatch("get_recovery_tips", {}, ctx, db)
    trace.append(_step("get_recovery_tips", str(recovery.get("status"))))

    actions = plan.get("data", {}).get("actions", []) if plan.get("status") == "ok" else []
    citations = {item["id"]: item for item in (
            (knowledge.get("citations") or []) + (recovery.get("citations") or []))}

    # 动作要领：逐个动作查知识库，作为 tips 给出来（Agent 没讲时由这里兜底）
    cue_tips: list[dict] = []
    for action in actions[:3]:
        cue = dispatch("search_knowledge",
                       {"query": action.get("name") or action.get("ref") or "动作要领"}, ctx, db)
        trace.append(_step("search_knowledge", str(cue.get("status")), target=action.get("name")))
        for item in cue.get("citations") or []:
            citations[item["id"]] = item
        data = cue.get("data") or []
        if data:
            cue_tips.append({"text": f"{action.get('name')}：{data[0]['content'][:80]}",
                             "source_ids": [data[0]["id"]]})

    final = {
        "summary": (
            f"今天 {len(actions)} 个动作；{len(meals)} 餐各给了 3 个替换选项，"
            "训练前先热身，餐后留出 60-90 分钟再练。"
        ),
        "schedule": [
            {"time": "18:30", "activity": f"{len(actions)} 个动作，约 35 分钟",
             "why": "按当前计划版本执行"},
        ],
        "meals": meals,
        "tips": cue_tips + (recovery.get("data") or {}).get("tips", []),
        "nutrition_notes": [
            {"text": f"今日热量目标 {nutrition.get('data', {}).get('target_kcal', 0)} kcal，"
                     f"蛋白 {nutrition.get('data', {}).get('protein_g', 0)}g。",
             "source_ids": []},
        ],
        "citations": list(citations.values()),
        "disclaimer": DEFAULT_DISCLAIMER,
        "fallback": True,
    }
    if mcd.get("status") == "ok":
        final["mcdonalds"] = mcd.get("data")
    return TodayResult(final=final, trace=trace, fallback=True)


# ---------------------------------------------------------------- 校验

def validate(final: dict, ctx: dict, db) -> list[str]:
    """确定性校验：不通过就降级，不让"看起来像答案"的东西直接给用户。"""
    problems: list[str] = []
    if not isinstance(final, dict):
        return ["final 不是对象"]

    if final.get("blocked"):
        return problems

    meals = final.get("meals") or []
    if not meals:
        problems.append("缺少餐食推荐")
    for meal in meals:
        options = meal.get("options") or []
        if len(options) != 3:
            problems.append(f"{meal.get('slot')} 选项数不是 3")
        for option in options:
            if not links_allowed(option.get("links") or {}):
                problems.append(f"{meal.get('slot')} 链接不在白名单")

    excluded = {str(item) for item in (ctx.get("allergies") or [])}
    for meal in meals:
        for option in meal.get("options") or []:
            for item in option.get("items") or []:
                if item.get("name") in excluded:
                    problems.append(f"出现忌口食材 {item.get('name')}")
            # 模型输出的选项可能不带 items，用标题兜底检查
            title = str(option.get("title") or "")
            for term in excluded:
                if term and term in title:
                    problems.append(f"选项标题出现忌口 {term}")

    citation_ids = {item.get("id") for item in final.get("citations") or []}
    if citation_ids:
        existing = {row.id for row in db.query(KnowledgeChunk).filter(
            KnowledgeChunk.id.in_(citation_ids)).all()}
        missing = citation_ids - existing
        if missing:
            problems.append(f"引用不存在：{sorted(missing)}")

    for tip in (final.get("tips") or []) + (final.get("nutrition_notes") or []):
        for source_id in tip.get("source_ids") or []:
            if source_id and source_id not in (citation_ids | _all_knowledge_ids(db)):
                problems.append(f"tip 引用不存在：{source_id}")

    if any(word in json.dumps(final, ensure_ascii=False) for word in MEDICAL):
        problems.append("输出包含用药/诊断建议")
    return problems


def _all_knowledge_ids(db) -> set[str]:
    return {row.id for row in db.query(KnowledgeChunk.id).all()}


# ---------------------------------------------------------------- 入口

def run(db, user, day, text: str, client=None) -> TodayResult:
    ctx = load_context(db, user, day)
    # 手机比价工具需要 VLM 客户端（同一个 OpenAI 兼容客户端即可）
    ctx["vlm_client"] = client
    try:
        result = _run_loop(db, ctx, text, client) if client else _deterministic(db, ctx, text)
    except Exception as exc:  # noqa: BLE001 - 模型失败要能降级
        result = _deterministic(db, ctx, text)
        result.error = f"{type(exc).__name__}: {str(exc)[:120]}"

    problems = validate(result.final or {}, ctx, db)
    if problems:
        fallback = _deterministic(db, ctx, text)
        fallback.trace = result.trace + [_step("validate", "failed", problems=problems[:5])]
        fallback.error = "; ".join(problems[:3])
        return fallback
    result.trace = result.trace + [_step("validate", "ok")]
    return result
