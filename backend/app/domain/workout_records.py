"""Transactional training facts. Never infer completion from a planned action."""
import copy
import datetime as dt
import hashlib
import json
from sqlalchemy import select, update
from app.models import User, WorkoutSession, WorkoutLog, WorkoutRecordRevision
from app.domain.timeutil import as_utc


def revision(session):
    value={k:copy.deepcopy(getattr(session,k)) for k in ('status','items','actual_minutes','note','agent_log')}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def lock(db,user):
    if db.bind.dialect.name=='sqlite':
        db.execute(update(User).where(User.id==user.id).values(updated_at=User.updated_at))
    else:
        db.scalar(select(User).where(User.id==user.id).with_for_update())
    db.expire_all()


def owned(db,user,sid):
    row=db.get(WorkoutSession,sid)
    if row is None or row.user_id!=user.id:raise ValueError('训练会话不属于当前用户。')
    return row


def log_data(row):
    return {'id':row.id,'session_id':row.session_id,'session_item_id':row.session_item_id,'action_ref':row.action_ref,
        'sets':row.sets,'reps':row.reps,'duration_min':row.duration_min,'revision':row.revision,'details':copy.deepcopy(row.details)}


def read(db,user,sid):
    from app.domain.workout import session_payload, progress_for_day
    from app.domain.day_plan import effective_day
    from app.domain.workout_replan import current_session, session_cycle
    row=owned(db,user,sid)
    logs=list(db.scalars(select(WorkoutLog).where(WorkoutLog.user_id==user.id,WorkoutLog.session_id==sid).order_by(WorkoutLog.session_item_id)))
    payload=session_payload(row)
    current=effective_day(db,user,row.date,fallback_next=False)
    cycle=current.get('workout_cycle_id')
    progress=progress_for_day(current_session(db,user,row.date,cycle),current['actions'])
    known=any(x.get('kind')=='fact_finish' and x.get('actual_minutes_known') for x in row.agent_log or [])
    if row.status=='finished' and not known:payload['actual_minutes']=None
    return {'session':payload,'record_revision':revision(row),'logs':[log_data(x) for x in logs],
        'record_scope':'prior_cycle' if session_cycle(row)!=cycle else 'current_cycle',
        'current_day_progress':progress,
        'actual_minutes_known':known,
        'actual_minutes_source':next((x.get('actual_minutes_source','unknown') for x in row.agent_log or [] if x.get('kind')=='fact_finish'),'unknown'),
        'legacy_records_unlinked':row.status=='finished' and not any(x.get('kind')=='fact_finish' for x in row.agent_log or []),
        'unrecorded_items':[{'id':i['id'],'name':i.get('name'),'completed_sets':i.get('completed_sets',0),'status':i.get('status')}
            for i in row.items or [] if int(i.get('completed_sets') or 0)<max(1,int(i.get('target_sets') or 1))]}


def verify(db,user,sid):
    db.flush();db.expire_all();row=owned(db,user,sid)
    logs=list(db.scalars(select(WorkoutLog).where(WorkoutLog.user_id==user.id,WorkoutLog.session_id==sid)))
    items={i['id']:i for i in row.items}
    if len({l.session_item_id for l in logs})!=len(logs):raise ValueError('日志存在重复条目。')
    for log in logs:
        item=items.get(log.session_item_id)
        if not item or log.sets!=int(item.get('completed_sets') or 0) or log.details.get('set_logs')!=(item.get('set_logs') or []):
            raise ValueError('日志与实际训练条目不一致。')
    if {i['id'] for i in row.items if int(i.get('completed_sets') or 0)>0}!={l.session_item_id for l in logs if (l.sets or 0)>0}:
        raise ValueError('实际完成记录没有完整持久化。')
    if row.status!='finished':raise ValueError('结束状态未持久化。')
    return read(db,user,sid)


