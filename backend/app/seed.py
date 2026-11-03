"""演示种子数据。

目的：让服务一启动就有一份可演示的"第 1 周计划"，前端与测试无需先注册/建档。
所有 ID 固定，测试可以直接对 ID 与版本号做断言。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Goal, Plan, PlanAction, PlanVersion, Profile, User

# 固定 UUID：便于演示脚本与 pytest 直接引用，不依赖运行时生成
DEMO_USER_ID = "00000000-0000-0000-0000-000000000001"
DEMO_PLAN_ID = "00000000-0000-0000-0000-000000000101"
DEMO_VERSION_ID = "00000000-0000-0000-0000-000000000201"
DEMO_GOAL_ID = "00000000-0000-0000-0000-000000000301"


def _actions() -> list[dict]:
    """生成 seed 计划的 12 个槽位（含训练与餐食）。

    每行含义：(day_index, slot, seq, action_type, action_ref, duration_min, sets, reps)。
    这里刻意写成紧凑元组表，方便对照 docs 里的演示剧本核对。
    """
    rows = [
        (1, "warmup", 1, "exercise", "dynamic_warmup", 10, None, None),
        (1, "strength", 1, "exercise", "goblet_squat", None, 3, 12),
        (1, "meal_lunch", 1, "meal", "chicken_rice_bowl", None, None, None),
        (2, "meal_breakfast", 1, "meal", "oats_eggs", None, None, None),
        (2, "meal_lunch", 1, "meal", "beef_salad", None, None, None),
        (2, "meal_dinner", 1, "meal", "fish_veggies", None, None, None),
        (3, "strength", 1, "exercise", "pushup", None, 3, 10),
        (3, "cardio", 1, "exercise", "brisk_walk", 25, None, None),
        (4, "mobility", 1, "exercise", "full_body_stretch", 15, None, None),
        (5, "strength", 1, "exercise", "row", None, 3, 12),
        (5, "cardio", 1, "exercise", "brisk_walk", 25, None, None),
        (6, "meal_dinner", 1, "meal", "tofu_veggies", None, None, None),
    ]
    return [
        {
            "day_index": day,
            "slot": slot,
            "seq": seq,
            # slot_key 是全系统定位槽位的统一口径
            "slot_key": f"{day}:{slot}:{seq}",
            "action_type": action_type,
            "action_ref": action_ref,
            "sets": sets,
            "reps": reps,
            "duration_min": duration,
            "intensity": "moderate" if action_type == "exercise" else None,
            "meta": {},
            "evidence_ids": [],
        }
        for day, slot, seq, action_type, action_ref, duration, sets, reps in rows
    ]


def seed_demo(session: Session) -> None:
    """幂等地写入 demo 用户及其 v1 计划。

    已存在 demo 用户时直接返回，所以可以放心地在每次应用启动时调用。
    """
    if session.scalar(select(User).where(User.id == DEMO_USER_ID)) is not None:
        return

    user = User(id=DEMO_USER_ID, nickname="demo", role="user")
    session.add(user)
    # flush 而非 commit：让后续外键引用能拿到同一事务内的数据
    session.flush()

    profile = Profile(
        user_id=DEMO_USER_ID,
        sex="male",
        birth_year=1997,
        height_cm=178,
        weight_kg=78,
        activity_level="moderate",
        training_experience="beginner",
        equipment=["bodyweight", "dumbbell"],
        weekly_days=3,
        session_minutes=45,
        diet_pattern="omnivore",
        allergies=[],
        dislikes=[],
        # 演示画像：一个明确的目标 + 可用的器械，让"生成计划"一上来就有东西可算
        goal="fat_loss",
        goal_note="4 周减脂：每周 3 天，每次 40 分钟，家里只有一对哑铃",
        diet_preferences=[],
    )
    session.add(profile)

    goal = Goal(
        id=DEMO_GOAL_ID,
        user_id=DEMO_USER_ID,
        type="fat_loss",
        target_weight_kg=72,
        target_rate_kg_per_week=0.5,
        priority=1,
        status="active",
        source="user",
    )
    session.add(goal)
    session.flush()

    plan = Plan(
        id=DEMO_PLAN_ID,
        user_id=DEMO_USER_ID,
        goal_id=DEMO_GOAL_ID,
        status="active",
        current_version=1,
    )
    session.add(plan)
    session.flush()

    version = PlanVersion(
        id=DEMO_VERSION_ID,
        plan_id=DEMO_PLAN_ID,
        version=1,
        status="active",
        calorie_target=1800,
        protein_target_g=120,
        step_target=8000,
        summary="4 周减脂第 1 周：3 次训练 + 2 次快走，热量 1800 kcal。",
    )
    session.add(version)
    session.flush()

    for row in _actions():
        session.add(PlanAction(plan_version_id=DEMO_VERSION_ID, **row))

    session.commit()
