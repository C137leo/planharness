"""ORM 模型（SQLAlchemy 2.0 Mapped 声明式风格）。

表结构对应 docs/03-domain-and-protocol.md：用户与画像、目标/约束账本、
计划与版本化动作、补丁与审批、打卡与周复盘、Agent 运行记录。

状态字段目前都用约定字符串（如 active / superseded / pending_approval），
P0 不建枚举表，保持迁移简单。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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


class WorkoutLog(Base):
    """实际训练记录：用户今天到底练了什么、多久。

    和 PlanAction 的区别：PlanAction 是"计划要练的"，WorkoutLog 是"真的练了的"。
    热量只能从这条记录算，不能拿计划反推。
    """

    __tablename__ = "workout_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    action_ref: Mapped[str] = mapped_column(String(64))
    # strength / cardio / flexibility，用于分组统计
    kind: Mapped[str] = mapped_column(String(16), default="strength")
    duration_min: Mapped[int] = mapped_column(Integer, default=0)
    sets: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    intensity: Mapped[str] = mapped_column(String(16), default="normal")
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


class ContextSnapshot(Base):
    """一次「上下文装配」的快照：说清楚这次到底喂给模型看了什么。

    解决的问题：模型答得不对时，光看输入输出复现不了 —— 因为上下文是动态裁剪的。
    把「保留了哪些 section、各占多少 token、哪些被挤掉、是否压缩过」落库，
    才能事后回答「它当时是不是压根没看到膝盖疼这条约束」。

    属于 P1 上下文 Harness 的记账表；P0 只建表，暂不写入。
    """

    __tablename__ = "context_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 属于哪一次 Agent 运行；与 AgentRun.context_snapshot_id 互指（那边是后填的指针）
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_runs.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)

    # 这次装配服务于哪个任务：explain_patch / plan_generation（与 AgentRun.kind 对齐）
    purpose: Mapped[str] = mapped_column(String(32))
    # [{name, tokens, included}] —— 被纳入上下文的片段及其 token 开销
    sections: Mapped[list] = mapped_column(JSON, default=list)
    # 实际用量与预算上限；token_count > token_budget 说明发生了裁剪
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    token_budget: Mapped[int] = mapped_column(Integer, default=0)
    # [{name, tokens, reason}] —— 被预算挤掉的片段，reason 说明是超限还是无关
    dropped: Mapped[list] = mapped_column(JSON, default=list)
    compressed: Mapped[bool] = mapped_column(Boolean, default=False)
    # compression_method: truncate / summarize / extract；未压缩时为 None
    compression_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class AuditEvent(Base):
    """审计事件（哈希链）：每条事件的 hash 都把上一条的 hash 算进去。

    为什么不用「只记日志」：日志可以被删、被改，而且没人能证明它没被动过。
    这里把 (seq, payload_hash, prev_hash) 一起哈希，任何一处改动都会让后续链路对不上。

    stream_id 用 plan_id：patches / approvals / rollback 都是围绕一份计划发生的，
    一条流就能回答「这份计划被人动过什么」。
    """

    __tablename__ = "audit_events"
    __table_args__ = (UniqueConstraint("stream_id", "seq", name="uq_audit_seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    stream_id: Mapped[str] = mapped_column(String(36), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    # actor_type: user / agent / system
    actor_type: Mapped[str] = mapped_column(String(16), default="agent")
    actor_id: Mapped[str] = mapped_column(String(36), default="")
    # action: patch_created / approval_granted / approval_rejected /
    #         patch_committed / verification_failed / rollback_committed
    action: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class PatchRequest(Base):
    """用户的一次「改计划」请求（自然语言 + 结构化意图）。

    和 PlanPatch 的分工：
    - PatchRequest：用户说了什么、解析成什么意图、处在哪一步（clarifying/ready/applied）；
    - PlanPatch：具体要改哪几个槽位（草案/已提交都在这里）。

    一次请求最多 3 轮（parent_request_id 串起来），防无限返工。
    """

    __tablename__ = "patch_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    plan_id: Mapped[str] = mapped_column(String(36), ForeignKey("plans.id"), index=True)
    base_version: Mapped[int] = mapped_column(Integer)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    quick_action: Mapped[str] = mapped_column(String(32), default="")
    # intent: {kind, body_parts, target_body_parts, available_days, duration_min, notes}
    intent: Mapped[dict] = mapped_column(JSON, default=dict)
    # status: clarifying / ready / applied / rejected / invalid
    status: Mapped[str] = mapped_column(String(16), default="clarifying")
    # 需要追问时给用户看的问题
    question: Mapped[str] = mapped_column(Text, default="")
    # 指向草案补丁
    patch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    parent_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    round: Mapped[int] = mapped_column(Integer, default=1)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PatchFeedback(Base):
    """用户对草案的「再改改」反馈。"""

    __tablename__ = "patch_feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(String(36), ForeignKey("patch_requests.id"), index=True)
    user_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