def sync_item(db,user,session,item):
    log=db.scalar(select(WorkoutLog).where(WorkoutLog.session_id==session.id,WorkoutLog.session_item_id==item['id']))
    done=int(item.get('completed_sets') or 0)
    if log is None and done==0:return
    if log is None:
        log=WorkoutLog(user_id=user.id,date=session.date,session_id=session.id,session_item_id=item['id'],
            action_ref=item['action_ref'],kind='cardio' if item.get('slot')=='cardio' else 'flexibility' if item.get('slot') in {'warmup','mobility'} else 'strength',
            duration_min=0,intensity='unknown',note='真实训练记录；动作时长未采集',revision=1)
        db.add(log)
    else:
        if log.user_id!=user.id:raise ValueError('日志所有权不一致。')
        log.revision+=1
    sets=copy.deepcopy(item.get('set_logs') or [])
    log.sets=done
    log.duration_min=round(float(item.get('reported_duration_min') or 0))
    # Repetitions may differ by set. Only the actual final set supplies this legacy field.
    log.reps=sets[-1].get('reps') if sets else None
    log.details={'name':item.get('name'),'plan_id':session.plan_id,'plan_version':session.plan_version,
        'started_at':as_utc(session.started_at).isoformat(),'finished_at':as_utc(session.finished_at).isoformat(),
        'completion_state':item['status'],'set_logs':sets,'actual_duration_known':item.get('reported_duration_min') is not None,
        'duration_source':item.get('duration_source','unknown'),'duration_is_estimate':item.get('duration_source') in {'confirmed_plan_default','user_reported_plan'},
        'source':item.get('completion_source','recorded_facts'),'record_note':item.get('record_note','')}


def reported_item(item,sets,*,reps=None,duration=None,source='user_reported'):
    result=copy.deepcopy(item)
    logs=copy.deepcopy(result.get('set_logs') or [])[:sets]
    while len(logs)<sets:
        logs.append({'set_no':len(logs)+1,'reps':reps,'weight_kg':None,'rpe':None,'source':source})
    result.update(completed_sets=sets,set_logs=logs,
        status='done' if sets>=int(result.get('target_sets') or 1) else 'active' if sets else 'pending',
        completion_source=source)
    if duration is not None:
        result.update(reported_duration_min=duration,duration_source=source)
    return result


def completion_preview(session,args):
    items=copy.deepcopy(session.items or [])
    overrides={v.item_id:v for v in args.item_overrides}
    if len(overrides)!=len(args.item_overrides) or not set(overrides)<={i['id'] for i in items}:
        raise ValueError('完成摘要包含重复或不属于此会话的条目。')
    for n,item in enumerate(items):
        override=overrides.get(item['id'])
        if override is not None:
            defaults=args.completion_basis=='confirm_current_plan'
            reps=override.reps if override.reps is not None else item.get('target_reps') if defaults else None
            duration=override.duration_min if override.duration_min is not None else (item.get('duration_min') if override.actual_sets else 0) if defaults else None
            items[n]=reported_item(item,override.actual_sets,reps=reps,
                duration=duration,source='user_reported')
            if override.reps is not None:
                items[n]['reported_reps']=override.reps
                for entry in items[n]['set_logs']:
                    entry.update(reps=override.reps,reps_source='user_reported')
            if defaults and override.duration_min is None:items[n]['duration_source']='confirmed_plan_default'
            if defaults and override.reps is None:
                for entry in items[n]['set_logs'][len(item.get('set_logs') or []):]:entry['reps_source']='confirmed_plan_default'
        elif args.completion_basis=='confirm_current_plan' and item.get('status') not in {'skipped','done'} and not item.get('outside_current_plan'):
            items[n]=reported_item(item,max(int(item.get('completed_sets') or 0),int(item.get('target_sets') or 1)),
                reps=item.get('target_reps'),duration=item.get('reported_duration_min',item.get('duration_min')),
                source='confirmed_plan_default')
    return items


def progress(db,user,sid,item_id,args):
    from app.domain.workout import _advance
    lock(db,user);session=owned(db,user,sid)
    if session.status not in {'active','paused','completed'}:raise ValueError('训练已结束，请使用记录修订能力。')
    if revision(session)!=args.expected_revision:raise ValueError('训练进度已变化，请重新查看。')
    items=copy.deepcopy(session.items);index=next((n for n,i in enumerate(items) if i['id']==item_id),None)
    if index is None:raise ValueError('目标训练条目不存在。')
    old=items[index]
    sets=int(old.get('target_sets') or 1) if args.completion_basis=='as_planned' else args.actual_sets
    if sets<int(old.get('completed_sets') or 0):raise ValueError('进行中减少已记录组数需要明确的记录纠错，不能覆盖事实。')
    source='user_reported_plan' if args.completion_basis=='as_planned' else 'user_reported'
    duration=args.duration_min if args.duration_min is not None else old.get('duration_min') if args.completion_basis=='as_planned' else None
    items[index]=reported_item(old,sets,reps=args.reps,duration=duration,source=source)
    if items[index]==old:return read(db,user,sid)
    session.items=items
    session.agent_log=[*(session.agent_log or []),{'kind':'progress_report','item_id':item_id,'before':old,'after':copy.deepcopy(items[index])}]
    _advance(session);db.flush();db.expire_all()
    row=owned(db,user,sid)
    actual=next(i for i in row.items if i['id']==item_id)
    if actual['completed_sets']!=sets:raise ValueError('实际进度写入验证失败。')
    return read(db,user,sid)


