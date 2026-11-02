"""计划生成领域服务（对应 docs/03 的 Plan Generation 流程）。

流水线：calculate_targets（算目标）→ validate_targets（目标安全门）
        → build_actions（按模板组装 12 个槽位）→ validate_actions（计划安全门）
        → 落库为新版本（旧版本标 superseded）。

关键取舍：P0 用确定性模板 + 数值规则，而不是直接让 LLM 自由生成，
这样 validate 才可能真正拦得住；LLM 版本后续作为模板的可选替代。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Goal, Plan, PlanAction, PlanVersion, Profile, User


class PlanValidationError(ValueError):
    """计划校验失败：携带错误列表，由接口层转成 422 + detail。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class PlanTargets:
    """一次生成所需的全部数值目标；frozen 保证算出来后不会被中途改写。"""

    bmr: float
    tdee: float
    calories: float
    protein_g: float
    fat_g: float
    carbs_g: float
    step_target: int
    training_days: int


# 活动系数（PAL，常用口径）
ACTIVITY_FACTORS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "high": 1.725,
}

# 热量保守下限：低于该值不做减脂缺口，避免"越狠越有效"的错误导向
CALORIE_FLOOR = {"male": 1500.0, "female": 1200.0, "other": 1300.0}


def _age(birth_year: int) -> int:
    """按出生年份估算年龄，并夹到成人下限（P0 不服务未成年人）。"""
    return max(18, date.today().year - birth_year)


def calculate_targets(profile: Profile) -> PlanTargets:
    """用 Mifflin-St Jeor 公式估算 BMR/TDEE，并按保守缺口推导宏量营养素。

    计算口径：
    - BMR：10*kg + 6.25*cm - 5*age + 性别的固定项
    - TDEE = BMR × 活动系数
    - 缺口取 min(500 kcal, 20% TDEE)，再受热量下限保护
    - 蛋白质 1.6 g/kg、脂肪 0.8 g/kg，剩余热量全部给碳水
    """
    age = _age(profile.birth_year)
    bmr = 10 * profile.weight_kg + 6.25 * profile.height_cm - 5 * age
    if profile.sex == "male":
        bmr += 5
    elif profile.sex == "female":
        bmr -= 161
    else:
        # 性别未提供时取男女修正项的近似中值
        bmr -= 78

    factor = ACTIVITY_FACTORS.get(profile.activity_level, 1.375)
    tdee = bmr * factor
    deficit = min(500.0, tdee * 0.20)
    calories = max(CALORIE_FLOOR.get(profile.sex, 1300.0), tdee - deficit)

    protein_g = round(1.6 * profile.weight_kg, 1)
    fat_g = round(0.8 * profile.weight_kg, 1)
    # 碳水 = 剩余热量 / 4 kcal per gram，不足时归零
    carbs_g = max(0.0, round((calories - protein_g * 4 - fat_g * 9) / 4, 1))

    return PlanTargets(
        bmr=round(bmr, 1),
        tdee=round(tdee, 1),
        calories=round(calories, 1),
        protein_g=protein_g,
        fat_g=fat_g,
        carbs_g=carbs_g,
        step_target=8000,
        # 训练日夹在 2～5 天，既保证频次也不至于过度训练
        training_days=min(5, max(2, profile.weekly_days)),
    )


def validate_targets(profile: Profile, targets: PlanTargets) -> list[str]:
    """目标安全门：热量下限、缺口比例、蛋白区间、训练天数四类硬约束。

    返回错误列表；空列表表示通过（错误由调用方统一抛出，便于一次返回全部问题）。
    """
    errors: list[str] = []
    floor = CALORIE_FLOOR.get(profile.sex, 1300.0)

    if targets.calories < floor:
        errors.append(f"热量目标 {targets.calories} 低于保守下限 {floor}")
    if targets.calories > targets.tdee - 200:
        errors.append("热量目标没有形成安全缺口")
    deficit_ratio = (targets.tdee - targets.calories) / targets.tdee if targets.tdee else 0
    if deficit_ratio > 0.25:
        errors.append(f"热量缺口 {deficit_ratio:.0%} 超过 25% 上限")
    if not (1.2 * profile.weight_kg <= targets.protein_g <= 2.2 * profile.weight_kg):
        errors.append("蛋白质目标不在 1.2～2.2 g/kg 区间")
    if targets.training_days < 2 or targets.training_days > 5:
        errors.append("训练天数必须在 2～5 天")
    if targets.training_days > profile.weekly_days:
        errors.append("训练天数超过用户每周可练天数")
    return errors


