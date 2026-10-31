from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DemoLoginIn(BaseModel):
    user_id: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str


class ProfileOut(BaseModel):
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
    plan_id: str
    status: str
    version: int
    calorie_target: float
    protein_target_g: float
    step_target: int
    summary: str
    actions: list[ActionOut]


class WeChatLoginIn(BaseModel):
    code: str
