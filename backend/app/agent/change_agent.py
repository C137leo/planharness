"""定向修改 Agent：用户原话 → 具体改动操作。

为什么不让规则表说了算：用户说"这周膝盖不舒服，上肢多练一点但别太累"，
规则表只能命中"膝盖"，模型能同时处理"上肢""别太累""这周"三个约束。
但模型只负责**提议**，操作合不合法由确定性校验说了算（见 `validate_operations`）。

没有模型时退回规则表（`planner.parse_intent` + `plan_operations`），
所以接口行为在"有 Key / 没 Key"两种情况下都可用。
"""

from __future__ import annotations

import json

from app.domain.catalog import is_knee_risky_ref, name_of, refs_for_actions
from app.domain.patches import is_knee_risky
from app.domain.planner import QUICK_ACTIONS, Intent, parse_intent, plan_operations

MAX_OPERATIONS = 6

CHANGE_CONTRACT = """你是计划修改器。根据用户这句话，输出**具体要改哪些槽位**的 JSON。

规则：
1. 只允许 replace_action / remove_action 两种操作；slot_key 必须来自给定清单。
2. replace_action 的 after_action_ref 必须来自给定的动作清单。
3. 尽量少改：能改 1 个槽位就不要改 3 个。
3b. 「今天想休息 / 今天不练 / 挪到明天」这类需求用 remove_action 把那一训练日的槽位删掉，
    不要输出"换成同一个动作"的 replace（那是空操作）。
3c. 只有真的换动作才用 replace_action，且 after_action_ref 必须与原来的不同。
4. 受伤时不能换成同一部位的动作；可用天数减少时必须删掉多余训练日。
5. 信息不足（比如只说了"受伤了"但没说部位）时，status 用 clarifying 并在 question 里问一句。
6. 只输出 JSON，不要 Markdown 围栏：
{
  "status": "ready" | "clarifying" | "invalid",
  "kind": "rest | injury_stop | injury_swap | increase | diet | time | other",
  "summary": "一句话说清这次改什么",
  "question": "status=clarifying 时必填",
  "operations": [
    {"op": "replace_action", "slot_key": "1:strength:1",
     "after_action_ref": "backpack_row", "reason": "避开膝主导动作"}
  ]
}
"""