def finish(db,user,sid,args):
    lock(db,user);session=owned(db,user,sid)
    if session.status=='finished':return read(db,user,sid)
    if revision(session)!=args.expected_revision:raise ValueError('训练记录已变化，请重新预览后确认。')
    if session.status not in {'active','paused','completed'}:raise ValueError('该训练会话不能结束。')
    items=completion_preview(session,args)
    ids=[i['id'] for i in items]
    if len(ids)!=len(set(ids)):raise ValueError('训练条目ID重复。')
    for item in items:
        done=int(item.get('completed_sets') or 0)
        if done<0 or len(item.get('set_logs') or [])>done:raise ValueError('实际组数与逐组记录不一致。')
        if item.get('status') not in {'done','skipped'}:
            item['status']='partial' if done else 'skipped' if args.unfinished_policy=='skip_remaining' else 'unrecorded'
    session.items=items;session.status='finished';session.finished_at=dt.datetime.now(dt.timezone.utc)
    duration_items=[i for i in items if int(i.get('completed_sets') or 0)>0]
    default_minutes=sum(float(i['reported_duration_min']) for i in duration_items) if args.completion_basis=='confirm_current_plan' and all(i.get('reported_duration_min') is not None for i in duration_items) else None
    session.actual_minutes=args.actual_minutes if args.actual_minutes is not None else round(default_minutes) if default_minutes is not None else 0
    session.difficulty=args.difficulty;session.fatigue=args.fatigue;session.note=args.note
    total=sum(max(1,int(i.get('target_sets') or 1)) for i in items)
    session.completion_pct=round(sum(int(i.get('completed_sets') or 0) for i in items)/total*100,1) if total else 0
    session.current_index=len(items)
    session.agent_log=[*(session.agent_log or []),{'kind':'fact_finish','actual_minutes_known':args.actual_minutes is not None or default_minutes is not None,
        'actual_minutes_source':'user_reported' if args.actual_minutes is not None else 'confirmed_plan_default' if default_minutes is not None else 'unknown',
        'completion_basis':args.completion_basis,
        'unfinished_policy':args.unfinished_policy,'at':session.finished_at.isoformat()}]
    for item in items:sync_item(db,user,session,item)
    return verify(db,user,sid)


def correct(db,user,sid,item_id,args):
    lock(db,user);session=owned(db,user,sid)
    if session.status!='finished':raise ValueError('只有已结束的训练可以走记录修订；进行中请记录实际完成组。')
    if not any(x.get('kind')=='fact_finish' for x in session.agent_log or []):
        raise ValueError('旧训练日志尚未关联真实条目，不能猜测归属后修改。')
    if revision(session)!=args.expected_revision:raise ValueError('记录版本已变化，请重新查看后确认。')
    items=copy.deepcopy(session.items);item=next((i for i in items if i['id']==item_id),None)
    if item is None:raise ValueError('训练条目不在此会话。')
    before=copy.deepcopy(item)
    done=args.actual_sets if args.actual_sets is not None else int(item.get('completed_sets') or 0)
    sets=copy.deepcopy(item.get('set_logs') or [])[:done]
    while len(sets)<done:sets.append({'set_no':len(sets)+1,'reps':None,'weight_kg':None,'rpe':None,'source':'user_reported'})
    if args.set_no is not None:
        if args.set_no>done:raise ValueError('组号超出已确认的实际组数。')
        for key in ('reps','weight_kg'):
            value=getattr(args,key)
            if value is not None:sets[args.set_no-1][key]=value
    state=args.completion_state or ('unrecorded' if done==0 else 'done' if done>=int(item.get('target_sets') or 1) else 'partial')
    if (state in {'skipped','unrecorded'} and done>0) or (state in {'done','partial'} and done==0):
        raise ValueError('完成状态与实际组数矛盾。')
    item.update(completed_sets=done,set_logs=sets,status=state,record_note=args.reason)
    session.items=items
    session.agent_log=[*(session.agent_log or []),{'kind':'record_correction','item_id':item_id,'reason':args.reason}]
    total=sum(max(1,int(i.get('target_sets') or 1)) for i in items)
    session.completion_pct=round(sum(int(i.get('completed_sets') or 0) for i in items)/total*100,1) if total else 0
    db.add(WorkoutRecordRevision(user_id=user.id,session_id=sid,session_item_id=item_id,before=before,after=copy.deepcopy(item),reason=args.reason))
    sync_item(db,user,session,item)
    return verify(db,user,sid)
