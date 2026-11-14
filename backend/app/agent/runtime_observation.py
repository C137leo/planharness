"""Best-effort observations at the real execution dispatcher, not a second executor."""
from functools import wraps
import time
from app.models import utcnow


def append(db,event):
    try:
        from app.domain.runtime_console import redact
        sink=db.info.get('runtime_trace')
        if sink is not None:sink.append(redact({'timestamp':utcnow().isoformat(),**event}))
    except Exception:
        pass


def observe_execution(fn):
    @wraps(fn)
    def wrapped(db,user,snapshot,tasks,*args,**kwargs):
        started=time.monotonic()
        ids=[t.task_id for t in tasks]
        append(db,{'event_type':'execution.boundary.started','stage':'execution_boundary','status':'started','task_ids':ids})
        try:
            result=fn(db,user,snapshot,tasks,*args,**kwargs)
        except Exception as exc:
            append(db,{'event_type':'execution.boundary.failed','stage':getattr(exc,'stage','execution_boundary'),'status':'failed','task_ids':ids,'error_type':type(exc).__name__,'duration_ms':round((time.monotonic()-started)*1000)})
            raise
        append(db,{'event_type':'execution.boundary.completed','stage':'execution_boundary','status':result.get('overall_status','unknown'),'task_ids':ids,'duration_ms':round((time.monotonic()-started)*1000),'task_results':result.get('task_results',[])})
        return result
    return wrapped
