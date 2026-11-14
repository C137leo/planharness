"""Owner-scoped, read-only projections of persisted Agent runtime evidence.

No business decision or write is performed here. Unknown evidence stays unknown.
"""
import re
from sqlalchemy import select
from app.models import AgentRun, AgentMessage, CoachBusinessSnapshot, CoachTaskExecution
from app.agent.coach_agent import _iso_utc

SECRET = re.compile(r'(?i)(authorization|api[_-]?key|token|password|secret|cookie|credential)')
VALUE_SECRET = re.compile(r'(?i)\b(?:bearer\s+\S+|sk-[a-z0-9_-]{8,})')


def redact(value):
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if SECRET.search(str(k)) else redact(v) for k,v in value.items()}
    if isinstance(value, (list,tuple)):return [redact(v) for v in value]
    if isinstance(value,str):
        value=VALUE_SECRET.sub('[REDACTED]',value)
        return re.sub(r'(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)',r'\1****\2',value)
    return value


def trace_events(trace, run_id):
    """Stable identities/order on reload. Missing historical timestamps remain null."""
    return [dict(event_id=f'{run_id}:trace:{n}',trace_id=run_id,turn_id=run_id,sequence=n,
        event_type=e.get('event_type') or e.get('stage') or e.get('node') or 'unclassified',
        timestamp=e.get('timestamp') or e.get('ts'),status=e.get('status','unknown'),
        task_id=e.get('task_id'),duration_ms=e.get('duration_ms',e.get('latency_ms')),payload=redact(e))
        for n,e in enumerate(trace or []) if isinstance(e,dict)]


def turn(db,uid,id):
    run=db.get(AgentRun,id)
    if not run or run.user_id!=uid:raise LookupError('运行记录不存在或无权访问。')
    output=run.output if isinstance(run.output,dict) else {}
    input=run.input if isinstance(run.input,dict) else {}
    thread=input.get('thread')
    tasks=output.get('task_plan') or []
    results=output.get('task_results') or []
    # Join only the exact execution IDs returned by this turn, never by task label/time proximity.
    executions={}
    for result in results:
        key=result.get('execution_id')
        if key:
            row=db.get(CoachTaskExecution,key)
            if row and row.user_id==uid and row.thread==thread:executions[key]=row
    snapshot_id=(input.get('context') or {}).get('snapshot_id')
    snapshot=db.get(CoachBusinessSnapshot,snapshot_id) if snapshot_id else None
    if snapshot and (snapshot.user_id!=uid or snapshot.thread!=thread):snapshot=None
    events=trace_events(run.trace,run.id)
    used=[e['payload']['context_input'] for e in events if 'context_input' in e['payload']]
    projected=[]
    for task in tasks:
        matches=[r for r in results if r.get('task_id')==task.get('task_id')]
        journals=[executions[r['execution_id']] for r in matches if r.get('execution_id') in executions]
        projected.append({'task':task,'results':matches,'status':matches[-1].get('status','unknown') if matches else 'not_executed',
            'persisted_changes':[change for j in journals for change in j.resource_changes],
            'persistence_evidence':'verified_execution_journal' if journals else 'not_recorded',
            'verification':[r.get('state_history',[]) for r in matches]})
    failures=[r for r in results if r.get('status') in {'failed','blocked','unknown'}]
    return redact({'turn_id':run.id,'trace_id':run.id,'conversation_id':thread,'status':run.status,
        'input':input,'reply':output.get('reply',''),'created_at':_iso_utc(run.created_at),
        'finished_at':_iso_utc(run.finished_at),'latency_ms':run.latency_ms,
        'tasks':projected,'events':events,'pending_confirmation':output.get('pending_confirmation'),
        'unfulfilled_goals':output.get('unfulfilled_goals',[]),
        'context':{'recorded_business_snapshot':snapshot.payload if snapshot else None,
                   'actual_model_inputs':used or None,'tool_rereads':None,
                   'note':'业务快照不等于LLM输入。历史缺少的采集记录不可用，不从当前数据补造。'},
        'diagnostics':{'error_code':run.error_code,'task_failures':failures,
            'failed_events':[e for e in events if e['status'] in {'failed','invalid_schema','stopped'}],
            'transaction_rollback':'not_recorded'},
        'result_evidence':{'agent_reply':output.get('reply',''),'task_results':results,
            'persisted_journal_count':len(executions),'goal_verified':None,
            'note':'工具结果、持久化执行日志与完整目标验证分别展示；没有证据不标为已验证。'}})


def conversations(db,uid,limit=50):
    from app.agent.coach_agent import conversations_payload
    from app.models import User
    items=conversations_payload(db,db.get(User,uid),limit)
    for item in items:
        last=db.scalar(select(AgentMessage).where(AgentMessage.user_id==uid,AgentMessage.thread==item['thread'],AgentMessage.run_id.is_not(None)).order_by(AgentMessage.created_at.desc()))
        run=db.get(AgentRun,last.run_id) if last else None
        item['status']=run.status if run and run.user_id==uid else 'unknown'
    from app.models import RuntimeConversation
    existing={item['thread'] for item in items}
    for row in db.scalars(select(RuntimeConversation).where(RuntimeConversation.user_id==uid).order_by(RuntimeConversation.created_at.desc()).limit(limit)):
        if row.id not in existing:items.append({'thread':row.id,'title':row.title,'preview':'尚未发送消息','message_count':0,'status':'idle','updated_at':_iso_utc(row.created_at)})
    items.sort(key=lambda x:x['updated_at'],reverse=True)
    return redact(items[:limit])


def messages(db,uid,thread,limit=100,before=''):
    query=select(AgentMessage).where(AgentMessage.user_id==uid,AgentMessage.thread==thread)
    if before:
        anchor=db.get(AgentMessage,before)
        if not anchor or anchor.user_id!=uid or anchor.thread!=thread:raise LookupError('历史位置无效。')
        from sqlalchemy import or_,and_
        query=query.where(or_(AgentMessage.created_at<anchor.created_at,and_(AgentMessage.created_at==anchor.created_at,AgentMessage.id<anchor.id)))
    rows=list(db.scalars(query.order_by(AgentMessage.created_at.desc(),AgentMessage.id.desc()).limit(limit+1)))
    more=len(rows)>limit;rows=rows[:limit]
    return redact({'items':[{'id':r.id,'role':r.role,'text':r.text,'turn_id':r.run_id,'created_at':_iso_utc(r.created_at),'actions':r.actions or [],'result':r.result or {}} for r in reversed(rows)],'before':rows[-1].id if more else ''})