# 餐食模板库：allergens 用于按用户过敏原做排除，kcal/protein 用于估算
MEALS = [
    {
        "ref": "oats_eggs",
        "name": "燕麦鸡蛋",
        "kcal": 450,
        "protein": 25,
        "allergens": ["egg", "dairy"],
    },
    {
        "ref": "chicken_rice_bowl",
        "name": "鸡胸糙米碗",
        "kcal": 620,
        "protein": 45,
        "allergens": [],
    },
    {
        "ref": "beef_salad",
        "name": "牛肉沙拉",
        "kcal": 520,
        "protein": 38,
        "allergens": [],
    },
    {
        "ref": "fish_veggies",
        "name": "鱼排蔬菜",
        "kcal": 480,
        "protein": 36,
        "allergens": ["fish"],
    },
    {
        "ref": "tofu_veggies",
        "name": "豆腐时蔬",
        "kcal": 430,
        "protein": 28,
        "allergens": ["soy"],
    },
    {
        "ref": "rice_veggies",
        "name": "米饭时蔬",
        "kcal": 500,
        "protein": 15,
        "allergens": [],
    },
]

# 动作模板库：equipment 为空表示自重即可完成
EXERCISES = {
    "squat": {
        "name": "高脚杯深蹲",
        "equipment": ["dumbbell"],
        "sets": 3,
        "reps": 12,
    },
    "bodyweight_squat": {
        "name": "自重深蹲",
        "equipment": [],
        "sets": 3,
        "reps": 15,
    },
    "pushup": {"name": "俯卧撑", "equipment": [], "sets": 3, "reps": 10},
    "row": {"name": "哑铃划船", "equipment": ["dumbbell"], "sets": 3, "reps": 12},
    "backpack_row": {"name": "背包划船", "equipment": [], "sets": 3, "reps": 12},
    "brisk_walk": {"name": "快走", "equipment": [], "duration_min": 30},
    "easy_walk": {"name": "低冲击快走", "equipment": [], "duration_min": 25},
    "glute_bridge": {"name": "臀桥", "equipment": [], "sets": 3, "reps": 15},
    "stretch": {"name": "全身拉伸", "equipment": [], "duration_min": 15},
}


def _meal_for_slot(profile: Profile, slot: str) -> dict:
    """挑一份不含用户过敏原的餐食；全部冲突时退回最后一条（米饭时蔬）作为兜底。"""
    allergies = set(profile.allergies or [])
    for meal in MEALS:
        if not (set(meal["allergens"]) & allergies):
            return meal
    return MEALS[-1]


def _pick_exercise(profile: Profile, kind: str) -> tuple[str, dict]:
    """按动作类别 + 可用器械选具体动作，缺器械时降级为自重替代。"""
    equipment = set(profile.equipment or [])
    if kind == "squat":
        if "dumbbell" in equipment:
            key = "squat"
        else:
            key = "bodyweight_squat"
    elif kind == "push":
        key = "pushup"
    elif kind == "row":
        key = "row" if "dumbbell" in equipment else "backpack_row"
    elif kind == "walk":
        key = "brisk_walk"
    else:
        key = "stretch"
    return key, EXERCISES[key]


def build_actions(profile: Profile, targets: PlanTargets) -> list[dict]:
    """按固定周模板组装 12 个槽位，再把"动作类别"实例化成具体动作/餐食。

    模板本身是常量表：每周结构稳定，变化只来自画像（器械、过敏原），
    这也让 diff 的 churn 指标有可比性。
    """
    rows: list[tuple] = [
        (1, "strength", 1, "exercise", "squat", None, None),
        (1, "meal_lunch", 1, "meal", "chicken_rice_bowl", None, None),
        (2, "cardio", 1, "exercise", "walk", 30, None),
        (2, "meal_breakfast", 1, "meal", "oats_eggs", None, None),
        (2, "meal_dinner", 1, "meal", "fish_veggies", None, None),
        (3, "strength", 1, "exercise", "push", None, None),
        (3, "meal_lunch", 1, "meal", "beef_salad", None, None),
        (4, "cardio", 1, "exercise", "walk", 30, None),
        (5, "strength", 1, "exercise", "row", None, None),
        (5, "meal_dinner", 1, "meal", "tofu_veggies", None, None),
        (6, "cardio", 1, "exercise", "walk", 30, None),
        (7, "mobility", 1, "exercise", "stretch", 15, None),
    ]

    actions: list[dict] = []
    for day, slot, seq, action_type, key, duration, _ in rows:
        action = {
            "day_index": day,
            "slot": slot,
            "seq": seq,
            "slot_key": f"{day}:{slot}:{seq}",
            "action_type": action_type,
            "intensity": "moderate" if action_type == "exercise" else None,
            "meta": {},
            "evidence_ids": [],
        }
        if action_type == "meal":
            meal = _meal_for_slot(profile, slot)
            action.update(
                action_ref=meal["ref"],
                portion_g=None,
                # meta 存展示所需营养信息，避免前端再查一次模板库
                meta={
                    "name": meal["name"],
                    "kcal": meal["kcal"],
                    "protein_g": meal["protein"],
                    "allergens": meal["allergens"],
                },
            )
        else:
            ref, exercise = _pick_exercise(profile, key)
            action.update(
                action_ref=ref,
                duration_min=duration or exercise.get("duration_min"),
                sets=exercise.get("sets"),
                reps=exercise.get("reps"),
                meta={
                    "name": exercise["name"],
                    "equipment": exercise["equipment"],
                },
            )
        actions.append(action)
    return actions


