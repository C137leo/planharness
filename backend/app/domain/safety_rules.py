"""身体部位 → 风险动作 → 安全替代（一套通用规则，替代"膝盖特例"）。

背景：以前只有膝痛是硬编码特例（`FORBIDDEN_KNEE_REFS` + 名字关键词），
脚踝/肩/腰/胸/肘/腕只能靠 `EXERCISE_BODY_PARTS` 里那十几个老动作猜，
而 skill 目录里有 302 个动作，等于基本没覆盖。

这里把判定改成三层信号，新增一个部位只要往 `REGIONS` 里加一行：

1. **目录元数据**（主信号）：`primary_muscle`（Quads/Shoulders/Chest/Lower Back…）
   和 `knee_risky` 标记；
2. **动作名 token**（兜底）：`squat` / `press` / `row` …，但会做上下肢一致性检查，
   避免「肩痛却把 leg-press 也拦掉」；
3. **动作模式**：`movement_mode=knee_dominant` 这类结构化标注。

规则只回答"这个动作会不会加重这个部位"，不回答"疼到什么程度该不该练"——
后者交给红旗症状筛查和就医建议，不在这里猜。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.exercise_catalog import EXERCISE_CATALOG

# 上下肢分组：token 命中后还要过这一关，否则 "leg-press" 会被 "press" 误伤
LOWER_MUSCLES = {"quads", "hamstrings", "glutes", "calves", "legs", "adductors",
                 "abductors", "hip flexors"}
UPPER_MUSCLES = {"chest", "shoulders", "triceps", "biceps", "lats", "upper back",
                 "lower back", "back", "forearms", "traps", "core"}

# 用户诉求很直接：上肢不适就只练下肢，下肢不适就只练上肢。
# 这条比"某个动作是否压到某个关节"更强：肩痛时划船、卧推、俯卧撑一律不排。
UPPER_REGIONS = {"shoulder", "chest", "back", "elbow", "wrist", "arm"}
LOWER_REGIONS = {"knee", "ankle", "legs", "hip"}
# 腰是躯干（屈伸/轴向负荷），不按上下肢切，仍按具体动作判断
SPINE_REGIONS = {"lower_back"}


@dataclass(frozen=True)
class Region:
    key: str
    label: str
    keywords: tuple[str, ...]
    muscles: frozenset[str]
    tokens: tuple[str, ...]
    # 只有膝盖用目录里的 knee_risky 标记（其余部位没有这个字段）
    uses_knee_flag: bool = False


def _r(key: str, label: str, keywords: tuple[str, ...], muscles: tuple[str, ...],
       tokens: tuple[str, ...], *, knee_flag: bool = False) -> Region:
    return Region(key=key, label=label, keywords=keywords,
                  muscles=frozenset(muscles), tokens=tokens, uses_knee_flag=knee_flag)


REGIONS: dict[str, Region] = {
    "knee": _r("knee", "膝盖", ("膝", "knee"), ("quads",),
               ("squat", "lunge", "split-squat", "pistol", "step-up", "run", "jog",
                "jump", "hop", "knee"), knee_flag=True),
    "ankle": _r("ankle", "脚踝", ("脚踝", "踝", "ankle"), ("calves",),
                ("run", "jog", "jump", "hop", "calf", "heel", "sprint", "skip",
                 "lunge", "step-up")),
    "legs": _r("legs", "腿", ("腿", "大腿", "小腿", "leg"),
               ("quads", "hamstrings", "glutes", "calves", "legs", "adductors"),
               ("squat", "lunge", "leg", "deadlift", "calf", "step-up")),
    "hip": _r("hip", "髋", ("髋", "胯", "hip"), ("glutes", "hip flexors", "adductors"),
              ("hip", "glute", "bridge", "thrust", "abduction", "kickback")),
    "lower_back": _r("lower_back", "腰", ("腰", "下背", "lower back"),
                     ("lower back", "back"),
                     ("deadlift", "rdl", "row", "swing", "good-morning",
                      "back-extension", "hyperextension", "bent-over")),
    "back": _r("back", "背", ("背", "背部", "upper back", "back"),
               ("back", "upper back", "lower back", "lats"),
               ("row", "pulldown", "pull-up", "chin-up", "deadlift", "shrug",
                "face-pull", "reverse-fly")),
    "chest": _r("chest", "胸", ("胸", "胸部", "chest", "pec"), ("chest",),
                ("bench", "push-up", "pushup", "fly", "dip", "chest", "pec")),
    "shoulder": _r("shoulder", "肩", ("肩", "shoulder"), ("shoulders",),
                   ("press", "raise", "fly", "dip", "pull-up", "chin-up",
                    "handstand", "snatch", "jerk", "arnold",
                    # 推/撑类：主练肌肉常写 Chest，但肩前束与肩袖同样吃力
                    "push-up", "pushup", "plank", "overhead", "scapular",
                    "mountain-climber", "bear-crawl")),
    "elbow": _r("elbow", "手肘", ("肘", "elbow"), ("biceps", "triceps", "forearms"),
                ("curl", "extension", "pushdown", "dip", "press", "pull-up", "chin-up",
                 "push-up", "pushup", "plank")),
    "wrist": _r("wrist", "手腕", ("手腕", "腕", "wrist"), ("forearms",),
                ("push-up", "pushup", "plank", "handstand", "wrist", "curl")),
    "arm": _r("arm", "手臂", ("手臂", "胳膊", "arm"), ("biceps", "triceps", "forearms"),
              ("curl", "pushdown", "extension", "dip", "press", "row",
               "push-up", "pushup", "plank")),
}

# 兜底替换池：都不带器械、冲击小；真正用哪个还要过 is_risky_for 过滤
SAFE_POOL: tuple[str, ...] = (
    "dead-bug", "banded-dead-bug", "stretch", "full-body-stretch",
    "glute-bridge", "banded-glute-bridge", "banded-row", "seated-row",
    "banded-face-pull", "chest-supported-row", "easy-walk", "hip-thrust",
)


def _norm(ref: str) -> str:
    return str(ref or "").replace("_", "-").lower()


def _catalog(ref: str) -> dict:
    return EXERCISE_CATALOG.get(ref) or EXERCISE_CATALOG.get(_norm(ref)) or {}


def _muscle(ref: str) -> str:
    return str(_catalog(ref).get("primary_muscle") or "").lower()


def label_of(region: str) -> str:
    item = REGIONS.get(region)
    return item.label if item else region


def labels_of(regions: list[str] | set[str]) -> str:
    return "/".join(label_of(region) for region in (regions or []))


def detect_regions(text: str) -> list[str]:
    """从用户原话里识别不适部位（按 REGIONS 顺序，去重）。"""
    found: list[str] = []
    raw = (text or "").lower()
    for key, region in REGIONS.items():
        if key in found:
            continue
        if any(word in raw for word in region.keywords):
            # "背" 也会命中 "lower back" 的英文，但中文里 腰/背 是两种诉求：
            # 命中"腰"就只算 lower_back，命中"背"才算 back
            found.append(key)
    # 腿痛常常也牵扯膝盖，但用户只说了"腿"，就不要替他把膝盖也算进去
    if "back" in found and "lower_back" in found:
        found.remove("back")
    return found


def _category_ok(muscle: str, region: str) -> bool:
    """上下肢一致性：肩痛不该因为 leg-press 里有 press 就误伤。"""
    if not muscle:
        return True
    if region in ("shoulder", "chest", "back", "arm", "elbow", "wrist"):
        return muscle not in LOWER_MUSCLES
    if region in ("ankle", "legs", "hip", "knee"):
        return muscle not in UPPER_MUSCLES or muscle == "core"
    return True


def is_risky_for(ref: str, region: str, meta: dict | None = None) -> bool:
    """这个动作是否会加重该部位。region 不认识时一律返回 False。"""
    config = REGIONS.get(region)
    if config is None:
        return False
    meta = meta or {}
    muscle = _muscle(ref)
    if muscle and muscle in config.muscles:
        return True
    if region == "knee":
        if _catalog(ref).get("knee_risky"):
            return True
        if str(meta.get("movement_mode") or "") == "knee_dominant":
            return True
    ref_norm = _norm(ref)
    if not any(token in ref_norm for token in config.tokens):
        return False
    # 腰/背是"稳定肌"：硬拉、划船这类动作主练的肌肉可能是腿或背阔，
    # 但对腰背的负荷是实打实的，所以这两个部位不做上下肢一致性过滤。
    if region in ("back", "lower_back"):
        return True
    # 其余部位要过上下肢一致性，避免"press" 把 leg-press 也算到肩上。
    # 目录里查不到的动作（muscle 为空）宁可按命中算，交给人工审批兜底。
    return _category_ok(muscle, region)


def regions_of(ref: str, meta: dict | None = None) -> set[str]:
    """这个动作会用到/压到哪些部位（用于"避开受伤部位"的覆盖检查）。"""
    return {region for region in REGIONS if is_risky_for(ref, region, meta)}


def movement_side(ref: str) -> str | None:
    """动作主要练上肢还是下肢；核心/查不到返回 None。"""
    muscle = _muscle(ref)
    if muscle in LOWER_MUSCLES:
        return "lower"
    if muscle in UPPER_MUSCLES and muscle != "core":
        return "upper"
    return None


# "整条肢体"级别的诉求：手臂 / 腿（关节级别不按整条链禁练，
# 否则膝痛会把臀桥也拦掉，而臀桥恰恰是对膝最友好的替代动作）
WHOLE_LIMB_REGIONS = {"arm": "upper", "legs": "lower"}


def injured_side(regions: list[str] | set[str]) -> str | None:
    """这组不适"主要在哪条肢体"：全在上肢 → upper，全在下肢 → lower，否则 None。

    用于替代动作的优先级（上肢不适优先给下肢动作），不用于安全门拦截。
    """
    parts = set(regions or [])
    if not parts:
        return None
    has_upper = bool(parts & UPPER_REGIONS)
    has_lower = bool(parts & LOWER_REGIONS)
    if has_upper and not has_lower:
        return "upper"
    if has_lower and not has_upper:
        return "lower"
    return None


def side_conflict(ref: str, regions: list[str] | set[str]) -> bool:
    """整条肢体都不适（手臂 / 腿）时，整条链的动作都不排。

    关节级别（膝/肩/肘…）只按具体动作判断：膝痛不该把臀桥也拦掉。
    """
    parts = set(regions or [])
    if not parts:
        return False
    sides = {WHOLE_LIMB_REGIONS[r] for r in parts if r in WHOLE_LIMB_REGIONS}
    if len(sides) != 1:
        return False
    return movement_side(ref) == next(iter(sides))


def why_risky(ref: str, regions: list[str] | set[str], meta: dict | None = None) -> str | None:
    """返回第一个命中部位的中文名（用于错误文案）。

    先按具体部位判断；若整条链都不该练（上肢伤还排上肢），返回该部位名。
    """
    for region in regions or []:
        if is_risky_for(ref, region, meta):
            return label_of(region)
    if side_conflict(ref, regions):
        side = injured_side(regions)
        # 文案用"肩/手臂"这种具体部位，方便用户理解
        first = next((r for r in (regions or []) if
                      (side == "upper" and r in UPPER_REGIONS) or
                      (side == "lower" and r in LOWER_REGIONS)), "")
        return label_of(first) if first else ("上肢" if side == "upper" else "下肢")
    return None


# skill 目录里一批动作没有中文名（name_zh 是空串），但它们是替换池里的常客，
# 直接显示 "dead-bug" 对用户没意义，所以这里补一份最小中文表。
CN_NAMES: dict[str, str] = {
    "dead-bug": "死虫式",
    "banded-dead-bug": "弹力带死虫式",
    "stretch": "拉伸",
    "full-body-stretch": "全身拉伸",
    "glute-bridge": "臀桥",
    "banded-glute-bridge": "弹力带臀桥",
    "hip-thrust": "臀推",
    "banded-row": "弹力带划船",
    "seated-row": "坐姿划船",
    "chest-supported-row": "胸支撑划船",
    "banded-face-pull": "弹力带面拉",
    "face-pull": "面拉",
    "lat-pulldown": "高位下拉",
    "inverted-row": "反向划船",
    "easy-walk": "快走",
    "backpack-row": "背包划船",
    "leg-curl": "腿弯举",
    "dumbbell-bent-over-row": "哑铃俯身划船",
}


def name_of(ref: str) -> str:
    """动作中文名：先查最小中文表，再查目录，最后退回 ref。"""
    key = _norm(ref)
    if key in CN_NAMES:
        return CN_NAMES[key]
    item = _catalog(ref)
    return item.get("name_zh") or item.get("name_en") or ref


def _safe_candidates(injured: set[str]) -> list[str]:
    """兜底候选：先用 SAFE_POOL，不够再从目录里补几个没风险的。"""
    candidates = [ref for ref in SAFE_POOL if not why_risky(ref, injured)]
    if len(candidates) < 3:
        for ref, item in EXERCISE_CATALOG.items():
            if item.get("primary_muscle") == "Core" and not why_risky(ref, injured):
                candidates.append(ref)
            if len(candidates) >= 6:
                break
    return candidates


# 上肢受伤时的替代优先级：能用轻负荷抗阻就别直接退化成拉伸
UPPER_SAFE_PREFERENCE = (
    "banded-row", "seated-row", "chest-supported-row", "banded-face-pull",
    "dead-bug", "banded-dead-bug", "stretch", "full-body-stretch", "easy-walk",
)


# 伤病期替换顺序：反向肢体 > 核心 > 拉伸；都没有就交给调用方删掉（静养）
FALLBACK_POOL = (
    # 下肢（上肢受伤时用）
    "goblet-squat", "leg-press", "hip-thrust", "glute-bridge", "walking-lunge",
    "leg-curl", "calf-raise", "romanian-deadlift",
    # 上肢（下肢受伤时用）
    "banded-row", "seated-row", "chest-supported-row", "dumbbell-bench-press",
    "incline-bench-press", "banded-face-pull", "push-up",
    # 核心 / 拉伸（兜底）
    "dead-bug", "banded-dead-bug", "stretch", "full-body-stretch",
)


def is_excluded_ref(ref: str, tokens: set[str] | list[str] | None) -> bool:
    """用户明确说"不要/不能用"的动作：按 ref 子串匹配。

    例：text 里写"俯卧撑和划船不能用"，tokens 会包含 push-up / pushup / row，
    于是 push-up、incline-push-up、seated-row、chest-supported-row 都会被排除。
    """
    norm = _norm(ref)
    return any(_norm(token) in norm for token in (tokens or []))


def safe_replacement(current_ref: str, injured: set[str], used: set[str],
                     *, prefer: str = "",
                     exclude: set[str] | list[str] | None = None) -> str | None:
    """挑一个不加重受伤部位、且没被用过的替代动作。

    规则：
    1. 先按部位过滤（`is_risky_for` 说不行的一律不选）；
    2. 用户在反馈里点名的动作（`exclude`）也不能选；
    3. 上肢替换优先"轻负荷抗阻"，其次核心，最后才是拉伸
       （肩痛不该直接把推类动作换成拉伸，训练刺激会掉太多）；
    4. 尽量不选计划里已经在用的动作，避免整份计划变成同一个动作。
    """
    side = injured_side(injured)
    opposite = {"upper": "lower", "lower": "upper"}.get(side or "", "")

    def rank(ref: str) -> int:
        """0=反向肢体（最想要） 1=核心 2=拉伸 3=其它"""
        ref_side = movement_side(ref)
        if opposite and ref_side == opposite:
            return 0
        if ref_side is None:
            return 1
        if _norm(ref) in ("stretch", "full-body-stretch"):
            return 2
        return 3

    pool = [
        ref for ref in FALLBACK_POOL
        if ref != current_ref
        and not why_risky(ref, injured)
        and not is_excluded_ref(ref, exclude)
    ]
    # 去重 + 按"反向肢体 > 核心 > 拉伸"排序
    seen: set[str] = set()
    pool = [ref for ref in pool if not (ref in seen or seen.add(ref))]
    pool.sort(key=rank)
    fresh = [ref for ref in pool if ref not in used]
    candidates = fresh or pool
    return candidates[0] if candidates else None


def region_names_for_error(ref: str, regions: list[str] | set[str]) -> str:
    """错误文案里用的部位清单，只保留真正命中的那几个。"""
    hit = [label_of(region) for region in regions or [] if is_risky_for(ref, region)]
    return "/".join(hit)
