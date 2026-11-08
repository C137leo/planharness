"""全局大模型开关。

为什么放在最底层：TodayAgent、计划草案、补丁文案、VLM 比价最终都会走
`ToolCallingClient` / `DeepSeekExplainer`。在这两个入口拦一次，比在每个
接口里各写一遍可靠；接口层再加 503 提示，保证用户知道发生了什么。

开关存在 `runtime_flags` 表里，不依赖重启；数据库每次调用前读一次，
以后即使 uvicorn 开多 worker 也不会出现某个 worker 还偷偷请求模型。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import HTTPException

from app.models import RuntimeFlag

logger = logging.getLogger(__name__)

FLAG_KEY = "llm_paused"
PAUSED_MESSAGE = "该项目正在修改中，暂时无法调用大模型，请等待通知后再使用~"

_session_factory: Callable | None = None


class LLMDisabledError(RuntimeError):
    """模型调用被运营开关关闭。"""

    def __init__(self) -> None:
        super().__init__(PAUSED_MESSAGE)


def configure_llm_guard(session_factory: Callable) -> None:
    global _session_factory
    _session_factory = session_factory


def _read_paused(db) -> bool:
    row = db.get(RuntimeFlag, FLAG_KEY)
    return bool(row.value) if row is not None else False


def is_llm_paused(db=None) -> bool:
    """读当前开关；传 db 时复用请求事务，否则开一个短 session。"""
    if db is not None:
        return _read_paused(db)
    if _session_factory is None:
        return False
    with _session_factory() as session:
        return _read_paused(session)


def set_llm_paused(db, paused: bool, *, updated_by: str = "") -> bool:
    row = db.get(RuntimeFlag, FLAG_KEY)
    if row is None:
        row = RuntimeFlag(key=FLAG_KEY, value=paused, updated_by=updated_by or None)
        db.add(row)
    else:
        row.value = paused
        row.updated_by = updated_by or None
    db.flush()
    return paused


def ensure_llm_enabled() -> None:
    """模型客户端调用前必过；失败就抛，调用方不得静默降级成"假装调用过"。"""
    if is_llm_paused():
        raise LLMDisabledError()


def require_llm_enabled() -> None:
    """FastAPI dependency：开关打开时直接 503，连 run 都不创建。"""
    if is_llm_paused():
        raise HTTPException(status_code=503, detail=PAUSED_MESSAGE)
