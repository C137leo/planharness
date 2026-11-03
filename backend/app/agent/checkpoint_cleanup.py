"""清理 LangGraph 的 checkpoint 表（按 thread 的「最后活跃时间」保留 N 天）。

为什么要单独一个任务：

`checkpoints` / `checkpoint_writes` / `checkpoint_blobs` 三张表**只增不减** ——
图每走一步都写一份快照。一天几百次审批，一个月就是几万行 JSON，
而且这些行永远不会被再读一遍（审批早就结束了）。

本模块做两件事：

1. 找出「最近 N 天没有任何活动」的 thread（时间取 `checkpoint->>'ts'`，
   因为这三张表本身没有 created_at 列）；
2. 调 `PostgresSaver.prune(ids, strategy="delete")` 把整条 thread 删干净 ——
   用库自带的 API，而不是自己拼三条 DELETE，避免漏表或漏事务。

用法（建议挂 systemd timer / cron，每天跑一次）：

    python -m app.agent.checkpoint_cleanup --days 30
    python -m app.agent.checkpoint_cleanup --days 30 --dry-run   # 只看会删多少
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.agent.checkpointer import postgres_dsn
from app.config import get_settings

logger = logging.getLogger(__name__)

# 按 thread 聚合，取该 thread 最新一次 checkpoint 的时间戳。
# `checkpoint->>'ts'` 是 ISO8601 字符串（如 2024-07-31T20:14:19.804150+00:00），
# NULLIF 兜住空串，避免 ::timestamptz 转换报错。
STALE_THREADS_SQL = """
SELECT thread_id
FROM checkpoints
GROUP BY thread_id
HAVING max(NULLIF(checkpoint->>'ts', '')::timestamptz) < now() - make_interval(days => %s)
"""


def stale_thread_ids(conn, older_than_days: int) -> list[str]:
    """查出「最后活动时间早于 N 天前」的 thread_id 列表。

    `conn` 只要求有 `execute(sql, params).fetchall()`，方便测试注入假连接。
    """
    if older_than_days < 0:
        raise ValueError("older_than_days 不能为负")
    rows = conn.execute(STALE_THREADS_SQL, (older_than_days,)).fetchall()
    ids: list[str] = []
    for row in rows:
        # 真实连接用 dict_row（返回 dict）；也兼容 tuple 形式
        ids.append(row["thread_id"] if isinstance(row, dict) else row[0])
    return ids


def purge_old_checkpoints(conn, saver, *, older_than_days: int = 30, dry_run: bool = False) -> dict:
    """删除过期 thread 的全部 checkpoint。

    返回统计：`{"threads": 命中数, "pruned": 实际删除数, "dry_run": bool}`。
    `conn` 只用于查询，`saver` 只用于删除 —— 职责分开，测试时两者都可以是假的。
    """
    ids = stale_thread_ids(conn, older_than_days)
    pruned = 0
    if ids and not dry_run:
        saver.prune(ids, strategy="delete")
        pruned = len(ids)
    if dry_run:
        logger.info("[dry-run] 命中 %d 条过期 thread，未删除", len(ids))
    else:
        logger.info("已清理 %d 条过期 thread（阈值 %d 天）", pruned, older_than_days)
    return {"threads": len(ids), "pruned": pruned, "dry_run": dry_run}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="清理过期的 LangGraph checkpoint")
    parser.add_argument("--days", type=int, default=30, help="保留最近多少天（默认 30）")
    parser.add_argument("--dsn", default="", help="Postgres DSN；留空则用配置里的 database_url")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不删除")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = get_settings()
    if args.dsn:
        settings = settings.model_copy(update={"checkpointer_dsn": args.dsn})
    try:
        dsn = postgres_dsn(settings)
    except ValueError as exc:
        logger.error("拿不到 Postgres DSN：%s", exc)
        return 2

    # 延迟导入：只跑清理任务时才需要这两个包
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn, \
            PostgresSaver.from_conn_string(dsn) as saver:
        stats = purge_old_checkpoints(
            conn, saver, older_than_days=args.days, dry_run=args.dry_run
        )
    print(
        f"threads={stats['threads']} pruned={stats['pruned']} dry_run={stats['dry_run']}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - 直接命令行调用
    sys.exit(main())