def validate_actions(profile: Profile, actions: list[dict]) -> list[str]:
    """计划安全门：槽位数量、力量日下限与恢复日下限、过敏原冲突。"""
    errors: list[str] = []
    if len(actions) != 12:
        errors.append(f"计划动作数应为 12，实际 {len(actions)}")
    strength_days = {
        item["day_index"]
        for item in actions
        if item["action_type"] == "exercise" and item["slot"] == "strength"
    }
    if len(strength_days) < 2:
        errors.append("至少需要 2 个力量训练日")
    # 一周 7 天里至少留 2 天不练力量，保证恢复
    if len({1, 2, 3, 4, 5, 6, 7} - strength_days) < 2:
        errors.append("至少需要 2 个不练力量的日子")

    allergies = set(profile.allergies or [])
    for item in actions:
        if item["action_type"] == "meal":
            if set(item["meta"].get("allergens", [])) & allergies:
                errors.append(f"餐食 {item['action_ref']} 含过敏原")
    return errors


def generate_plan(db: Session, user: User) -> tuple[Plan, PlanVersion, list[PlanAction]]:
    """生成并提交一个新版本的计划。

    - 没有 active 计划时创建计划并落 v1；
    - 已有计划时版本号 +1，旧版本标记 superseded（不删除，保留可回滚的历史）。
    校验不通过时抛 PlanValidationError，且不产生任何写入。
    """
    profile = db.get(Profile, user.id)
    goal = db.scalar(
        select(Goal)
        .where(Goal.user_id == user.id, Goal.status == "active")
        .order_by(Goal.created_at.desc())
    )
    if profile is None or goal is None:
        raise PlanValidationError(["缺少 profile 或 active goal"])

    # 先算再校验：目标层不通过就不必组装动作
    targets = calculate_targets(profile)
    errors = validate_targets(profile, targets)
    if errors:
        raise PlanValidationError(errors)

    actions = build_actions(profile, targets)
    errors = validate_actions(profile, actions)
    if errors:
        raise PlanValidationError(errors)

    plan = db.scalar(
        select(Plan)
        .where(Plan.user_id == user.id, Plan.status == "active")
        .order_by(Plan.created_at.desc())
    )
    if plan is None:
        plan = Plan(user_id=user.id, goal_id=goal.id, status="active", current_version=1)
        db.add(plan)
        db.flush()
        new_version = 1
        old_version = None
    else:
        old_version = plan.current_version
        new_version = old_version + 1
        # 旧版本降级为 superseded，供回滚与审计追溯
        old = db.scalar(
            select(PlanVersion).where(
                PlanVersion.plan_id == plan.id, PlanVersion.version == old_version
            )
        )
        if old is not None:
            old.status = "superseded"

    version = PlanVersion(
        plan_id=plan.id,
        version=new_version,
        parent_version=old_version,
        status="active",
        calorie_target=targets.calories,
        protein_target_g=targets.protein_g,
        step_target=targets.step_target,
        summary=(
            f"4 周减脂第 {new_version} 版：{targets.training_days} 次训练，"
            f"热量 {targets.calories:.0f} kcal，蛋白质 {targets.protein_g:.0f}g。"
        ),
    )
    db.add(version)
    db.flush()

    records: list[PlanAction] = []
    for action in actions:
        record = PlanAction(plan_version_id=version.id, **action)
        db.add(record)
        records.append(record)

    plan.current_version = new_version
    db.commit()
    # commit 后刷新，保证返回给接口层的对象带完整时间戳/默认值
    db.refresh(version)
    for record in records:
        db.refresh(record)
    return plan, version, records
