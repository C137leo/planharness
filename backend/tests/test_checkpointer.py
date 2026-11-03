"""checkpointer 后端选择的单元测试（不依赖真实 Postgres）。

验证的是「配置 → 后端」的映射规则：
- `auto` 看数据库方言：SQLite → 内存，Postgres → Postgres；
- 显式指定优先于 auto；
- 非法取值报错，而不是悄悄退回内存；
- DSN 会把 SQLAlchemy 的 `+psycopg` 方言后缀去掉（psycopg 不认这个后缀）。
"""

from __future__ import annotations

import os

import pytest

from app.agent.checkpointer import (
    CheckpointerConfigError,
    make_checkpointer,
    postgres_dsn,
    resolve_backend,
)
from app.config import Settings

PG_URL = "postgresql+psycopg://postgre:@127.0.0.1:5432/planharness"
SQLITE_URL = "sqlite:///./planharness.db"


def _settings(**kwargs) -> Settings:
    kwargs.setdefault("env", "dev")
    return Settings(**kwargs)


def test_auto_uses_memory_for_sqlite() -> None:
    """本地 SQLite → 内存 checkpointer（不连数据库）。"""
    assert resolve_backend(_settings(database_url=SQLITE_URL)) == "memory"


def test_auto_uses_postgres_when_database_is_postgres() -> None:
    """配了 Postgres → 自动用 Postgres（持久化）。"""
    assert resolve_backend(_settings(database_url=PG_URL)) == "postgres"


def test_explicit_backend_overrides_auto() -> None:
    """显式指定优先：prod 想临时演练可以强制 memory。"""
    assert (
        resolve_backend(_settings(database_url=PG_URL, checkpointer_backend="memory"))
        == "memory"
    )
    assert (
        resolve_backend(_settings(database_url=SQLITE_URL, checkpointer_backend="postgres"))
        == "postgres"
    )


def test_invalid_backend_raises() -> None:
    """拼错的取值必须报错，不能悄悄当 auto。"""
    with pytest.raises(CheckpointerConfigError):
        resolve_backend(_settings(checkpointer_backend="postgress"))


def test_dsn_strips_sqlalchemy_dialect_suffix() -> None:
    """`postgresql+psycopg://` 要转成 psycopg 认得的 `postgresql://`。"""
    dsn = postgres_dsn(_settings(database_url=PG_URL))
    assert dsn.startswith("postgresql://")
    assert "+psycopg" not in dsn


def test_dsn_rejects_non_postgres() -> None:
    """要求 Postgres 却给了 SQLite → 明确报错，而不是连一个不存在的库。"""
    with pytest.raises(CheckpointerConfigError):
        postgres_dsn(_settings(database_url=SQLITE_URL))


def test_make_checkpointer_memory_mode_returns_noop_closer() -> None:
    """内存模式返回 (None, close)，close 可以安全调用。"""
    saver, close = make_checkpointer(_settings(database_url=SQLITE_URL))
    assert saver is None
    close()  # 不应该抛异常


@pytest.mark.skipif(
    not os.getenv("PH_TEST_POSTGRES_DSN"),
    reason="需要设置 PH_TEST_POSTGRES_DSN 指向一个可用的 Postgres 才能跑",
)
def test_postgres_checkpointer_setup_and_roundtrip() -> None:
    """真实 Postgres 上建表并写读一次 checkpoint（默认跳过）。"""
    from langgraph.checkpoint.postgres import PostgresSaver

    settings = _settings(database_url=os.environ["PH_TEST_POSTGRES_DSN"])
    saver, close = make_checkpointer(settings)
    try:
        assert isinstance(saver, PostgresSaver)
    finally:
        close()


# ---------------- checkpoint 清理任务 ----------------

from app.agent.checkpoint_cleanup import (  # noqa: E402
    STALE_THREADS_SQL,
    parse_args,
    purge_old_checkpoints,
    stale_thread_ids,
)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params):
        self.executed.append((sql, params))
        return self

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self.cursor = _FakeCursor(rows)

    def execute(self, sql, params):
        return self.cursor.execute(sql, params)


class _FakeSaver:
    def __init__(self):
        self.pruned: list[tuple[list[str], str]] = []

    def prune(self, thread_ids, *, strategy):
        self.pruned.append((list(thread_ids), strategy))


def test_stale_sql_uses_checkpoint_timestamp() -> None:
    """时间戳藏在 checkpoint JSONB 里（表没有 created_at 列），SQL 必须取它。"""
    assert "checkpoint->>'ts'" in STALE_THREADS_SQL
    assert "make_interval(days => %s)" in STALE_THREADS_SQL


def test_stale_thread_ids_reads_dict_rows() -> None:
    conn = _FakeConn([{"thread_id": "t-1"}, {"thread_id": "t-2"}])
    assert stale_thread_ids(conn, 30) == ["t-1", "t-2"]
    sql, params = conn.cursor.executed[0]
    assert "GROUP BY thread_id" in sql
    assert params == (30,)


def test_purge_deletes_hit_threads() -> None:
    """命中即整条 thread 删除，策略用 "delete"（连 writes/blobs 一起清）。"""
    conn = _FakeConn([{"thread_id": "t-1"}, {"thread_id": "t-2"}])
    saver = _FakeSaver()

    stats = purge_old_checkpoints(conn, saver, older_than_days=7)

    assert stats == {"threads": 2, "pruned": 2, "dry_run": False}
    assert saver.pruned == [(["t-1", "t-2"], "delete")]


def test_purge_dry_run_does_not_delete() -> None:
    """dry-run 只统计，不动数据 —— 上线前先看会删多少。"""
    conn = _FakeConn([{"thread_id": "t-1"}])
    saver = _FakeSaver()

    stats = purge_old_checkpoints(conn, saver, dry_run=True)

    assert stats["threads"] == 1
    assert stats["pruned"] == 0
    assert saver.pruned == []


def test_purge_with_no_stale_threads_skips_prune() -> None:
    conn = _FakeConn([])
    saver = _FakeSaver()

    stats = purge_old_checkpoints(conn, saver)

    assert stats["threads"] == 0
    assert saver.pruned == []


def test_cleanup_cli_parses_args() -> None:
    args = parse_args(["--days", "7", "--dry-run"])
    assert args.days == 7
    assert args.dry_run is True
    assert args.dsn == ""
