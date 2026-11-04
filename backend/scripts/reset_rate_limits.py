"""清空限流计数（**仅限本地/演示环境**）。

什么时候需要它：演示时连续点错几次，把自己的 IP/手机号额度用完，
接下来一小时（甚至一天）都会被 429 拦住。清空计数即可恢复。

为什么不是"把限制调大"：限制本身是对的，问题只在演示时反复试错。
生产环境不要跑这个脚本——那等于给攻击者复位计数器。

用法：
    cd backend
    .venv/bin/python scripts/reset_rate_limits.py --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Base, make_engine, make_session_factory  # noqa: E402
from app.models import RateLimitBucket  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="清空限流计数（仅本地/演示）")
    parser.add_argument("--yes", action="store_true", help="跳过确认")
    args = parser.parse_args()

    settings = get_settings()
    if settings.env not in ("dev", "test"):
        print(f"拒绝执行：当前 ENV={settings.env}，这个脚本只用于本地/演示环境")
        return 1
    if not args.yes:
        answer = input(f"将清空 {settings.database_url} 的限流计数，继续？[y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消")
            return 1

    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as db:
        before = int(db.scalar(select(func.count()).select_from(RateLimitBucket)) or 0)
        db.execute(delete(RateLimitBucket))
        db.commit()

    print(f"已清空 {before} 条限流记录（{settings.database_url}）")
    print("提示：生产环境请勿执行；要长期放宽请改 .env 里的配额，而不是清计数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
