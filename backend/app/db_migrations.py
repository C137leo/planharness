"""轻量增量迁移：项目没有 Alembic，启动时补齐新增列并做数据回填。

只做**加法**：
- 新表由 `Base.metadata.create_all` 创建；
- 已有表缺的列用 `ALTER TABLE ADD COLUMN` 补；
- 不删除、不改类型，避免在 SQLite / Postgres 之间写两套迁移。

生产环境如果数据库账号没有 DDL 权限，需要由 DBA 预先执行等价 SQL；
失败会记录日志并继续启动（接口运行时会暴露出缺列错误，便于定位）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import (
    Memory,
    Plan,
    PlanAdjustment,
    Profile,
    TemporaryContext,
    User,
)
from app.domain.timeutil import as_utc, day_end_utc, user_timezone

logger = logging.getLogger("planharness.migrations")

# table -> [(column, sql type + default)]
ADDITIVE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "profiles": [
        ("likes", "JSON DEFAULT '[]'"),
        ("preferred_time_window", "VARCHAR(24) DEFAULT ''"),
        ("preferred_time_label", "VARCHAR(32) DEFAULT ''"),
        ("preferred_location", "VARCHAR(24) DEFAULT ''"),
    ],
    "memories": [
        ("importance", "FLOAT DEFAULT 0.5"),
        ("last_confirmed_at", "TIMESTAMP"),
        ("expires_at", "TIMESTAMP"),
    ],
    "temporary_contexts": [
        ("effective_date", "DATE"),
        ("status", "VARCHAR(16) DEFAULT 'active'"),
        ("completed_at", "TIMESTAMP"),
    ],
    "agent_messages": [
        ("result", "JSON DEFAULT '{}'"),
    ],
}


def ensure_additive_schema(engine: Engine) -> list[str]:
    """补齐新增列；返回实际执行的 ALTER 语句（测试/日志用）。"""
    dialect = engine.dialect.name
    executed: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in ADDITIVE_COLUMNS.items():
            if table not in existing_tables:
                continue
            current = {column["name"] for column in inspector.get_columns(table)}
            for column, ddl in columns:
                if column in current:
                    continue
                # Postgres 的 timestamp 建议带时区；SQLite 接受通用 TIMESTAMP
                if dialect == "postgresql" and ddl.startswith("TIMESTAMP"):
                    ddl = ddl.replace("TIMESTAMP", "TIMESTAMP WITH TIME ZONE", 1)
                statement = f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                try:
                    conn.execute(text(statement))
                    executed.append(statement)
                    logger.info("added column: %s", statement)
                except Exception:  # noqa: BLE001 - 单个列失败不能阻止其它列
                    logger.exception("failed to add column: %s", statement)

        # create_all 只为新表建索引；已有表补列后把生命周期查询索引补上
        indexes = [
            "CREATE INDEX IF NOT EXISTS ix_memories_expires_at ON memories (expires_at)",
            "CREATE INDEX IF NOT EXISTS ix_memory_user_kind_status ON memories (user_id, kind, status)",
            "CREATE INDEX IF NOT EXISTS ix_memory_expiry ON memories (kind, status, expires_at)",
            "CREATE INDEX IF NOT EXISTS ix_temp_contexts_effective_date ON temporary_contexts (effective_date)",
            "CREATE INDEX IF NOT EXISTS ix_temp_contexts_status ON temporary_contexts (status)",
            "CREATE INDEX IF NOT EXISTS ix_temp_user_status_date ON temporary_contexts (user_id, status, effective_date)",
        ]
        for statement in indexes:
            try:
                conn.execute(text(statement))
            except Exception:  # noqa: BLE001 - 索引失败只影响性能，不阻止启动
                logger.exception("failed to create index: %s", statement)
    return executed


def backfill_lifecycle_data(
    db: Session,
    *,
    short_term_retention_days: int = 30,
) -> dict:
    """把旧数据补成新口径，并迁移旧的 TemporaryContext override。"""
    counts = {"temp_effective_date": 0, "temp_status": 0,
              "temp_expiry": 0, "memory_expiry": 0,
              "legacy_overrides": 0}

    profiles = {row.user_id: row for row in db.scalars(select(Profile)).all()}
    now = datetime.now(timezone.utc)

    for row in db.scalars(select(TemporaryContext)).all():
        changed = False
        if row.effective_date is None:
            row.effective_date = row.day
            counts["temp_effective_date"] += 1
            changed = True
        if not row.status:
            row.status = "active"
            counts["temp_status"] += 1
            changed = True
        if row.expires_at is None and row.effective_date is not None:
            profile = profiles.get(row.user_id)
            row.expires_at = day_end_utc(row.effective_date, profile)
            counts["temp_expiry"] += 1
            changed = True
        if changed:
            db.add(row)

    # 旧的 plan_override 是 TemporaryContext，升级为 PlanAdjustment（保留原行做历史）
    legacy_rows = list(db.scalars(
        select(TemporaryContext).where(
            TemporaryContext.key == "plan_override",
            TemporaryContext.status.in_(("active", "completed")),
        )
    ).all())
    for row in legacy_rows:
        payload = dict(row.value or {})
        effective = row.effective_date or row.day
        exists = db.scalar(
            select(PlanAdjustment).where(
                PlanAdjustment.user_id == row.user_id,
                PlanAdjustment.effective_date == effective,
                PlanAdjustment.reason == str(payload.get("reason") or ""),
                PlanAdjustment.status == "active",
            )
        )
        if exists is None:
            plan = db.scalar(
                select(Plan).where(
                    Plan.user_id == row.user_id, Plan.status == "active"
                )
            )
            db.add(PlanAdjustment(
                user_id=row.user_id,
                plan_id=plan.id if plan is not None else None,
                base_version=payload.get("base_version"),
                effective_date=effective,
                status="active",
                reason=str(payload.get("reason") or row.text or ""),
                summary=str(payload.get("summary") or ""),
                constraints=payload.get("constraints") or {},
                actions=payload.get("actions") or [],
                changes=[],
                borrowed_from=payload.get("borrowed_from"),
                expires_at=row.expires_at or day_end_utc(effective, profiles.get(row.user_id)),
                created_at=row.created_at or now,
            ))
            counts["legacy_overrides"] += 1
        # 原始上下文不再参与读取；PlanAdjustment 保留可追溯的计划快照
        row.status = "expired"
        db.add(row)

    # 旧短期记忆没有 expires_at：按 updated_at + 默认保留期补齐
    retention = timedelta(days=max(1, int(short_term_retention_days)))
    for row in db.scalars(
        select(Memory).where(Memory.kind == "short_term")
    ).all():
        changed = False
        base = as_utc(row.updated_at or row.created_at) or now
        if row.expires_at is None:
            row.expires_at = base + retention
            counts["memory_expiry"] += 1
            changed = True
        if row.last_confirmed_at is None:
            row.last_confirmed_at = base
            changed = True
        if changed:
            db.add(row)

    db.flush()
    return counts
