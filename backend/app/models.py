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
    Index,
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
    # 训练目标：fat_loss / muscle_gain / cardio / health（对应 skill 的 减脂/增肌/心肺/综合健康）
    goal: Mapped[str] = mapped_column(String(24), default="fat_loss")
    # 用户原话，保留下来给模型和安全门看，不参与枚举判断
    goal_note: Mapped[str] = mapped_column(Text, default="")
    # 饮食偏好（素食/不吃牛肉…），和 allergies 一起决定食谱候选集
    diet_preferences: Mapped[list] = mapped_column(JSON, default=list)
    # 过敏原/忌口都会参与餐食筛选
    allergies: Mapped[list] = mapped_column(JSON, default=list)
    # 长期喜恶：Profile 是结构化事实的唯一来源，Memory 只做语义镜像
    dislikes: Mapped[list] = mapped_column(JSON, default=list)
    likes: Mapped[list] = mapped_column(JSON, default=list)
    # 长期训练时间 / 地点偏好（结构化，供 skill 与 Agent 共同读取）
    preferred_time_window: Mapped[str] = mapped_column(String(24), default="")
    preferred_time_label: Mapped[str] = mapped_column(String(32), default="")
    preferred_location: Mapped[str] = mapped_column(String(24), default="")
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
    # 用户原话（"我想 3 个月瘦 8 公斤"），审计时能看到目标从哪来
    raw_text: Mapped[str] = mapped_column(Text, default="")
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
    # template = 内置模板；coach = skill 生成（训练+食谱+营养）
    source: Mapped[str] = mapped_column(String(16), default="template")
    # skill 的营养目标（BMR/TDEE/目标热量/宏量区间），按版本会变，所以存在计划上
    nutrition: Mapped[dict] = mapped_column(JSON, default=dict)
    # skill 的训练结构（structure/days/minutes/prescription），用于展示与复盘
    program: Mapped[dict] = mapped_column(JSON, default=dict)
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
    # 餐食估算热量（skill 食谱给出）；训练动作为空
    kcal: Mapped[int | None] = mapped_column(Integer, nullable=True)
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


class MealPlan(Base):
    """一天的餐食安排：由 skill 的食物库按目标宏量分配，过敏原已在生成时排除。

    和 PlanAction 的分工：PlanAction 存"这一天有哪些动作/餐次槽位"，
    MealPlan 存"这一餐具体吃什么、多少克、多少热量"。分表是因为一条记录
    对应 2~4 个食材，塞进一行 JSON 之外还想按热量聚合。
    """

    __tablename__ = "meal_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    plan_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plan_versions.id"), index=True
    )
    day_index: Mapped[int] = mapped_column(Integer)
    slot: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(32))
    # [{name, grams, kcal, protein, fat, carb}]
    items: Mapped[list] = mapped_column(JSON, default=list)
    kcal: Mapped[int] = mapped_column(Integer, default=0)
    protein_g: Mapped[float] = mapped_column(Float, default=0.0)
    fat_g: Mapped[float] = mapped_column(Float, default=0.0)
    carb_g: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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


class PlanDraft(Base):
    """生成出来的计划草案：先看、再改（≤3 轮）、最后确认才落版本。

    为什么单独建表而不是直接写 PlanVersion：草案要能整份丢弃，
    而版本表是 append-only 的审计对象，两者生命周期不同。
    """

    __tablename__ = "plan_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    base_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    instruction: Mapped[str] = mapped_column(Text, default="")
    # 每轮反馈原话，复盘"为什么改了 3 轮"
    feedback: Mapped[list] = mapped_column(JSON, default=list)
    # 完整提案：{program, nutrition, meals, days, warnings, source}
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    round: Mapped[int] = mapped_column(Integer, default=1)
    # blocked / needs_input 时给用户看的原话
    question: Mapped[str] = mapped_column(Text, default="")
    # 生成链路的技术信息：工具调用序列、降级原因、token/cost
    trace: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeChunk(Base):
    """健康知识条目：动作要领 / 安全 / 恢复 / 营养。

    每条必须能溯源（source_title + source_url）；没有出处的回答不允许进库。
    embedding 暂时留空，后续要上向量检索再填。
    """

    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    # knee / shoulder / back / core / legs / chest / general
    body_part: Mapped[str] = mapped_column(String(24), index=True, default="general")
    # technique / safety / recovery / nutrition
    topic: Mapped[str] = mapped_column(String(24), index=True, default="technique")
    content: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    source_title: Mapped[str] = mapped_column(String(160))
    source_url: Mapped[str] = mapped_column(String(300))
    version: Mapped[int] = mapped_column(Integer, default=1)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)


