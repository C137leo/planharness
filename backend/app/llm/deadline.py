"""Per-turn model budget shared across planner, review, repair and proposal calls."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_deadline = ContextVar('coach_model_deadline', default=None)


def remaining_timeout(configured=30.0):
    from app.llm.cancellation import check_cancelled
    check_cancelled()
    deadline = _deadline.get()
    if deadline is None:
        return configured
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('本次理解耗时已达到上限；未自动重试写入，请核对结果后继续。')
    return min(configured, remaining)


@contextmanager
def model_budget(seconds=120):
    token = _deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)
