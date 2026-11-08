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
10. 用户提到麦当劳、或问"能不能点外卖"时，调用 mcd_nutrition，用官方营养表的热量/蛋白质
    数字回答；那是营养数据，不是价格，不要编价格。
11. 用户要求"分几次训练 / 早上几点 / 晚上几点 / 几点到几点"时，schedule 必须按用户给的
    时间拆成多个时段，不要在没被要求时固定写 18:30；用户没指定时间才用默认安排。

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


def _push(trace: list[dict], on_step, node: str, status: str, **detail) -> None:
    """记一步，并立刻通知外面（前端靠它边跑边看）。

    没有这个回调，/runs/{id} 要等整轮跑完才有 trace，用户只能干等十几秒。
    on_step 抛异常不打断规划：观测失败不该毁掉这次结果。
    """
    step = _step(node, status, **detail)
    trace.append(step)
    if on_step is None:
        return
    try:
        on_step(step)
    except Exception:  # noqa: BLE001 - 埋点失败不影响主流程
        pass


def _tool_observation(result: dict) -> dict:
    return result


def _brief(result: dict) -> dict:
    """工具结果的"摘要"，给 trace 看：不要整段数据，只留能看懂这一行信息的部分。"""
    data = result.get("data")
    detail: dict = {"error": result.get("error")} if result.get("error") else {}
    if isinstance(data, dict):
        for key in ("meal_slot", "target_kcal", "blocked", "actions", "meals",
                    "items", "options", "tips"):
            value = data.get(key)
            if isinstance(value, list):
                detail[f"{key}_count"] = len(value)
            elif isinstance(value, dict):
                detail[f"{key}_count"] = len(value)
            elif value is not None:
                detail[key] = value
        budget = data.get("budget") or {}
        if isinstance(budget, dict) and budget.get("remaining_kcal") is not None:
            detail["remaining_kcal"] = budget["remaining_kcal"]
    elif isinstance(data, list):
        detail["count"] = len(data)
    citations = result.get("citations") or []
    if citations:
        detail["citations"] = len(citations)
    return detail


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


def _run_loop(db, ctx: dict, text: str, client, on_step=None) -> TodayResult:
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
        # 模型这一等可能要几秒：先报"在思考"，别让前端只看到上一次的结果
        _push(trace, on_step, "agent", "thinking", step=step + 1)
        # 最终 JSON 含 3 餐 × 3 选项，1200 token 容易被截断成非法 JSON；
        # 给足预算，仍然失败就降级到确定性编排
        reply = client.complete(messages, tools=TOOLS, max_tokens=3000)
        message = reply.get("message") or {}
        usage = reply.get("usage") or {}
        usage_total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        usage_total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        calls = message.get("tool_calls") or []
        _push(trace, on_step, "agent", "tools" if calls else "final",
              tool_calls=[(call.get("function") or {}).get("name") for call in calls])
        if not calls:
            content = (message.get("content") or "").strip()
            truncated = reply.get("finish_reason") == "length"
            try:
                if truncated:
                    raise ValueError("output_truncated")
                answer = _parse_answer(content)
            except (ValueError, ValidationError) as exc:
                _push(trace, on_step, "parse", "failed",
                      error=f"{type(exc).__name__}: {str(exc)[:100]}")
                # 修复一次：只让模型重发文字部分，餐食本来就不在模型输出里
                answer = _repair(client, messages, f"{type(exc).__name__}: {str(exc)[:100]}")
                _push(trace, on_step, "parse", "repaired")
            final = _assemble(answer, food_meals, tool_citations, mcd_data)
            # 不管模型有没有主动查，规划完成后都补一次麦当劳营养库：
            # 这是产品需求（"每次规划后额外调一次"），不能靠模型自觉。
            final["mcdonalds"] = _mcd_block(db, ctx, trace, cached=mcd_data, on_step=on_step)
            return TodayResult(final=final, trace=trace, usage=usage_total)

        messages.append({"role": "assistant", "content": message.get("content") or "",
                         "tool_calls": calls})
        blocked = False
        for call in calls:
            name = (call.get("function") or {}).get("name") or ""
            args = _parse_args((call.get("function") or {}).get("arguments"))
            result = _tool_observation(dispatch(name, args, ctx, db))
            _push(trace, on_step, name, str(result.get("status")),
                  args=args, brief=_brief(result))
            for citation in result.get("citations") or []:
                tool_citations[citation["id"]] = citation
            if name == "mcd_nutrition" and result.get("status") == "ok":
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