class FoodOption(Base):
    """某一餐的 3 个"换一换"推荐（含平台搜索深链）。

    为什么不存"推荐菜库"：变体是从 FOODS + 当前这一餐算出来的，
    存下来只是缓存（同一版本同一天同一餐不必重复计算）。
    """

    __tablename__ = "food_options"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    plan_id: Mapped[str] = mapped_column(String(36), ForeignKey("plans.id"), index=True)
    plan_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("plan_versions.id"), index=True)
    plan_date: Mapped[date] = mapped_column(Date)
    meal_slot: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(64))
    # [{name, grams, kcal, protein, fat, carb}]
    items: Mapped[list] = mapped_column(JSON, default=list)
    kcal: Mapped[int] = mapped_column(Integer, default=0)
    protein_g: Mapped[float] = mapped_column(Float, default=0.0)
    fat_g: Mapped[float] = mapped_column(Float, default=0.0)
    carb_g: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    # {jd, taobao, meituan}
    platform_links: Mapped[dict] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(16), default="catalog")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthChallenge(Base):
    """注册/登录前的一次性校验票据。

    客户端先申请票据，再带着票据提交；服务端校验签名、有效期、是否已用，
    以及"从签发到提交"的耗时是否过短（机器提交通常只有几十毫秒）。
    过期行会被限流器顺手清理，表不会无限增长。
    """

    __tablename__ = "auth_challenges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # register / login
    purpose: Mapped[str] = mapped_column(String(16))
    ip: Mapped[str] = mapped_column(String(64), default="")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RateLimitBucket(Base):
    """固定窗口计数桶：key 里带窗口起点，过期窗口由清理任务删除。

    主键是 "scope:identifier@window_start"，所以同一键在不同窗口是不同的行，
    不需要额外的窗口比较逻辑，也不会出现"跨窗口累加"的问题。
    """

    __tablename__ = "rate_limit_buckets"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[int] = mapped_column(Integer, index=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


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


class Memory(Base):
    """Agent 的分层记忆。

    - kind=long_term：稳定偏好（目标 / 训练时间 / 地点 / 器械 / 喜恶）；
      会影响后续计划生成与今日安排。
    - kind=short_term：近期状态（最近几次完成情况、难度反馈、疲劳）；
      只影响最近几次调整，不写进用户画像。

    临时要求（今天只有 20 分钟）不写这里，而是写 TemporaryContext。

    同一个 (user_id, kind, category, key) 只保留一条 active 记录：
    用户改主意时更新而不是无限追加，避免"既喜欢午休又喜欢晚上"的自相矛盾。
    """

    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    # long_term / short_term
    kind: Mapped[str] = mapped_column(String(16), default="long_term", index=True)
    # goal / schedule / location / equipment / like / dislike / experience /
    # session_minutes / weekly_days / training_state / session / note
    category: Mapped[str] = mapped_column(String(32), index=True)
    # 同类记忆里的稳定标识（如 dislike:深蹲、schedule:preferred_time）
    key: Mapped[str] = mapped_column(String(64), default="value")
    value: Mapped[str] = mapped_column(Text, default="")
    # 结构化参数：时间段、器械列表、最近完成率等，前端与生成器都直接读
    structured: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(24), default="chat")
    evidence: Mapped[str] = mapped_column(Text, default="")
    # confidence = 对"这条记忆是否真实"的把握；importance = 对用户有多重要
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    # 最近一次被用户明确确认/复述的时间（长期记忆不会因为时间久而失效）
    last_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 只有 short_term 会设置；long_term 默认 None，直到用户修改
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # active / archived（用户删除）/ expired（生命周期到期）/ superseded（被新值取代）
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "kind", "category", "key", name="uq_memory_key"),
        Index("ix_memory_user_kind_status", "user_id", "kind", "status"),
        Index("ix_memory_expiry", "kind", "status", "expires_at"),
    )


