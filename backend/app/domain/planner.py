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

from app.domain.planning import MEALS
from app.domain.safety_rules import (
    detect_regions,
    is_excluded_ref,
    labels_of,
    name_of as movement_name,
    regions_of,
    safe_replacement,
    side_conflict,
    why_risky,
)

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
    # 用户明确点名"不要/不能用"的动作：tokens 用于匹配 ref，labels 用于展示
    excluded_tokens: list[str] = field(default_factory=list)
    excluded_labels: list[str] = field(default_factory=list)

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
            "excluded_tokens": self.excluded_tokens,
            "excluded_labels": self.excluded_labels,
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
    """部位识别交给通用规则表：膝/踝/腿/髋/腰/背/胸/肩/肘/腕/手臂都认。"""
    return detect_regions(text)


CN_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}


def _find_days(text: str) -> int | None:
    import re

    # 「2 天」「两天」「每周练 3 天」
    match = re.search(r"([1-7]|[一二两三四五六七])\s*天", text)
    if not match:
        return None
    raw = match.group(1)
    return int(raw) if raw.isdigit() else CN_NUMBERS.get(raw)


# 用户点名"不要用"的动作 → ref 关键词。只收常见动作，避免把"不要加量"误伤。
EXCLUDE_ACTION_TERMS: dict[str, tuple[tuple[str, ...], str]] = {
    "俯卧撑": (("push-up", "pushup"), "俯卧撑"),
    "划船": (("row",), "划船"),
    "深蹲": (("squat",), "深蹲"),
    "硬拉": (("deadlift",), "硬拉"),
    "卧推": (("bench-press", "bench_press"), "卧推"),
    "弓步": (("lunge",), "弓步"),
    "引体": (("pull-up", "pullup", "chin-up"), "引体"),
    "平板支撑": (("plank",), "平板支撑"),
    "臀桥": (("glute-bridge", "glute_bridge"), "臀桥"),
    "腿举": (("leg-press", "leg_press"), "腿举"),
    "推举": (("overhead-press", "shoulder-press"), "推举"),
    "弯举": (("curl",), "弯举"),
    "侧平举": (("lateral-raise",), "侧平举"),
    "面拉": (("face-pull",), "面拉"),
}
# 只有出现这些否定/排除语义，才把动作名当成"不要用"
EXCLUDE_HINTS = (
    "不要", "不能", "不想", "别", "避免", "排除", "去掉", "除了", "不用",
    "不合适", "不行", "受不了", "还是不能", "不能用", "会用到",
)