def _mcd_block(db, ctx: dict, trace: list[dict], cached: dict | None = None,
               on_step=None) -> dict:
    """麦当劳官方营养库块：模型调用过就复用，没调用就补一次。

    失败也返回结构（带 status/error），前端能显示"为什么没有麦当劳"，
    而不是整块消失让人以为功能没做。
    """
    if cached:
        return cached
    if ctx.get("plan") is None:
        return {"status": "disabled", "error": "no_active_plan"}
    result = dispatch("mcd_nutrition", {}, ctx, db)
    _push(trace, on_step, "mcd_nutrition", str(result.get("status")))
    if result.get("status") == "ok" and result.get("data"):
        return result["data"]
    return {"status": result.get("status") or "error",
            "error": result.get("error") or "mcd_unknown"}


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


# ---------------------------------------------------------------- 时间偏好



def _normalize_clock(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


FEEDBACK_MARK = "修改意见："


def _latest_instruction(text: str) -> str:
    """只取最后一条修改意见；否则历次时间会越积越多，改一次反而多出一个时段。"""
    raw = text or ""
    if FEEDBACK_MARK in raw:
        return raw.rsplit(FEEDBACK_MARK, 1)[1]
    return raw


def _parse_times_with_spans(raw: str) -> list[tuple[str, int, int]]:
    import re

    raw = raw or ""
    ranges = list(re.finditer(
        r"([01]?\d|2[0-3])[:：]([0-5]\d)\s*[-~至到]\s*([01]?\d|2[0-3])[:：]([0-5]\d)",
        raw,
    ))
    if ranges:
        return [
            (f"{_normalize_clock(int(m.group(1)), int(m.group(2)))}-"
             f"{_normalize_clock(int(m.group(3)), int(m.group(4)))}",
             m.start(), m.end())
            for m in ranges
        ]

    def apply_period(hour: int, period: str) -> int:
        if period in ("下午", "晚上", "傍晚") and hour < 12:
            return hour + 12
        if period == "中午" and hour < 11:
            return 12
        if period in ("早上", "上午") and hour == 12:
            return 0
        return hour

    clocks = list(re.finditer(
        r"(早上|上午|中午|下午|晚上|傍晚)?\s*([01]?\d|2[0-3])[:：]([0-5]\d)", raw))
    if clocks:
        return [
            (_normalize_clock(apply_period(int(m.group(2)), m.group(1)), int(m.group(3))),
             m.start(), m.end())
            for m in clocks
        ]

    # "晚上 8 点 / 7点半 / 7点30分"
    out = []
    for m in re.finditer(
            r"(早上|上午|中午|下午|晚上|傍晚)?\s*(\d{1,2})\s*点\s*(半|\d{1,2}\s*分)?", raw):
        h = apply_period(int(m.group(2)), m.group(1))
        suffix = (m.group(3) or "").strip()
        minute = 30 if suffix == "半" else (
            int(suffix.replace("分", "").strip()) if suffix else 0)
        if 0 <= h <= 23 and 0 <= minute <= 59:
            out.append((_normalize_clock(h, minute), m.start(), m.end()))
    return out


def _parse_times(raw: str) -> list[str]:
    return [value for value, _, _ in _parse_times_with_spans(raw)]


TRAINING_WORDS = ("训练", "运动", "健身", "跑步", "力量", "有氧", "练")
MEAL_WORDS = ("早餐", "早饭", "午餐", "午饭", "晚餐", "晚饭", "加餐", "吃饭", "用餐")


def _nearest_kind(raw: str, start: int, end: int) -> str | None:
    """时间点最近的关键词是"餐"还是"训练"；用来避免把早餐时间当成训练时间。"""
    best_distance, best_kind = 10 ** 9, None
    for kind, words in (("meal", MEAL_WORDS), ("training", TRAINING_WORDS)):
        for word in words:
            cursor = raw.find(word)
            while cursor != -1:
                distance = min(abs(cursor - end), abs(start - (cursor + len(word))))
                if distance < best_distance:
                    best_distance, best_kind = distance, kind
                cursor = raw.find(word, cursor + 1)
    return best_kind if best_distance <= 10 else None


def _filter_meal_times(raw: str) -> list[str]:
    kept = []
    for value, start, end in _parse_times_with_spans(raw):
        if _nearest_kind(raw, start, end) != "meal":
            kept.append(value)
    return kept


def _preferred_times(text: str) -> list[str]:
    """从用户原话里抓训练时间；支持 7:30 / 7点 / 7点半 / 7:30-8:10。

    有"修改意见"时，后一次给的时间覆盖前一次，而不是把两次时间拼起来。
    早餐/午餐/晚餐的时间不算训练时间。
    """
    latest = _latest_instruction(text)
    if _parse_times(latest):
        return _filter_meal_times(latest)
    return _filter_meal_times(text)


def _explicit_split_count(raw: str) -> int:
    if any(word in raw for word in ("三次", "3次", "3 次", "分三段", "分三次")):
        return 3
    if any(word in raw for word in ("两次", "2次", "2 次", "分两段", "分两次", "几次")):
        return 2
    if any(word in raw for word in (
            "合并成一次", "合成一次", "只练一次", "一次训练", "不用分", "不要分",
            "就练一次", "改成一次", "1次", "1 次")):
        return 1
    return 0


def _split_count(text: str, times: list[str]) -> int:
    latest = _latest_instruction(text)
    latest_count = _explicit_split_count(latest)
    if latest_count:
        return latest_count
    if latest != text and _parse_times(latest):
        return min(3, max(1, len(times)))
    base_count = _explicit_split_count(text)
    if base_count:
        return base_count
    if len(times) >= 2:
        return min(3, len(times))
    return 1


def _session_cap_minutes(text: str) -> int | None:
    """识别"每次训练控制在30分钟"；只缩短，不把一次训练拉长。"""
    import re

    match = re.search(
        r"(?:每次|单次|每个时段)[^。；;\n]{0,8}?(\d{1,3})\s*分钟", text or "")
    if not match:
        return None
    value = int(match.group(1))
    return value if 10 <= value <= 180 else None


def _schedule_for(text: str, actions: list[dict]) -> list[dict]:
    """把训练动作按用户时间偏好拆成 1-3 个时段。

    模型路径由 prompt 约束；这里是模型不可用/校验失败时的确定性兜底，
    不能永远写死 18:30。
    """
    times = _preferred_times(text)
    count = _split_count(text, times)
    if actions:
        count = max(1, min(count, len(actions)))
    if len(times) < count:
        defaults = {1: ["18:30"], 2: ["07:30", "18:30"],
                    3: ["07:30", "12:30", "18:30"]}[count]
        used = set()
        for value in times:
            used.update(value.split("-"))
        for value in defaults:
            if len(times) >= count:
                break
            if value not in used:
                times.append(value)
                used.add(value)
    times.sort(key=lambda value: (
        int(value.split("-")[0].split(":")[0]) * 60
        + int(value.split("-")[0].split(":")[1])
    ))
    times = times[:count]
    if not times:
        times = ["18:30"]

    if not actions:
        return [{"time": times[0], "activity": "按当前计划执行", "why": "按当前计划版本执行"}]

    size, extra = divmod(len(actions), count)
    groups: list[list[dict]] = []
    cursor = 0
    for index in range(count):
        take = size + (1 if index < extra else 0)
        groups.append(actions[cursor:cursor + take])
        cursor += take
    cap = _session_cap_minutes(text)
    schedule = []
    for index, group in enumerate(groups):
        names = [str(item.get("name") or item.get("action_ref") or "训练") for item in group]
        minutes = sum(int(item.get("duration_min") or 0) for item in group) or 35
        if cap:
            minutes = min(minutes, cap)
        schedule.append({
            "time": times[index] if index < len(times) else times[-1],
            "activity": f"{'、'.join(names)} · 约 {minutes} 分钟",
            "why": "按你指定的时间拆分" if _preferred_times(text) else "按当前计划版本执行",
        })
    return schedule


def _enforce_schedule(final: dict, text: str, ctx: dict) -> None:
    """用户明确给了时间/拆分要求时，模型输出也要服从。

    模型可能仍按习惯写一个 18:30；这里用同一套确定性时间解析覆盖 schedule，
    避免"用户说了分两次，模型还是只给一个时段"。
    """
    times = _preferred_times(text)
    if not times and _split_count(text, times) <= 1 and not _session_cap_minutes(text):
        return
    actions = [
        {"name": (getattr(item, "meta", None) or {}).get("name_zh") or item.action_ref,
         "action_ref": item.action_ref,
         "duration_min": item.duration_min}
        for item in (ctx.get("actions") or [])
        if getattr(item, "action_type", "") == "exercise"
    ]
    final["schedule"] = _schedule_for(text, actions)


# ---------------------------------------------------------------- 确定性编排

def _deterministic(db, ctx: dict, text: str, on_step=None) -> TodayResult:
    trace: list[dict] = []
    safety = dispatch("safety_screen", {"text": text}, ctx, db)
    _push(trace, on_step, "safety_screen", str(safety.get("status")))
    if (safety.get("data") or {}).get("blocked"):
        return TodayResult(final=_blocked_answer(trace), trace=trace,
                           fallback=True, refused=True)

    plan = dispatch("get_today_plan", {}, ctx, db)
    _push(trace, on_step, "get_today_plan", str(plan.get("status")))
    nutrition = dispatch("get_nutrition_targets", {}, ctx, db)
    _push(trace, on_step, "get_nutrition_targets", str(nutrition.get("status")))

    body_part = "knee" if "膝" in text else ("back" if "腰" in text else None)
    knowledge = dispatch("search_knowledge",
                         {"query": "训练安排 热身 恢复", "body_part": body_part}, ctx, db)
    _push(trace, on_step, "search_knowledge", str(knowledge.get("status")))

    meals = []
    for slot in [slot for slot in (ctx.get("meals") or {}) if slot != "snack"][:3]:
        food = dispatch("search_food_options", {"meal_slot": slot}, ctx, db)
        _push(trace, on_step, "search_food_options", str(food.get("status")), meal_slot=slot)
        if food.get("status") != "ok":
            continue
        meals.append({
            "slot": slot,
            "target_kcal": food["data"]["target_kcal"],
            "options": food["data"]["options"],
        })

    # 每次规划后额外补一次麦当劳营养库（可直接点餐的替代选项）
    mcd = _mcd_block(db, ctx, trace, on_step=on_step)

    recovery = dispatch("get_recovery_tips", {}, ctx, db)
    _push(trace, on_step, "get_recovery_tips", str(recovery.get("status")))

    actions = plan.get("data", {}).get("actions", []) if plan.get("status") == "ok" else []
    citations = {item["id"]: item for item in (
            (knowledge.get("citations") or []) + (recovery.get("citations") or []))}

    # 动作要领：逐个动作查知识库，作为 tips 给出来（Agent 没讲时由这里兜底）
    cue_tips: list[dict] = []
    for action in actions[:3]:
        cue = dispatch("search_knowledge",
                       {"query": action.get("name") or action.get("ref") or "动作要领"}, ctx, db)
        _push(trace, on_step, "search_knowledge", str(cue.get("status")), target=action.get("name"))
        for item in cue.get("citations") or []:
            citations[item["id"]] = item
        data = cue.get("data") or []
        if data:
            cue_tips.append({"text": f"{action.get('name')}：{data[0]['content'][:80]}",
                             "source_ids": [data[0]["id"]]})

    schedule = _schedule_for(text, actions)
    split_note = f"分 {len(schedule)} 次训练；" if len(schedule) > 1 else ""
    final = {
        "summary": (
            f"今天 {len(actions)} 个动作；{split_note}{len(meals)} 餐各给了 3 个替换选项，"
            "训练前先热身，餐后留出 60-90 分钟再练。"
        ),
        "schedule": schedule,
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
    # 留痕：ok 给数据，失败也带 error，前端和排查都能看到
    final["mcdonalds"] = mcd
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

def run(db, user, day, text: str, client=None, on_step=None,
        mcd_token: str | None = None) -> TodayResult:
    """跑一轮今日规划。

    on_step(step) 是"边跑边看"的钩子：每记一步就回调一次，
    后台任务用它把 trace 立刻写库，前端轮询就能看到进度，而不是干等。
    mcd_token 是用户自己的麦当劳 MCP token；为空时用服务端配置。
    """
    ctx = load_context(db, user, day)
    ctx["mcd_token"] = mcd_token
    # 手机比价工具需要 VLM 客户端（同一个 OpenAI 兼容客户端即可）
    ctx["vlm_client"] = client
    try:
        result = (_run_loop(db, ctx, text, client, on_step=on_step) if client
                  else _deterministic(db, ctx, text, on_step=on_step))
    except Exception as exc:  # noqa: BLE001 - 模型失败要能降级
        result = _deterministic(db, ctx, text, on_step=on_step)
        result.error = f"{type(exc).__name__}: {str(exc)[:120]}"

    if result.final and not result.refused:
        _enforce_schedule(result.final, text, ctx)

    problems = validate(result.final or {}, ctx, db)
    if problems:
        fallback = _deterministic(db, ctx, text, on_step=on_step)
        _push(result.trace, on_step, "validate", "failed", problems=problems[:5])
        fallback.trace = result.trace
        fallback.error = "; ".join(problems[:3])
        return fallback
    _push(result.trace, on_step, "validate", "ok")
    return result