class MemoryRevision(Base):
    """记忆变更记录：改值、归档、过期、取代都留旧值，便于追溯与解释。

    这不是完整审计链（链在 AuditEvent），而是"这条记忆是怎么变的"的人类可读历史。
    """

    __tablename__ = "memory_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    memory_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    category: Mapped[str] = mapped_column(String(32), default="")
    key: Mapped[str] = mapped_column(String(64), default="")
    old_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
    old_status: Mapped[str] = mapped_column(String(16), default="")
    new_status: Mapped[str] = mapped_column(String(16), default="")
    source: Mapped[str] = mapped_column(String(24), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TemporaryContext(Base):
    """只对某一天有效的临时上下文。

    典型内容：今天只有 20 分钟 / 不去健身房 / 器械被占用 / 今天想休息。
    它不覆盖长期记忆，也不写 PlanVersion；一天一条 key，过期即失效。
    """

    __tablename__ = "temporary_contexts"
    __table_args__ = (
        UniqueConstraint("user_id", "day", "key", name="uq_temp_context_day_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    # day 是旧字段（保留兼容）；effective_date 是业务口径，按用户时区的本地日期
    # 新数据以 effective_date 为准；day 保留给旧库兼容，允许为空
    day: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    text: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(24), default="chat")
    # active / completed / reset / expired
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("user_id", "day", "key", name="uq_temp_context_day_key"),
        Index("ix_temp_user_status_date", "user_id", "status", "effective_date"),
    )


class PlanAdjustment(Base):
    """当天有效计划的调整记录（DailyPlan 的输入，不是长期 PlanVersion）。

    - PlanVersion：长期训练周期，append-only；
    - PlanAdjustment：某一天的临时调整，可追溯、可重置、可恢复；
    - DailyPlan：运行时由 base PlanVersion + active PlanAdjustment 组合出的有效视图。

    旧实现把 override 塞在 TemporaryContext 里；这里升级为独立实体，
    避免"环境上下文"和"计划调整"混在一张表，也让计划页能显示调整标识。
    """

    __tablename__ = "plan_adjustments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    base_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    # active / superseded / reset / expired
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    constraints: Mapped[dict] = mapped_column(JSON, default=dict)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    changes: Mapped[list] = mapped_column(JSON, default=list)
    borrowed_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    restored_from_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("ix_adjustment_user_date_status", "user_id", "effective_date", "status"),
    )


class AgentMessage(Base):
    """统一 Agent 的对话消息（短期会话记忆）。

    只存"用户说了什么 + Agent 做了什么"的摘要，不存完整 trace（trace 在 AgentRun）。
    换页面后仍能读到同一段对话，这是"同一个 Agent"的前提。
    """

    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    # 目前只有一个线程；留字段方便以后拆"计划教练 / 营养教练"
    thread: Mapped[str] = mapped_column(String(32), default="coach", index=True)
    # user / agent
    role: Mapped[str] = mapped_column(String(8), default="user")
    text: Mapped[str] = mapped_column(Text, default="")
    intent: Mapped[dict] = mapped_column(JSON, default=dict)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    # 这条回复当时可展开的完整结果（计划草案/今日安排/动作指导/训练会话等）。
    # 切页面或重新加载后，历史附件还能点开查看，不只保留一行文字。
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkoutSession(Base):
    """一次训练的执行状态（从点「开始训练」到提交反馈）。

    为什么不复用 WorkoutLog：WorkoutLog 是"练完的事实"，一条一个动作；
    这里要保存"练到哪一组了 / 哪些动作被替换过"，是执行过程，生命周期不同。
    只有 finish 时才把已完成的部分写成 WorkoutLog。

    items 是 JSON 列表，每个元素：
    {id, slot_key, action_ref, name, slot, target_sets, target_reps,
     duration_min, rest_sec, completed_sets, status, replaced_from, reason,
     guidance, meta}
    """

    __tablename__ = "workout_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    plan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # active / paused / completed / finished / abandoned
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # plan / agent / feedback
    source: Mapped[str] = mapped_column(String(16), default="plan")
    current_index: Mapped[int] = mapped_column(Integer, default=0)
    items: Mapped[list] = mapped_column(JSON, default=list)
    planned_minutes: Mapped[int] = mapped_column(Integer, default=0)
    actual_minutes: Mapped[int] = mapped_column(Integer, default=0)
    completion_pct: Mapped[float] = mapped_column(Float, default=0.0)
    difficulty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fatigue: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    agent_log: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TodayAgentPlan(Base):
    """某一天的 Agent 规划结果快照：和这一天绑定，重开页面还能看到同一份。

    和 `AgentRun` 的分工：
    - `AgentRun` 是观测日志（trace/token/耗时），会越攒越多，按次留；
    - 这里回答的是"今天到底定了什么"：一天一条（user_id + day 唯一），
      餐食、麦当劳推荐、tips 全在 output 里，重新规划就覆盖当天这条。

    mcdonalds 也在 output 里，所以"那天推荐了哪几样、剩多少额度"能复现。
    """

    __tablename__ = "today_agent_plans"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_today_agent_plan_day"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    # 生成时的计划快照：版本变了也能看出"这条建议是基于哪一版"
    plan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_text: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    # 生成它的那次 run（trace 去 /runs 查）
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RuntimeFlag(Base):
    """运行期开关（不重启即可生效）；目前只有 llm_paused。"""

    __tablename__ = "runtime_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


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
    # plan_id 是 36；用户级审计流是 "user:<36>"，所以不能只留 36。
    stream_id: Mapped[str] = mapped_column(String(64), index=True)
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
