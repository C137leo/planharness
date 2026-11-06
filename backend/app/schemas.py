"""接口出入参模型（Pydantic v2）。

约定：
- *In 是请求体，只暴露演示所需的最小字段；
- *Out 是响应体，from_attributes=True 表示可直接由 ORM 对象校验生成；
- 响应外层统一包在 {code, message, data, trace_id} 里，见 api/routes.ok()。
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DemoLoginIn(BaseModel):
    """demo 登录入参；不传 user_id 时登录预置演示账号。"""

    user_id: str | None = None


class TokenOut(BaseModel):
    """登录响应：JWT + 令牌类型 + 用户 ID。"""

    access_token: str
    token_type: str = "bearer"
    user_id: str


class ProfileOut(BaseModel):
    """用户画像出参，字段与 Profile 表保持一致。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    sex: str
    birth_year: int
    height_cm: float
    weight_kg: float
    activity_level: str
    training_experience: str
    equipment: list[str] = Field(default_factory=list)
    weekly_days: int
    session_minutes: int
    diet_pattern: str
    goal: str = "fat_loss"
    goal_note: str = ""
    diet_preferences: list[str] = Field(default_factory=list)
    allergies: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    timezone: str


class ActionOut(BaseModel):
    """单个动作槽位出参（训练/餐食共用），不含内部 meta 细节。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    day_index: int
    slot: str
    seq: int
    slot_key: str
    action_type: str
    action_ref: str
    sets: int | None = None
    reps: int | None = None
    duration_min: int | None = None
    intensity: str | None = None
    portion_g: float | None = None
    kcal: int | None = None
    # 动作/食材的中文名、器械、RIR 等附加信息，前端直接展示
    meta: dict = Field(default_factory=dict)


class MealItemOut(BaseModel):
    name: str
    grams: float
    kcal: int
    protein: float = 0
    fat: float = 0
    carb: float = 0


class MealPlanOut(BaseModel):
    """一餐：吃什么、多少克、多少热量。过敏原已在生成时排除。"""

    model_config = ConfigDict(from_attributes=True)

    day_index: int
    slot: str
    title: str
    items: list[dict] = Field(default_factory=list)
    kcal: int = 0
    protein_g: float = 0
    fat_g: float = 0
    carb_g: float = 0


class TodayOut(BaseModel):
    """今天的安排：练什么 + 吃什么。"""

    weekday: int
    date: str
    actions: list[ActionOut] = Field(default_factory=list)
    meals: list[MealPlanOut] = Field(default_factory=list)


class CurrentPlanOut(BaseModel):
    """当前生效版本的计划：版本号 + 营养目标 + 动作 + 每日餐食 + 今日安排。"""

    plan_id: str
    status: str
    version: int
    calorie_target: float
    protein_target_g: float
    step_target: int
    summary: str
    source: str = "template"
    nutrition: dict = Field(default_factory=dict)
    program: dict = Field(default_factory=dict)
    actions: list[ActionOut]
    meals: list[MealPlanOut] = Field(default_factory=list)
    today: TodayOut | None = None


class WeChatLoginIn(BaseModel):
    """微信登录入参：小程序 wx.login 拿到的一次性 code。"""

    code: str


class PatchCreateIn(BaseModel):
    """创建补丁入参；P0 只实现膝痛场景，available_days 表示本周可训练天数。"""

    reason_code: str = "injury"
    available_days: int = 2
    idempotency_key: str | None = None


class PatchOut(BaseModel):
    """补丁出参：操作列表、校验结果与 diff 指标一并返回，供 Console 展示。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    plan_id: str
    base_version: int
    reason_code: str
    reason_text: str
    status: str
    operations: list[dict]
    validation: dict
    diff: dict
    created_at: datetime


class ApprovalIn(BaseModel):
    """审批/驳回入参，只带一句人工备注。"""

    comment: str = ""


class RollbackIn(BaseModel):
    """回滚入参：回滚到哪个版本号。"""

    target_version: int


class CheckinIn(BaseModel):
    date: date_type | None = None
    weight_kg: float | None = None
    sleep_hours: float | None = None
    hunger_level: int | None = Field(default=None, ge=1, le=5)
    soreness_level: int | None = Field(default=None, ge=1, le=5)
    adherence_pct: int | None = Field(default=None, ge=0, le=100)
    note: str = ""


class CheckinOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    date: date_type
    weight_kg: float | None
    sleep_hours: float | None
    hunger_level: int | None
    soreness_level: int | None
    adherence_pct: int | None
    note: str

