"""The registry is both the model's menu and the executor's dispatch allowlist."""
from dataclasses import dataclass
from typing import Literal
from pydantic import Field, field_validator, model_validator
from app.agent.binding_contract import StrictModel


class NoArgs(StrictModel):
    pass


class ActionValues(StrictModel):
    sets: int | None = Field(default=None, ge=1, le=10)
    reps: int | None = Field(default=None, ge=1, le=100)
    sets_delta: int | None = Field(default=None, ge=-9, le=9)
    reps_delta: int | None = Field(default=None, ge=-99, le=99)


class ExerciseArgs(ActionValues):
    exercise_id: str


class FoodArgs(StrictModel):
    ingredient_id: str
    grams: int | float = Field(gt=0, le=500)


class QuantityArgs(StrictModel):
    grams: int | float = Field(gt=0, le=1000)


class ProfileArgs(StrictModel):
    sex: Literal['male','female'] | None = None
    birth_year: int | None = Field(default=None,ge=1900,le=2026)
    height_cm: int | float | None = Field(default=None,ge=120,le=230)
    goal_note: str | None = Field(default=None,max_length=500)
    equipment: list[str] | None = None
    activity_level: Literal['sedentary','light','moderate','active'] | None = None
    training_experience: Literal['beginner','intermediate','advanced'] | None = None
    preferred_time_label: str | None = Field(default=None,max_length=32)
    goal: Literal["fat_loss", "muscle_gain", "cardio", "health"] | None = None
    weight_kg: int | float | None = Field(default=None, ge=30, le=300)
    weekly_days: int | None = Field(default=None, ge=1, le=7)
    session_minutes: int | None = Field(default=None, ge=5, le=180)
    preferred_time_window: str | None = Field(default=None, max_length=24)
    preferred_location: str | None = Field(default=None, max_length=24)
    dislikes: list[str] | None = None
    likes: list[str] | None = None
    allergies: list[str] | None = None
    diet_preferences: list[str] | None = None


class AllergyAdditions(StrictModel):
    allergies: list[str] = Field(min_length=1, max_length=20)


class NoteArgs(StrictModel):
    kind: Literal['long_term', 'short_term'] = 'short_term'
    retention_days: int = Field(default=7, ge=1, le=90, description='短期备注有效天数；长期偏好用kind=long_term，不设置到期时间。')
    category: Literal["note", "food_like", "food_dislike", "food_allergy", "training_state"] = "note"
    value: str = Field(min_length=1, max_length=1000)


class MealRecordArgs(StrictModel):
    slot: Literal['breakfast', 'lunch', 'dinner', 'snack']
    title: str = Field(min_length=1, max_length=80)
    kcal: int = Field(ge=0, le=3000)
    protein_g: int | float = Field(ge=0, le=300)
    note: str = Field(default='', max_length=300)

    @field_validator('title')
    @classmethod
    def nonblank_title(cls, value):
        if not value.strip(): raise ValueError('进食描述不能为空。')
        return value.strip()


class MealCorrectionArgs(StrictModel):
    slot: Literal['breakfast', 'lunch', 'dinner', 'snack'] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=80)
    kcal: int | None = Field(default=None, ge=0, le=3000)
    protein_g: int | float | None = Field(default=None, ge=0, le=300)
    note: str | None = Field(default=None, max_length=300)

    @field_validator('title')
    @classmethod
    def nonblank_title(cls, value):
        if value is not None and not value.strip(): raise ValueError('进食描述不能为空。')
        return value.strip() if value is not None else None

    @model_validator(mode='after')
    def has_change(self):
        if not self.model_dump(exclude_none=True): raise ValueError('请明确要更正的实际进食字段。')
        return self


@dataclass(frozen=True)
class Capability:
    capability_id: str
    domain: str
    description: str
    argument_model: type[StrictModel]
    target_types: tuple[str, ...]
    mutates_state: bool = True
    confirmation: str = "uncertain"
    executor: str = ""
    constraints: str = "必须选择快照中属于本用户的真实 ID，执行范围仅为目标对象；版本必须一致。"

    execution_boundary: str = "local_database"
    idempotency_policy: str = "request_key_and_verified_execution_record"
    verification_policy: str = "database_reload_and_scope_comparison"

    def public(self):
        return {"capability_id": self.capability_id, "domain": self.domain,
                "description": self.description, "input_schema": self.argument_model.model_json_schema(),
                "target_types": list(self.target_types), "mutates_state": self.mutates_state,
                "confirmation": self.confirmation, "constraints": self.constraints,
                "executor": self.executor,
                "scope_schema": {"target_types": list(self.target_types), "ownership": "authenticated_user", "version": "snapshot_revision"},
                "confirmation_policy": self.confirmation,
                "idempotency_policy": self.idempotency_policy,
                "verification_policy": self.verification_policy,
                "execution_boundary": self.execution_boundary,
                "binding_schema": {"resolution.selected_id": {"role": "destination_or_existing_object", "types": list(self.target_types)},
                                   **({"arguments.exercise_id": {"role": "source_catalog_exercise", "types": ["exercise"]}} if issubclass(self.argument_model, ExerciseArgs) else {}),
                                   **({"arguments.ingredient_id": {"role": "source_catalog_ingredient", "types": ["ingredient"]}} if issubclass(self.argument_model, FoodArgs) else {})}}


