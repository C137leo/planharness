"""轻量增量迁移：旧库已有表时补齐新增列和索引。"""

from __future__ import annotations

from sqlalchemy import inspect, text

from app.db import make_engine
from app.db_migrations import ensure_additive_schema


def test_additive_schema_adds_lifecycle_columns(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE profiles (user_id VARCHAR(36) PRIMARY KEY)"
        ))
        conn.execute(text(
            "CREATE TABLE memories (id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36), "
            "kind VARCHAR(16), category VARCHAR(32), key VARCHAR(64), value TEXT, "
            "status VARCHAR(16))"
        ))
        conn.execute(text(
            "CREATE TABLE temporary_contexts (id VARCHAR(36) PRIMARY KEY, "
            "user_id VARCHAR(36), day DATE, key VARCHAR(64), value TEXT)"
        ))
    executed = ensure_additive_schema(engine)
    assert executed
    inspector = inspect(engine)
    profile_columns = {column["name"] for column in inspector.get_columns("profiles")}
    memory_columns = {column["name"] for column in inspector.get_columns("memories")}
    temp_columns = {column["name"] for column in inspector.get_columns("temporary_contexts")}
    assert {"likes", "preferred_time_window", "preferred_time_label",
            "preferred_location"} <= profile_columns
    assert {"importance", "last_confirmed_at", "expires_at"} <= memory_columns
    assert {"effective_date", "status", "completed_at"} <= temp_columns
    # 幂等：再跑一次不应报错
    ensure_additive_schema(engine)
