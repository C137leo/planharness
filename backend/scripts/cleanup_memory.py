"""手动执行一次记忆生命周期维护（生产排障/补跑用）。

用法：
    cd backend
    .venv/bin/python scripts/cleanup_memory.py

默认读取 .env 的 DATABASE_URL；也可以通过参数覆盖保留期。
后台任务由 app.main 的生命周期任务每天自动执行，这个脚本用于手动补跑。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import make_engine, make_session_factory  # noqa: E402
from app.domain.memory_lifecycle import run_lifecycle_cleanup  # noqa: E402


def main() -> int:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    with session_factory() as db:
        result = run_lifecycle_cleanup(
            db,
            short_term_retention_days=settings.short_term_memory_retention_days,
            temporary_retention_days=settings.temporary_context_retention_days,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
