"""ORM 模型（SQLAlchemy 2.0 Mapped 声明式风格）。

表结构对应 docs/03-domain-and-protocol.md：用户与画像、目标/约束账本、
计划与版本化动作、补丁与审批、打卡与周复盘、Agent 运行记录。

状态字段目前都用约定字符串（如 active / superseded / pending_approval），
P0 不建枚举表，保持迁移简单。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def new_id() -> str:
    """主键生成器：统一用 UUID4 字符串，方便多端/离线场景避免自增冲突。"""
    return str(uuid4())


def utcnow() -> datetime:
    """时间戳统一存 UTC，展示层再按用户时区转换。"""
    return datetime.now(timezone.utc)


class User(Base):
    """用户账号。

    demo 登录直接使用预置用户；微信登录接入后用 openid/unionid 关联同一账号。
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    openid: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    unionid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    nickname: Mapped[str] = mapped_column(String(64), default="demo")
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # role 预留给 Console 的运营/审核身份（user / admin）
    role: Mapped[str] = mapped_column(String(16), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    phone: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 微信用户可为空
    # 改密码后 +1，让所有旧 token 立即失效（有了它才谈得上"撤销"）
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class Profile(Base):
    """用户身体画像与训练偏好，是目标计算与计划生成的唯一输入。"""

    __tablename__ = "profiles"

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), primary_key=True)
    sex: Mapped[str] = mapped_column(String(16), default="male")
    birth_year: Mapped[int] = mapped_column(Integer, default=1997)
    height_cm: Mapped[float] = mapped_column(Float, default=178.0)
    weight_kg: Mapped[float] = mapped_column(Float, default=78.0)
    activity_level: Mapped[str] = mapped_column(String(16), default="moderate")
    training_experience: Mapped[str] = mapped_column(String(16), default="beginner")
    # 可用器械清单（JSON 数组），决定动作降级策略
    equipment: Mapped[list] = mapped_column(JSON, default=list)
    weekly_days: Mapped[int] = mapped_column(Integer, default=3)
    session_minutes: Mapped[int] = mapped_column(Integer, default=45)
    diet_pattern: Mapped[str] = mapped_column(String(16), default="omnivore")
    # 过敏原/忌口都会参与餐食筛选
    allergies: Mapped[list] = mapped_column(JSON, default=list)
    dislikes: Mapped[list] = mapped_column(JSON, default=list)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Goal(Base):
    """目标账本：P0 只支持减脂（fat_loss），保留字段给后续目标类型。"""

    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(24), default="fat_loss")
    target_weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_rate_kg_per_week: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=1)
    # 只有 active 的目标会参与生成计划
    status: Mapped[str] = mapped_column(String(16), default="active")
    # source 区分目标来自用户自述还是系统建议
    source: Mapped[str] = mapped_column(String(16), default="user")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Constraint(Base):
    """约束账本（P1 上下文 Harness 的落点）：一条可追溯、可失效的硬/软约束。"""

    __tablename__ = "constraints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(24))
    # code 是稳定标识（如 knee_pain），value 存结构化参数
    code: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    # hard 会被安全门强制校验，soft 只影响推荐
    severity: Mapped[str] = mapped_column(String(8), default="soft")
    source: Mapped[str] = mapped_column(String(16), default="user")
    status: Mapped[str] = mapped_column(String(16), default="active")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Plan(Base):
    """一份长期计划；具体内容按版本存放在 PlanVersion，便于回滚与审计。"""

    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    goal_id: Mapped[str] = mapped_column(String(36), ForeignKey("goals.id"))
    status: Mapped[str] = mapped_column(String(16), default="active")
    # 指向 PlanVersion.version，是"当前生效版本"的唯一真相
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanVersion(Base):
    """计划版本：只新增、不覆盖，被替换的版本标 superseded 以保留历史。"""

    __tablename__ = "plan_versions"
    __table_args__ = (UniqueConstraint("plan_id", "version", name="uq_plan_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(String(36), ForeignKey("plans.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    # parent_version 形成版本链，回滚/追溯靠它串起来
    parent_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 由哪个补丁产生；模板生成或回滚产生的版本为空
    created_from_patch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    calorie_target: Mapped[float] = mapped_column(Float)
    protein_target_g: Mapped[float] = mapped_column(Float)
    step_target: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanAction(Base):
    """计划里的单个动作槽位（训练或餐食），是补丁操作与 diff 的最小粒度。"""

    __tablename__ = "plan_actions"
    # 同一版本内 slot_key 唯一，补丁用它精确定位被改动的槽位
    __table_args__ = (UniqueConstraint("plan_version_id", "slot_key", name="uq_plan_slot"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plan_versions.id"), index=True
    )
    day_index: Mapped[int] = mapped_column(Integer)
    slot: Mapped[str] = mapped_column(String(32))
    seq: Mapped[int] = mapped_column(Integer, default=1)
    # slot_key 约定为 "day:slot:seq"（如 1:strength:1）
    slot_key: Mapped[str] = mapped_column(String(64))
    # action_type: exercise / meal；action_ref 指向动作或餐食模板
    action_type: Mapped[str] = mapped_column(String(16), default="exercise")
    action_ref: Mapped[str] = mapped_column(String(64))
    sets: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    intensity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    portion_g: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 列名用 metadata（保留字），属性名 meta 避免与 SQLAlchemy 的 MetaData 冲突
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)


class PlanPatch(Base):
    """最小变更补丁：先落库为待审批草稿，审批通过才生成新版本。"""

    __tablename__ = "plan_patches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(String(36), ForeignKey("plans.id"), index=True)
    # base_version 记录补丁基于哪个版本，审批时用它检测计划是否已变更
    base_version: Mapped[int] = mapped_column(Integer)
    goal_id: Mapped[str] = mapped_column(String(36))
    constraint_ids: Mapped[list] = mapped_column(JSON, default=list)
    reason_code: Mapped[str] = mapped_column(String(24))
    reason_text: Mapped[str] = mapped_column(Text, default="")
    # operations 是受控 DSL（remove_action / replace_action），不存任意代码
    operations: Mapped[list] = mapped_column(JSON, default=list)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    diff: Mapped[dict] = mapped_column(JSON, default=dict)
    # status: draft → pending_approval → committed / rejected
    status: Mapped[str] = mapped_column(String(24), default="draft")
    requires_approval: Mapped[bool] = mapped_column(default=True)
    # 唯一键防止客户端重试造成重复补丁
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    created_by: Mapped[str] = mapped_column(String(16), default="agent")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Approval(Base):
    """人工审批记录，只追加不修改，作为"谁在何时放行/驳回"的审计证据。"""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    patch_id: Mapped[str] = mapped_column(String(36), ForeignKey("plan_patches.id"), index=True)
    actor_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    # decision: approved / rejected
    decision: Mapped[str] = mapped_column(String(16))
    comment: Mapped[str] = mapped_column(Text, default="")
    # 预留：一次性审批链接的 token 摘要
    token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Checkin(Base):
    """每日打卡，是周复盘与补丁触发信号（疼痛/依从性）的数据来源。"""

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
    """周复盘：趋势结论 + 下一步建议，仍走 draft→确认的状态机。"""

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
    """一次 Agent 运行的观测记录：输入、输出、trace、token 与耗时，供 Console 展示。"""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    # kind 区分任务类型（generate_plan / knee_patch ...），mode 区分 A/B/C 对比模式
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
