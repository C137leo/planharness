"""LangGraph checkpointer 的后端选择：dev 用内存，prod 用 Postgres。

为什么必须分环境：

- **dev / 单测**：`InMemorySaver` 最省事，进程结束自然清空，不污染数据库；
- **prod**：进程会重启、会滚动发布。审批是「挂起等人」的状态，
  一旦存在内存里，`systemctl restart` 之后用户点「通过」就找不到那个 thread 了。

选择逻辑（`CHECKPOINTER_BACKEND` 可显式覆盖）：

| 取值 | 行为 |
| --- | --- |
| `auto`（默认） | `DATABASE_URL` 指向 Postgres → Postgres；否则 → 内存 |
| `memory` | 永远内存（生产临时演练用） |
| `postgres` | 永远 Postgres（dev 想验证持久化时用） |

`auto` 刻意**不看 `ENV`**，只看数据库方言 —— 因为「能不能持久化」事实上由
数据库决定：配的是 SQLite 就没有 Postgres 可写，硬上 Postgres 只会启动失败。
代价是「生产却配了 SQLite」这种矛盾配置会被静默降级，所以 `make_checkpointer`
会在这种组合下打一条 warning，提醒你这不是正常的生产形态。

显式指定 `postgres` 时失败就向上抛，不做降级 —— 你明确要求了持久化，
悄悄退回内存会让人误以为审批被存住了，那比启动失败更糟。
"""

from __future__ import annotations

import logging
from typing import Callable

from app.config import Settings

logger = logging.getLogger(__name__)

BACKENDS = ("auto", "memory", "postgres")


class CheckpointerConfigError(ValueError):
    """配置本身有问题（取值非法、要求 Postgres 却给了非 Postgres 的 DSN 等）。"""


def resolve_backend(settings: Settings) -> str:
    """把 `auto` 解析成具体后端；非法取值直接报错。"""
    backend = (settings.checkpointer_backend or "auto").strip().lower()
    if backend not in BACKENDS:
        raise CheckpointerConfigError(
            f"CHECKPOINTER_BACKEND 只能是 {BACKENDS}，实际 {backend!r}"
        )
    if backend == "auto":
        return "postgres" if is_postgres(settings) else "memory"
    return backend


def is_postgres(settings: Settings) -> bool:
    return (settings.database_url or "").strip().startswith("postgresql")


def postgres_dsn(settings: Settings) -> str:
    """拿到 psycopg 能用的 DSN。

    `database_url` 是 SQLAlchemy 方言格式（`postgresql+psycopg://`），
    而 psycopg 只认 `postgresql://` —— 少了这一步转换会报
    "invalid connection string"。
    """
    raw = (settings.checkpointer_dsn or settings.database_url or "").strip()
    if not raw:
        raise CheckpointerConfigError("checkpointer 需要 DSN，但 database_url 为空")
    if not raw.startswith("postgresql"):
        raise CheckpointerConfigError(
            f"checkpointer 要求 Postgres，当前 database_url 是 {raw.split(':', 1)[0]}://…"
        )
    return raw.replace("postgresql+psycopg://", "postgresql://", 1).replace(
        "postgresql+psycopg2://", "postgresql://", 1
    )


def make_checkpointer(
    settings: Settings,
) -> tuple[object | None, Callable[[], None]]:
    """按配置创建 checkpointer。

    返回 `(checkpointer, close)`：
    - 内存模式返回 `(None, noop)` —— 由调用方回退到 `InMemorySaver`；
    - Postgres 模式返回已 `setup()` 的 saver 和关闭函数（应用退出时调用）。
    """
    backend = resolve_backend(settings)

    if backend == "memory":
        if settings.env == "prod":
            logger.warning(
                "ENV=prod 但 DATABASE_URL 不是 Postgres（%s），checkpointer 退回内存："
                "进程重启会丢失挂起中的审批。生产请配置 Postgres 或显式设 "
                "CHECKPOINTER_BACKEND=postgres。",
                settings.database_url.split(":", 1)[0],
            )
        return None, (lambda: None)

    # 延迟导入：dev/单测不装这个包也能跑
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    dsn = postgres_dsn(settings)

    # autocommit=True：setup() 的 DDL 才能落库；
    # dict_row：PostgresSaver 内部用 row["column"] 取值，tuple 工厂会报 TypeError。
    pool = ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=settings.checkpointer_pool_size,
        kwargs={"autocommit": True, "row_factory": dict_row},
        timeout=10,
        open=True,
    )
    try:
        saver = PostgresSaver(pool)
        # 首次使用必须建表；setup() 内部是幂等的 DDL
        saver.setup()
    except Exception:
        pool.close()
        raise

    logger.info(
        "checkpointer 使用 Postgres（%s，池上限 %d）",
        dsn.split("@")[-1],
        settings.checkpointer_pool_size,
    )
    return saver, pool.close