def build_messages(text: str, quick_action: str, actions: list[dict],
                   available_refs: list[str], constraints: dict) -> list[dict]:
    compact = [
        {
            "slot_key": item["slot_key"],
            "day": item["day_index"],
            "slot": item["slot"],
            "type": item["action_type"],
            "ref": item["action_ref"],
            "name": (item.get("meta") or {}).get("name_zh") or item["action_ref"],
        }
        for item in actions
    ]
    payload = {
        "user_text": text,
        "quick_action": quick_action,
        "current_slots": compact,
        "allowed_after_refs": available_refs,
        "constraints": constraints,
        "quick_actions": list(QUICK_ACTIONS),
    }
    return [
        {"role": "system", "content": CHANGE_CONTRACT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def validate_operations(operations: list[dict], actions: list[dict],
                        available_refs: set[str], quick_action: str,
                        text: str) -> tuple[list[dict], list[str]]:
    """确定性校验：模型给的每个操作都必须能落到实处，否则整份拒绝。

    返回 (规范化后的操作, 错误列表)。错误非空时调用方应当要求用户补充信息，
    而不是"尽力应用一部分"——部分应用会让计划处于谁也没预期过的中间状态。
    """
    by_key = {item["slot_key"]: item for item in actions}
    errors: list[str] = []
    normalized: list[dict] = []
    seen: set[str] = set()

    if len(operations) > MAX_OPERATIONS:
        errors.append(f"一次最多改 {MAX_OPERATIONS} 个槽位，这次解析出 {len(operations)} 个")

    for op in operations:
        kind = str(op.get("op") or "")
        slot_key = str(op.get("slot_key") or "")
        if slot_key in seen:
            errors.append(f"重复操作同一槽位: {slot_key}")
            continue
        seen.add(slot_key)
        if slot_key not in by_key:
            errors.append(f"槽位不存在: {slot_key}")
            continue
        current = by_key[slot_key]

        if kind == "remove_action":
            normalized.append({"op": "remove_action", "slot_key": slot_key,
                               "before_action_ref": current["action_ref"],
                               "reason": str(op.get("reason") or "")[:120]})
            continue
        if kind != "replace_action":
            errors.append(f"不支持的操作: {kind or '(空)'}")
            continue

        ref = str(op.get("after_action_ref") or "")
        if ref not in available_refs:
            errors.append(f"动作不在可选清单里: {ref or '(空)'}")
            continue
        want_sets = op.get("sets")
        want_reps = op.get("reps")
        changed_shape = (
            (want_sets is not None and int(want_sets) != int(current.get("sets") or 0))
            or (want_reps is not None and int(want_reps) != int(current.get("reps") or 0))
        )
        if ref == current["action_ref"] and not changed_shape:
            # 空操作：模型表达"别改这个"时会这么写，直接忽略，不算错误
            continue

        candidate = {"action_ref": ref, "meta": {"name_zh": ref}}
        if "knee" in _injured_parts(text, quick_action) and is_knee_risky(candidate):
            errors.append(f"膝盖不适时不能用 {ref}")
            continue

        normalized.append({
            "op": "replace_action",
            "slot_key": slot_key,
            "before_action_ref": current["action_ref"],
            "after_action": {
                "action_type": current["action_type"],
                "action_ref": ref,
                "sets": int(want_sets) if want_sets is not None else current.get("sets"),
                "reps": int(want_reps) if want_reps is not None else current.get("reps"),
                "duration_min": current.get("duration_min"),
                "meta": {"name_zh": ref, "source": "change_agent"},
            },
            "reason": str(op.get("reason") or "")[:120],
        })

    return normalized, errors


def _injured_parts(text: str, quick_action: str) -> set[str]:
    intent = parse_intent(text, quick_action)
    return set(intent.body_parts)


def propose_without_model(text: str, quick_action: str,
                          actions: list[dict]) -> dict:
    """规则表兜底：和模型路径返回同一个形状，调用方不需要分支。"""
    intent = parse_intent(text, quick_action)
    if intent.needs_clarification:
        return {
            "status": "clarifying",
            "kind": intent.kind,
            "summary": "",
            "question": intent.question or "还需要一点信息才能改。",
            "operations": [],
            "intent": intent.to_dict(),
        }
    operations, risks = plan_operations(intent, actions)
    if not operations:
        return {
            "status": "clarifying",
            "kind": intent.kind,
            "summary": "",
            "question": risks[0] if risks else "按你的描述暂时没有需要改的地方。",
            "operations": [],
            "intent": intent.to_dict(),
        }
    return {
        "status": "ready",
        "kind": intent.kind,
        "summary": _summary(intent, operations),
        "question": "",
        "operations": operations,
        "risks": risks,
        "intent": intent.to_dict(),
        "source": "rules",
    }


def propose_with_model(client, text: str, quick_action: str, actions: list[dict],
                       constraints: dict) -> dict:
    """模型编排：一次调用产出操作；解析失败就抛异常，由调用方退回规则表。"""
    refs = refs_for_actions(actions)
    messages = build_messages(text, quick_action, actions, refs, constraints)
    reply = client.complete(messages, tools=None, temperature=0.1, max_tokens=700)
    content = (reply["message"].get("content") or "").strip()
    data = _parse_json(content)
    operations = list(data.get("operations") or [])
    normalized, errors = validate_operations(
        operations, actions, set(refs), quick_action, text
    )
    if errors:
        return {
            "status": "clarifying",
            "kind": str(data.get("kind") or "other"),
            "summary": "",
            "question": "这个改法我没法直接执行：" + "；".join(errors) + "。换个说法试试？",
            "operations": [],
            "risks": errors,
            "intent": {"kind": str(data.get("kind") or "other")},
            "source": "model",
        }
    if not normalized:
        # 模型没给出有效操作：让规则表再试一次，它认得"休息/减天数/清淡"这类结构性改动
        fallback = propose_without_model(text, quick_action, actions)
        fallback["source"] = "rules_after_model"
        return fallback
    intent = parse_intent(text, quick_action)
    return {
        "status": "ready",
        "kind": str(data.get("kind") or intent.kind),
        "summary": str(data.get("summary") or _summary(intent, normalized)),
        "question": "",
        "operations": normalized,
        "risks": list(data.get("risks") or []),
        "intent": intent.to_dict(),
        "source": "model",
        "usage": reply.get("usage") or {},
    }


def _summary(intent: Intent, operations: list[dict]) -> str:
    replace = sum(1 for op in operations if op.get("op") == "replace_action")
    remove = sum(1 for op in operations if op.get("op") == "remove_action")
    parts = []
    if replace:
        parts.append(f"换掉 {replace} 个动作")
    if remove:
        parts.append(f"删掉 {remove} 个槽位")
    head = "、".join(parts) or "调整计划"
    return f"{head}（{intent.kind}）"


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
