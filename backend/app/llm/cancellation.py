"""Request-scoped cancellation; never confuse cancelling inference with undoing writes."""
from contextvars import ContextVar
from contextlib import contextmanager

_state = ContextVar('coach_cancellation', default=None)


class ModelCancelled(BaseException):
    pass


def check_cancelled():
    state = _state.get()
    if state is not None and state['check']():
        raise ModelCancelled()


def active():
    return _state.get() is not None


def begin_execution():
    state = _state.get()
    if state is not None:
        state['begin']()


@contextmanager
def cancellation_scope(check, begin):
    token = _state.set({'check':check,'begin':begin})
    try:
        check_cancelled()
        yield
    finally:
        _state.reset(token)


async def cancellable_post(url, *, headers, body, timeout):
    """Cancelling the task closes this request's connection, not a shared client."""
    import asyncio
    import httpx
    from contextlib import suppress
    async with httpx.AsyncClient(trust_env=False,timeout=timeout) as client:
        task=asyncio.create_task(client.post(url,headers=headers,json=body))
        try:
            while not task.done():
                check_cancelled()
                await asyncio.wait({task},timeout=0.15)
            check_cancelled()
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