SPECS = [
    ('profile.add_allergies','profile','用户明确自报新增过敏原时加入长期档案，可多项，只做并集不删除原有项；同步修复长期食谱及未完成餐次，不新增餐次。假设、引述、否认过敏不用此能力。移除过敏须profile.update先确认。',AllergyAdditions,('profile',),True,'uncertain','profile'),
    ('nutrition.record_meal', 'nutrition_record', '登记某日期真实已吃的一餐，绑定day；餐次、描述、热量和蛋白必须有用户提供或已核对的估值。先展示确认，不从计划推断已经吃过，不把未知营养填0。', MealRecordArgs, ('day',), True, 'always', 'meal_record'),
    ('nutrition.correct_meal', 'nutrition_record', '修订本人真实进食记录，绑定meal_log；只填写用户要求修改的字段，先展示确认，不改计划餐食。', MealCorrectionArgs, ('meal_log',), True, 'always', 'meal_record'),
    ("workout.remove_action", "training", "移除指定日期的一个动作；热身也合法。", NoArgs, ("workout_action",), True, "uncertain", "action"),
    ("workout.skip_action", "training", "跳过某日动作（只从该日移除，不改长期计划）或标记当前训练条目 skipped。", NoArgs, ("workout_action", "session_item"), True, "uncertain", "action"),
    ("workout.update_action", "training", "调整目标组数/次数，支持相对变化。", ActionValues, ("workout_action", "session_item"), True, "uncertain", "action"),
    ("workout.replace_action", "training", "用目录动作替换一个动作，保留实例 ID。", ExerciseArgs, ("workout_action", "session_item"), True, "uncertain", "action"),
    ("workout.add_action", "training", "在指定日期新增一个目录动作。selected_id 必须是 day ID（添加目的地），exercise ID 只能填 arguments.exercise_id。", ExerciseArgs, ("day",), True, "uncertain", "action"),
    ("meal.add_food", "nutrition", "在一餐新增目录食材；保留原有食物。", FoodArgs, ("meal",), True, "uncertain", "food"),
    ("meal.remove_food", "nutrition", "从一餐移除一个具体食物实例。", NoArgs, ("food_item",), True, "uncertain", "food"),
    ("meal.replace_food", "nutrition", "将具体食物换成目录食材及份量。", FoodArgs, ("food_item",), True, "uncertain", "food"),
    ("meal.set_quantity", "nutrition", "调整具体食物克数，按原比例重算营养。", QuantityArgs, ("food_item",), True, "uncertain", "food"),
    ("profile.update", "profile", "更新长期档案，必须确认。除过敏更新会同步排除后续食谱冲突食材外，只更新档案值，不会自动重算训练负荷。", ProfileArgs, ("profile",), True, "always", "profile"),
    ("memory.create", "memory", "保存用户明确要求记住的备注或近期反馈。稳定长期偏好必须kind=long_term；本周/近期状态kind=short_term并填写retention_days。取消已保存备注用memory.archive，不补全旧create。", NoteArgs, ("profile",), True, "always", "memory"),
    ("memory.archive", "memory", "归档一个真实独立备注；canonical档案镜像不能单独归档，修改或清空长期字段应使用profile.update并先预览确认。", NoArgs, ("memory",), True, "always", "memory"),
    ("history.undo", "history", "仅补偿undo_supported=true的实际执行（动作、餐食、档案）；逐资源校验，无后续重叠更改才能恢复。记忆用archive，不撤销训练事实。", NoArgs, ("execution",), True, "always", "undo"),
    ("context.inspect", "read", "查看目标对象或日期的真实数据，不修改。", NoArgs, ("day", "workout_action", "meal", "food_item", "profile", "memory", "execution", "session", "session_item"), False, "never", "inspect"),
]
REGISTRY = {row[0]: Capability(*row) for row in SPECS}


def manifest():
    # Execution/recovery policy is consumed by orchestration, not natural-language
    # planning. Keep the pre-migration semantic menu stable and avoid duplicating
    # confirmation/scope metadata in every model request.
    execution_only = {"scope_schema", "confirmation_policy", "idempotency_policy", "verification_policy", "execution_boundary"}
    return [{key: value for key, value in c.public().items() if key not in execution_only}
            for c in REGISTRY.values()]


def capability(id):
    if id in REGISTRY:
        return REGISTRY[id]
    from app.agent.initial_plan_runtime import CAPABILITIES as INITIAL_CAPABILITIES
    if id in INITIAL_CAPABILITIES:
        return INITIAL_CAPABILITIES[id]
    from app.agent.workout_capabilities import CAPABILITIES as WORKOUT_CAPABILITIES
    if id in WORKOUT_CAPABILITIES:
        return WORKOUT_CAPABILITIES[id]
    from app.agent.day_plan_capabilities import CAPABILITIES as DAY_CAPABILITIES
    if id in DAY_CAPABILITIES:
        return DAY_CAPABILITIES[id]
    from app.agent.commerce_capabilities import CAPABILITIES
    return CAPABILITIES.get(id)
