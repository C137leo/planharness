"""把「用户一句话」翻译成一组补丁操作。

这一层要解决的是「补丁太死」：现在只有 create_knee_patch 一条硬编码路径，
只能处理「膝痛 + 只能练 N 天」。这里改成 **意图 → 操作** 的通用流程：

    parse_intent(文本/快捷项) → Intent → plan_operations(Intent, 当前计划) → operations

P0 的意图解析是规则驱动（关键词 + 快捷项），因为：
- 演示环境没有稳定的模型可用；
- 规则可复现、可测试、出错能定位到具体某条规则。

但接口留成 LLM 可替换的形状：`parse_intent` 返回的就是模型该产出的 JSON。
接真模型时只要换实现，下游（校验、草案、审批）完全不动。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.patches import FORBIDDEN_KNEE_REFS
from app.domain.planning import MEALS

# ---------------------------------------------------------------- 意图


@dataclass
class Intent:
    """用户到底想干什么。字段名和 LLM 该返回的 JSON 保持一致。"""

    kind: str                                   # rest / injury_stop / injury_swap / increase / diet / other
    body_parts: list[str] = field(default_factory=list)
    target_body_parts: list[str] = field(default_factory=list)
    available_days: int | None = None
    duration_min: int | None = None
    notes: str = ""
    needs_clarification: bool = False
    question: str | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "body_parts": self.body_parts,
            "target_body_parts": self.target_body_parts,
            "available_days": self.available_days,
            "duration_min": self.duration_min,
            "notes": self.notes,
            "needs_clarification": self.needs_clarification,
            "question": self.question,
        }


QUICK_ACTIONS = ("rest", "injury_stop", "injury_swap", "increase", "diet")

# 身体部位关键词（中文 + 英文），P0 只做常见几个
BODY_KEYWORDS = {
    "knee": ("膝", "膝盖", "knee"),
    "lower_back": ("腰", "lower back"),
    "shoulder": ("肩", "shoulder"),
    "wrist": ("手腕", "腕", "wrist"),
    "ankle": ("脚踝", "踝", "ankle"),
    "elbow": ("肘", "elbow"),
}

# 动作 → 涉及部位。用于「避开受伤部位」。
EXERCISE_BODY_PARTS = {
    "goblet_squat": ["knee", "legs"],
    "squat": ["knee", "legs"],
    "bodyweight_squat": ["knee", "legs"],
    "jump_squat": ["knee", "legs"],
    "glute_bridge": ["hips"],
    "lunge": ["knee", "legs"],
    "brisk_walk": ["knee", "legs"],
    "easy_walk": ["legs"],
    "run": ["knee", "legs"],
    "jog": ["knee", "legs"],
    "row": ["shoulder", "lower_back"],
    "backpack_row": ["lower_back"],
    "kettlebell_swing": ["lower_back", "knee"],
    "pushup": ["shoulder", "chest"],
    "decline_pushup": ["shoulder", "chest"],
    "dynamic_warmup": [],
    "full_body_stretch": [],
    "stretch": [],
}

# 受伤时可以退到的低冲击动作（按优先级）
SAFE_FALLBACKS = ("glute_bridge", "backpack_row", "pushup", "easy_walk", "stretch")


def _find_body_parts(text: str) -> list[str]:
    found: list[str] = []
    for part, words in BODY_KEYWORDS.items():
        if any(word in text for word in words):
            found.append(part)
    return found


CN_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}


def _find_days(text: str) -> int | None:
    import re

    # 「2 天」「两天」「每周练 3 天」
    match = re.search(r"([1-7]|[一二两三四五六七])\s*天", text)
    if not match:
        return None
    raw = match.group(1)
    return int(raw) if raw.isdigit() else CN_NUMBERS.get(raw)


def parse_intent(text: str, quick_action: str = "") -> Intent:
    """规则解析。字段与 LLM 契约一致，将来可整体替换。"""
    text = (text or "").strip()
    action = (quick_action or "").strip()

    # 1) 快捷项直接给出意图（UI 上点的那一下就是结构化输入）
    if action in QUICK_ACTIONS:
        kind = action
    else:
        # 2) 关键词。顺序很重要：
        #    「膝盖疼，换上肢」应该被理解成"换部位"，而不是"停止训练"，
        #    所以 swap 的判定必须排在 injury_stop 前面。
        swap_hints = ("换部位", "换一个部位", "换个部位", "换上肢", "练上肢", "避开", "换个练法")
        stop_hints = ("受伤", "疼", "痛", "不舒服", "拉伤", "扭到")
        increase_hints = ("加强度", "增加强度", "加点强度", "强度", "加量", "进阶", "上重量")

        if any(w in text for w in ("休息", "不想练", "太累", "歇一天", "今天不练")):
            kind = "rest"
        elif any(w in text for w in swap_hints):
            kind = "injury_swap"
        elif any(w in text for w in stop_hints):
            kind = "injury_stop"
        elif any(w in text for w in increase_hints):
            kind = "increase"
        elif any(w in text for w in ("饮食", "吃", "餐", "过敏", "忌口", "清淡", "少油", "少盐")):
            kind = "diet"
        # 只说"这周只能练两天"也算明确需求：压训练日，不一定要受伤
        elif _find_days(text) is not None and any(
            w in text for w in ("只能", "没时间", "没那么多时间", "抽不出", "这周")
        ):
            kind = "time"
        else:
            kind = "other"

    parts = _find_body_parts(text)
    days = _find_days(text)

    # 「膝盖疼，这周只能练两天」不是"停练"，而是"少练 + 换动作"。
    # 只有在明确说"停止/不能练"时才按 injury_stop 处理。
    if kind == "injury_stop" and days is not None and not any(
        w in text for w in ("停止", "不能练", "先别练", "不练了")
    ):
        kind = "injury_swap"
    intent = Intent(
        kind=kind,
        body_parts=parts if kind in ("injury_stop", "injury_swap") else [],
        available_days=days,
        notes=text,
    )

    # 3) 信息不足就追问，而不是猜
    if kind == "other":
        intent.needs_clarification = True
        intent.question = (
            "我没太理解你想怎么调整。可以试试：今天休息 / 受伤了停止训练 / "
            "换个部位练 / 增加强度 / 调整饮食。"
        )
    elif kind == "injury_stop" and not parts:
        intent.needs_clarification = True
        intent.question = "是哪个部位受伤了？比如膝盖、腰、肩、手腕。"
    elif kind == "injury_swap":
        if not parts:
            intent.needs_clarification = True
            intent.question = "哪个部位受伤了？我会避开它来安排。"
        elif not intent.target_body_parts:
            if any(w in text for w in ("上肢", "上半身", "胸", "背", "手臂")):
                intent.target_body_parts = ["upper_body"]
            elif any(w in text for w in ("下肢", "下半身", "腿")):
                intent.target_body_parts = ["lower_body"]
            else:
                intent.target_body_parts = ["upper_body"]

    return intent


# ---------------------------------------------------------------- 规划操作


# 晚餐候选：按热量从低到高排，diet 意图从里面挑更轻的一份
LIGHT_DINNERS = ("tofu_veggies", "rice_veggies", "fish_veggies", "beef_salad")
MEAL_INDEX: dict[str, dict] = {meal["ref"]: meal for meal in MEALS}


def _action_body_parts(ref: str) -> list[str]:
    return EXERCISE_BODY_PARTS.get(ref, [])


def _pick_safe_replacement(item: dict, injured: set[str], used: set[str]) -> str | None:
    """挑一个不涉及受伤部位、且没被用过的替代动作。"""
    current = item["action_ref"]
    current_parts = set(_action_body_parts(current))
    candidates = [
        ref
        for ref in SAFE_FALLBACKS
        if ref != current and not (set(_action_body_parts(ref)) & injured)
    ]
    # 优先挑"原本就没用到"的动作，避免整份计划变成同一个动作
    fresh = [ref for ref in candidates if ref not in used]
    for ref in fresh or candidates:
        if current_parts <= injured:
            return ref
        return ref
    return None


def plan_operations(intent: Intent, actions: list[dict]) -> tuple[list[dict], list[str]]:
    """按意图生成补丁操作；返回 (operations, risks)。

    **这里只提案，不做最终裁决** —— 能不能用由安全门 + validator 决定。
    """
    operations: list[dict] = []
    risks: list[str] = []
    injured = set(intent.body_parts)

    exercises = [a for a in actions if a["action_type"] == "exercise"]
    used_refs = {a["action_ref"] for a in actions}

    if intent.kind == "rest":
        # 今天休息：把当天所有训练换成拉伸（保留热量与饮食结构）
        for item in exercises:
            if item.get("slot") == "mobility":
                continue
            operations.append(
                {
                    "op": "replace_action",
                    "slot_key": item["slot_key"],
                    "before_action_ref": item["action_ref"],
                    "after_action": {
                        "action_ref": "stretch",
                        "duration_min": 15,
                        "sets": None,
                        "reps": None,
                        "meta": {"name": "全身拉伸", "equipment": []},
                    },
                    "reason": "今天以休息为主，训练换成拉伸",
                }
            )
        risks.append("休息日仍建议保持轻度活动（散步/拉伸），避免完全静止。")
        return operations, risks

    if intent.kind == "injury_stop":
        # 受伤：停掉所有训练，只留饮食与恢复
        for item in exercises:
            operations.append(
                {
                    "op": "remove_action",
                    "slot_key": item["slot_key"],
                    "before_action_ref": item["action_ref"],
                    "reason": f"{'/'.join(intent.body_parts)} 不适，暂停训练",
                }
            )
        risks.append("练停期间请留意症状变化；如出现红肿、剧痛或活动受限，请及时就医。")
        return operations, risks

    if intent.kind == "injury_swap":
        # 受伤换部位：把涉及受伤部位的动作换成不涉及它的低冲击动作
        if not injured:
            return [], ["需要知道受伤部位才能避开它"]
        for item in exercises:
            parts = set(_action_body_parts(item["action_ref"]))
            if not (parts & injured):
                continue
            replacement = _pick_safe_replacement(item, injured, used_refs)
            if replacement is None:
                continue
            used_refs.add(replacement)
            operations.append(
                {
                    "op": "replace_action",
                    "slot_key": item["slot_key"],
                    "before_action_ref": item["action_ref"],
                    "after_action": {
                        "action_ref": replacement,
                        "duration_min": 20 if replacement == "easy_walk" else None,
                        "sets": 3 if replacement not in ("easy_walk", "stretch") else None,
                        "reps": 12 if replacement not in ("easy_walk", "stretch") else None,
                        "meta": {"name": replacement, "equipment": []},
                    },
                    "reason": f"{'/'.join(intent.body_parts)} 不适，换掉涉及该部位的动作",
                }
            )
        # 天数限制：力量日超过可用天数 → 删掉靠后的力量日
        if intent.available_days:
            strength_days = sorted(
                {
                    a["day_index"]
                    for a in actions
                    if a["action_type"] == "exercise" and a["slot"] == "strength"
                }
            )
            overflow = strength_days[intent.available_days:]
            for day in overflow:
                for item in actions:
                    if (
                        item["action_type"] == "exercise"
                        and item["slot"] == "strength"
                        and item["day_index"] == day
                    ):
                        operations.append(
                            {
                                "op": "remove_action",
                                "slot_key": item["slot_key"],
                                "before_action_ref": item["action_ref"],
                                "reason": f"本周只能练 {intent.available_days} 天，删除第 {day} 天力量",
                            }
                        )

        risks.append("替换后如仍有疼痛，请停止训练并咨询医生。")
        return operations, risks

    if intent.kind == "increase":
        # 加强度：给力量动作各加 1 组（保守，不超过 4 组）
        for item in exercises:
            if item.get("slot") != "strength":
                continue
            sets = item.get("sets") or 3
            if sets >= 4:
                continue
            operations.append(
                {
                    "op": "replace_action",
                    "slot_key": item["slot_key"],
                    "before_action_ref": item["action_ref"],
                    "after_action": {
                        "action_ref": item["action_ref"],
                        "sets": sets + 1,
                        "reps": item.get("reps"),
                        "duration_min": item.get("duration_min"),
                        "meta": item.get("meta", {}),
                    },
                    "reason": "进阶：力量动作加 1 组",
                }
            )
        risks.append("加量后请观察恢复情况；如果连续两天酸痛明显，请退回原强度。")
        return operations, risks

    if intent.kind == "diet":
        # 只动晚餐：换成热量更低的餐食模板，训练结构保持不变
        light = [ref for ref in LIGHT_DINNERS if ref in MEAL_INDEX]
        for item in actions:
            if item["action_type"] != "meal" or item.get("slot") != "meal_dinner":
                continue
            current = item["action_ref"]
            replacement = next((ref for ref in light if ref != current), None)
            if replacement is None:
                continue
            operations.append(
                {
                    "op": "replace_action",
                    "slot_key": item["slot_key"],
                    "before_action_ref": current,
                    "after_action": {
                        "action_type": "meal",
                        "action_ref": replacement,
                        "sets": None,
                        "reps": None,
                        "duration_min": None,
                        "portion_g": MEAL_INDEX[replacement].get("portion_g"),
                        "kcal": MEAL_INDEX[replacement].get("kcal"),
                        "meta": {"name": MEAL_INDEX[replacement]["name"],
                                 "allergens": MEAL_INDEX[replacement].get("allergens", [])},
                    },
                    "reason": "晚餐换成更清淡的一份（少油、热量更低）",
                }
            )
        risks.append("饮食建议不构成医学营养处方；如有基础疾病请咨询专业人士。")
        return operations, risks

    if intent.kind == "time":
        # 时间不够：只保留前 N 个训练日，其余训练日删掉；餐食不动
        if not intent.available_days:
            return [], ["需要知道这周能练几天"]
        training_days = sorted(
            {a["day_index"] for a in exercises if a.get("slot") in ("strength", "cardio")}
        )
        overflow = training_days[intent.available_days:]
        for day in overflow:
            for item in exercises:
                if item["day_index"] != day:
                    continue
                operations.append(
                    {
                        "op": "remove_action",
                        "slot_key": item["slot_key"],
                        "before_action_ref": item["action_ref"],
                        "reason": f"本周只能练 {intent.available_days} 天，去掉第 {day} 天训练",
                    }
                )
        risks.append("压缩天数后仍建议保留一次低强度活动（散步/拉伸）。")
        return operations, risks

    return [], ["暂时还不支持这类调整"]


def cover_injured_slots(intent: Intent, base: list[dict],
                        operations: list[dict]) -> list[dict]:
    """模型可能只改一部分：把"仍然涉及受伤部位"的槽位补齐。

    这是 harness 该做的事——模型的提议可以不全，但**结果**必须满足硬约束。
    返回需要追加的操作（不修改入参）。
    """
    from app.domain.patches import _apply_operations

    if intent.kind not in ("injury_stop", "injury_swap") or not intent.body_parts:
        return []
    injured = set(intent.body_parts)
    try:
        after = _apply_operations(base, operations)
    except Exception:  # noqa: BLE001 - 操作本身有问题时交给上层报错
        return []

    touched = {op.get("slot_key") for op in operations}
    extra: list[dict] = []
    used_refs = {item["action_ref"] for item in after}
    for item in after:
        if item["action_type"] != "exercise" or item["slot_key"] in touched:
            continue
        parts = set(_action_body_parts(item["action_ref"]))
        if not (parts & injured):
            continue
        if intent.kind == "injury_stop":
            extra.append({
                "op": "remove_action",
                "slot_key": item["slot_key"],
                "before_action_ref": item["action_ref"],
                "reason": f"{'/'.join(intent.body_parts)} 不适，补删这个仍涉及该部位的动作",
            })
            continue
        replacement = _pick_safe_replacement(item, injured, used_refs)
        if replacement is None:
            continue
        used_refs.add(replacement)
        extra.append({
            "op": "replace_action",
            "slot_key": item["slot_key"],
            "before_action_ref": item["action_ref"],
            "after_action": {
                "action_ref": replacement,
                "duration_min": 20 if replacement == "easy_walk" else None,
                "sets": 3 if replacement not in ("easy_walk", "stretch") else None,
                "reps": 12 if replacement not in ("easy_walk", "stretch") else None,
                "meta": {"name": replacement, "equipment": []},
            },
            "reason": f"{'/'.join(intent.body_parts)} 不适，补齐替换",
        })
    return extra


def violates_constraints(actions: list[dict], intent: Intent) -> list[str]:
    """通用校验：受伤部位的禁忌动作 + 力量日超限。"""
    problems: list[str] = []
    injured = set(intent.body_parts)

    for item in actions:
        if item["action_type"] != "exercise":
            continue
        ref = item["action_ref"]
        if "knee" in injured and ref in FORBIDDEN_KNEE_REFS:
            problems.append(f"膝盖不适却仍安排了 {ref}")
        if injured and set(_action_body_parts(ref)) & injured and intent.kind == "injury_swap":
            problems.append(f"仍安排了涉及受伤部位的 {ref}")

    if intent.available_days:
        strength_days = {
            a["day_index"]
            for a in actions
            if a["action_type"] == "exercise" and a["slot"] == "strength"
        }
        if len(strength_days) > intent.available_days:
            problems.append(
                f"力量训练日 {len(strength_days)} 超过可用 {intent.available_days} 天"
            )

    return problems
