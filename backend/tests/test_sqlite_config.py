"""SQLite 并发配置：模型调用耗时较长时，不能再被锁库直接打成 500。"""

from app.db import make_engine


def test_sqlite_engine_uses_wal_and_busy_timeout(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'planharness.db'}")
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30000
    engine.dispose()