class RegisterIn(BaseModel):
    """注册入参。

    多出来的三个字段都是防机器注册用的：
    - challenge_id / challenge_token：服务端签发的票据，用来证明"确实打开过页面"；
    - company：蜜罐字段，正常用户看不见也不会填，脚本往往会把所有 input 填满。
    """

    phone: str = Field(min_length=1, max_length=32)
    # 长度策略放在 abuse.password_problem 里，这样错误信息是中文、口径统一
    password: str = Field(min_length=1, max_length=200)
    nickname: str = Field(default="新用户", max_length=64)
    challenge_id: str = Field(default="", max_length=64)
    challenge_token: str = Field(default="", max_length=128)
    company: str = Field(default="", max_length=64)


class LoginIn(BaseModel):
    phone: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=200)
    challenge_id: str = Field(default="", max_length=64)
    challenge_token: str = Field(default="", max_length=128)


class ChallengeOut(BaseModel):
    """一次性校验票据。token 由服务端 HMAC 签发，客户端只负责原样带回。"""

    challenge_id: str
    token: str
    expires_in: int
    min_fill_ms: int


class MeOut(BaseModel):
    user_id: str
    # demo 账号没有手机号，允许为空字符串（不要把"没有"变成 500）
    phone: str = ""
    nickname: str = ""
    goal: str = ""
    has_profile: bool = False

class TraceEntryOut(BaseModel):
    """trace 里的一步：哪个节点、什么状态、什么时候。"""

    node: str
    status: str
    ts: str
    detail: dict | None = None


class RunSummaryOut(BaseModel):
    """run 列表项：够 Console 列表用即可。"""

    id: str
    kind: str
    mode: str
    status: str
    stop_reason: str | None = None
    patch_id: str | None = None
    plan_id: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_cny: float = 0.0
    error_code: str | None = None


class RunDetailOut(RunSummaryOut):
    """run 详情：trace + 补丁 diff + 上下文快照 + 验证结论。"""

    trace: list[TraceEntryOut] = Field(default_factory=list)
    input: dict = Field(default_factory=dict)
    output: dict = Field(default_factory=dict)
    patch: dict | None = None
    context: dict | None = None
    verification: dict | None = None


class PatchRequestIn(BaseModel):
    """一句话改计划。base_version 传了就做乐观锁校验。"""

    text: str = Field(min_length=1, max_length=500)
    quick_action: str = ""
    base_version: int | None = None


class PatchFeedbackIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class WorkoutLogIn(BaseModel):
    """打卡里的训练记录；时长必填，热量由后端按 MET 算。"""

    date: date_type | None = None
    action_ref: str = Field(min_length=1, max_length=64)
    kind: Literal["strength", "cardio", "flexibility"] = "strength"
    duration_min: int = Field(ge=1, le=300)
    sets: int | None = Field(default=None, ge=0, le=50)
    reps: int | None = Field(default=None, ge=0, le=200)
    intensity: Literal["light", "normal", "hard"] = "normal"
    note: str = ""


class ProfileUpdateIn(BaseModel):
    """画像录入：基础信息 + 目标 + 饮食禁忌。

    字段都可选，传什么改什么；没传的保持原值（注册时的默认画像不会被清空）。
    """

    sex: Literal["male", "female"] | None = None
    birth_year: int | None = Field(default=None, ge=1940, le=2012)
    height_cm: float | None = Field(default=None, ge=120, le=230)
    weight_kg: float | None = Field(default=None, ge=30, le=250)
    goal: Literal["fat_loss", "muscle_gain", "cardio", "health"] | None = None
    goal_note: str | None = Field(default=None, max_length=500)
    weekly_days: int | None = Field(default=None, ge=1, le=7)
    session_minutes: int | None = Field(default=None, ge=10, le=180)
    activity_level: Literal["sedentary", "light", "moderate", "active"] | None = None
    training_experience: Literal["beginner", "intermediate", "advanced"] | None = None
    equipment: list[str] | None = None
    diet_pattern: Literal["omnivore", "vegetarian", "vegan", "pescatarian"] | None = None
    # 过敏原会直接喂给安全门与食谱筛选
    allergies: list[str] | None = None
    diet_preferences: list[str] | None = None
    dislikes: list[str] | None = None


class PlanDraftIn(BaseModel):
    """生成计划：一句原话 + 可选的执行模式。"""

    instruction: str = Field(default="", max_length=1000)
    parent_draft_id: str | None = None


class DraftFeedbackIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class PatchPresetOut(BaseModel):
    """预设的改动入口：用户点一下就知道能说什么。"""

    key: str
    label: str
    example: str
    hint: str
