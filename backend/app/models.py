from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    openid: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    unionid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    nickname: Mapped[str] = mapped_column(String(64), default="demo")
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Profile(Base):
    __tablename__ = "profiles"

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    sex: Mapped[str] = mapped_column(String(16), default="male")
    birth_year: Mapped[int] = mapped_column(Integer, default=1997)
    height_cm: Mapped[float] = mapped_column(Float, default=178.0)
    weight_kg: Mapped[float] = mapped_column(Float, default=78.0)
    activity_level: Mapped[str] = mapped_column(String(16), default="moderate")
    training_experience: Mapped[str] = mapped_column(String(16), default="beginner")
    equipment: Mapped[list] = mapped_column(JSON, default=list)
    weekly_days: Mapped[int] = mapped_column(Integer, default=3)
    session_minutes: Mapped[int] = mapped_column(Integer, default=45)
    diet_pattern: Mapped[str] = mapped_column(String(16), default="omnivore")
    allergies: Mapped[list] = mapped_column(JSON, default=list)
    dislikes: Mapped[list] = mapped_column(JSON, default=list)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(24), default="fat_loss")
    target_weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_rate_kg_per_week: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")
    source: Mapped[str] = mapped_column(String(16), default="user")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Constraint(Base):
    __tablename__ = "constraints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(24))
    code: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    severity: Mapped[str] = mapped_column(String(8), default="soft")
    source: Mapped[str] = mapped_column(String(16), default="user")
    status: Mapped[str] = mapped_column(String(16), default="active")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    goal_id: Mapped[str] = mapped_column(String(36), ForeignKey("goals.id"))
    status: Mapped[str] = mapped_column(String(16), default="active")
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanVersion(Base):
    __tablename__ = "plan_versions"
    __table_args__ = (UniqueConstraint("plan_id", "version", name="uq_plan_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(String(36), ForeignKey("plans.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_from_patch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    calorie_target: Mapped[float] = mapped_column(Float)
    protein_target_g: Mapped[float] = mapped_column(Float)
    step_target: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanAction(Base):
    __tablename__ = "plan_actions"
    __table_args__ = (UniqueConstraint("plan_version_id", "slot_key", name="uq_plan_slot"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plan_versions.id"), index=True
    )
    day_index: Mapped[int] = mapped_column(Integer)
    slot: Mapped[str] = mapped_column(String(32))
    seq: Mapped[int] = mapped_column(Integer, default=1)
    slot_key: Mapped[str] = mapped_column(String(64))
    action_type: Mapped[str] = mapped_column(String(16), default="exercise")
    action_ref: Mapped[str] = mapped_column(String(64))
    sets: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    intensity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    portion_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)


class PlanPatch(Base):
    __tablename__ = "plan_patches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(String(36), ForeignKey("plans.id"), index=True)
    base_version: Mapped[int] = mapped_column(Integer)
    goal_id: Mapped[str] = mapped_column(String(36))
    constraint_ids: Mapped[list] = mapped_column(JSON, default=list)
    reason_code: Mapped[str] = mapped_column(String(24))
    reason_text: Mapped[str] = mapped_column(Text, default="")
    operations: Mapped[list] = mapped_column(JSON, default=list)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    diff: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    requires_approval: Mapped[bool] = mapped_column(default=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    created_by: Mapped[str] = mapped_column(String(16), default="agent")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patch_id: Mapped[str] = mapped_column(String(36), ForeignKey("plan_patches.id"), index=True)
    actor_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String(16))
    comment: Mapped[str] = mapped_column(Text, default="")
    token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Checkin(Base):
    __tablename__ = "checkins"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_checkin_day"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    sleep_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    hunger_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    soreness_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    adherence_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    plan_id: Mapped[str] = mapped_column(String(36), ForeignKey("plans.id"))
    week_index: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    trend: Mapped[dict] = mapped_column(JSON, default=dict)
    recommendation: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(16), default="patch")
    scenario_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="running")
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    trace: Mapped[list] = mapped_column(JSON, default=list)
    context_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
