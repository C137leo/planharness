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


class CurrentPlanOut(BaseModel):
    """当前生效版本的计划：版本号 + 营养目标 + 全部动作。"""

    plan_id: str
    status: str
    version: int
    calorie_target: float
    protein_target_g: float
    step_target: int
    summary: str
    actions: list[ActionOut]


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
    phone: str = Field(min_length=6, max_length=32)
    password: str = Field(min_length=8, max_length=72)
    nickname: str = Field(default="新用户", max_length=64)

class LoginIn(BaseModel):
    phone: str
    password: str

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