def _find_exclusions(text: str) -> tuple[list[str], list[str]]:
    raw = text or ""
    if not any(word in raw for word in EXCLUDE_HINTS):
        return [], []
    tokens: list[str] = []
    labels: list[str] = []
    for name, (ref_tokens, label) in EXCLUDE_ACTION_TERMS.items():
        if name in raw:
            tokens.extend(ref_tokens)
            labels.append(label)
    return sorted(set(tokens)), labels


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
        stop_hints = ("受伤", "疼", "痛", "酸", "不舒服", "不适", "别扭", "拉伤", "扭到")
        explicit_stop = ("停止", "不能练", "先别练", "不练了", "别练了", "彻底休息")
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

    # 默认语义是"避开这个部位"，而不是"整份计划停掉"：
    # - 说了部位（膝/踝/腿/腰/背/肩/肘/腕/胸/手臂）→ injury_swap（只改涉及的槽位）
    # - 只有明确说"停止 / 不能练 / 不练了"，或者点了快捷项"先停练"→ injury_stop
    if (
        kind == "injury_stop"
        and parts
        and action != "injury_stop"
        and not any(word in text for word in explicit_stop)
    ):
        kind = "injury_swap"

    excluded_tokens, excluded_labels = _find_exclusions(text)
    intent = Intent(
        kind=kind,
        body_parts=parts if kind in ("injury_stop", "injury_swap") else [],
        available_days=days,
        notes=text,
        excluded_tokens=excluded_tokens,
        excluded_labels=excluded_labels,
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
            elif any(w in text for w in ("下肢", "下半身", "练腿", "腿部训练")):
                intent.target_body_parts = ["lower_body"]
            else:
                intent.target_body_parts = ["upper_body"]

    return intent


# ---------------------------------------------------------------- 规划操作


# 晚餐候选：按热量从低到高排，diet 意图从里面挑更轻的一份
LIGHT_DINNERS = ("tofu_veggies", "rice_veggies", "fish_veggies", "beef_salad")
MEAL_INDEX: dict[str, dict] = {meal["ref"]: meal for meal in MEALS}


def _action_body_parts(ref: str) -> list[str]:
    """动作涉及哪些部位：老映射表优先（更具体），查不到就用目录元数据推。"""
    return EXERCISE_BODY_PARTS.get(ref) or sorted(regions_of(ref))


def _pick_safe_replacement(item: dict, injured: set[str], used: set[str],
                           exclude: set[str] | list[str] | None = None) -> str | None:
    """挑一个不涉及受伤部位、且没被用过的替代动作（通用规则表给候选）。"""
    return safe_replacement(item["action_ref"], injured, used, exclude=exclude)


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
            excluded = is_excluded_ref(item["action_ref"], intent.excluded_tokens)
            # 除了"涉及受伤部位"，还要看整条链和用户点名排除的动作：
            # 用户说"俯卧撑/划船不能用"时，即使它们不涉及受伤部位，也要换掉。
            if (not excluded
                    and not (parts & injured)
                    and not side_conflict(item["action_ref"], injured)):
                continue
            replacement = _pick_safe_replacement(item, injured, used_refs, intent.excluded_tokens)
            if replacement is None:
                # 连反向肢体/核心/拉伸都没有 → 这个槽位直接删掉（静养）
                operations.append({
                    "op": "remove_action",
                    "slot_key": item["slot_key"],
                    "before_action_ref": item["action_ref"],
                    "reason": f"{labels_of(intent.body_parts)}不适，且没有安全的替代动作，先删掉这一项",
                })
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
                        "meta": {"name": replacement, "name_zh": movement_name(replacement),
                             "equipment": []},
                    },
                    "reason": f"{labels_of(intent.body_parts)}不适，换掉涉及该部位的动作",
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
            overflow = set(strength_days[intent.available_days:])
            if overflow:
                by_slot = {item["slot_key"]: item for item in actions}
                # 整天的槽位都要删：如果只删"没被替换过"的那些，
                # 这一天仍然有动作，力量日数不会真的下降。
                operations = [
                    op for op in operations
                    if by_slot.get(op.get("slot_key"), {}).get("day_index") not in overflow
                ]
                for item in actions:
                    if (
                        item["action_type"] == "exercise"
                        and item["slot"] == "strength"
                        and item["day_index"] in overflow
                    ):
                        operations.append(
                            {
                                "op": "remove_action",
                                "slot_key": item["slot_key"],
                                "before_action_ref": item["action_ref"],
                                # 标记用途：它不是为了"避开受伤部位"，剪枝时不能删
                                "why": "day_limit",
                                "reason": f"本周只能练 {intent.available_days} 天，删除第 {item['day_index']} 天力量",
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
        excluded = is_excluded_ref(item["action_ref"], intent.excluded_tokens)
        if (not excluded
                and not (parts & injured)
                and not side_conflict(item["action_ref"], injured)):
            continue
        if intent.kind == "injury_stop":
            extra.append({
                "op": "remove_action",
                "slot_key": item["slot_key"],
                "before_action_ref": item["action_ref"],
                "reason": f"{labels_of(intent.body_parts)}不适，补删这个仍涉及该部位的动作",
            })
            continue
        replacement = _pick_safe_replacement(item, injured, used_refs, intent.excluded_tokens)
        if replacement is None:
            extra.append({
                "op": "remove_action",
                "slot_key": item["slot_key"],
                "before_action_ref": item["action_ref"],
                "reason": f"{labels_of(intent.body_parts)}不适，且没有安全的替代动作，先删掉这一项",
            })
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
            "reason": f"{labels_of(intent.body_parts)}不适，补齐替换",
        })
    return extra


def trim_unrelated_ops(intent: Intent, base: list[dict],
                       operations: list[dict]) -> tuple[list[dict], int]:
    """剪掉"被改的槽位和受伤部位无关"的操作，返回 (保留的操作, 剪掉数)。

    模型经常"受伤 = 整份计划停掉"：实测让它处理"脚踝扭到了"，
    它把 9 个槽位全删了（含卧推、俯卧撑这种跟脚踝没关系的东西）。
    这是 harness 该管的事——模型可以激进，落库的补丁必须最小。
    """
    if intent.kind not in ("injury_stop", "injury_swap") or not intent.body_parts:
        return operations, 0
    injured = set(intent.body_parts)
    by_key = {item["slot_key"]: item for item in base}
    kept: list[dict] = []
    dropped = 0
    for op in operations:
        if op.get("why") == "day_limit":
            # 压训练日跟"哪个部位受伤"无关，不能当无关改动剪掉
            kept.append(op)
            continue
        item = by_key.get(op.get("slot_key"))
        if item is None:
            kept.append(op)
            continue
        risky = (why_risky(item["action_ref"], injured, item.get("meta"))
                 or is_excluded_ref(item["action_ref"], intent.excluded_tokens))
        if risky:
            kept.append(op)
        else:
            dropped += 1
    return kept, dropped


def violates_constraints(actions: list[dict], intent: Intent) -> list[str]:
    """通用校验：受伤部位的禁忌动作 + 力量日超限。"""
    problems: list[str] = []
    injured = set(intent.body_parts)

    for item in actions:
        if item["action_type"] != "exercise":
            continue
        ref = item["action_ref"]
        if is_excluded_ref(ref, intent.excluded_tokens):
            problems.append(f"你明确说不能用的动作仍在计划里：{movement_name(ref)}（{ref}）")
            continue
        hit = why_risky(ref, injured, item.get("meta"))
        if hit:
            problems.append(f"{hit}不适却仍安排了 {movement_name(ref)}（{ref}）")

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
