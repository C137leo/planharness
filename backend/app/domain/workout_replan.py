"""Explicit abandonment consent and immutable, separately scoped workout cycles."""
import copy
import datetime as dt
from sqlalchemy import select
from app.models import WorkoutReplanAuthorization, WorkoutSession, WorkoutLog, Profile, utcnow
from app.domain.timeutil import as_utc, day_end_utc


def active_cycle(db,user,day):
    row=db.scalar(select(WorkoutReplanAuthorization).where(
        WorkoutReplanAuthorization.user_id==user.id,WorkoutReplanAuthorization.date==day,
        WorkoutReplanAuthorization.status=='applied').order_by(WorkoutReplanAuthorization.applied_at.desc(),WorkoutReplanAuthorization.created_at.desc()))
    return row.id if row else None


def session_cycle(session):
    return next((i.get('workout_cycle_id') for i in session.items or [] if i.get('workout_cycle_id')),None)


def current_session(db,user,day,cycle):
    rows=db.scalars(select(WorkoutSession).where(WorkoutSession.user_id==user.id,WorkoutSession.date==day,
        WorkoutSession.status!='superseded').order_by(WorkoutSession.started_at.desc()))
    return next((s for s in rows if session_cycle(s)==cycle),None)


def recorded_history(db,user,day,cycle):
    from app.domain.catalog import display_action_name
    result=[]
    for session in db.scalars(select(WorkoutSession).where(WorkoutSession.user_id==user.id,WorkoutSession.date==day).order_by(WorkoutSession.started_at)):
        if cycle is not None and session_cycle(session)==cycle:continue
        for item in session.items or []:
            count=int(item.get('completed_sets') or 0)
            if count:
                result.append({'session_id':session.id,'session_item_id':item['id'],'name':display_action_name(item),
                    'completed_sets':count,'source':item.get('completion_source') or 'historical_record',
                    'set_logs':copy.deepcopy(item.get('set_logs') or [])})
    return result


def etag(row):
    from app.domain.day_revision import digest
    return digest({'id':row.id,'version':row.version,'base':row.base_revision,'scope':row.scope})


def effective_status(row, current_base_revision=None):
    """Read-only validity; historical approval does not imply present authority."""
    if row.status in {'pending','approved'}:
        if as_utc(row.expires_at)<=utcnow():return 'expired'
        if current_base_revision is not None and row.base_revision!=current_base_revision:return 'stale'
    return row.status


def payload(row, current_base_revision=None):
    from app.domain.workout import progress_for_day
    from types import SimpleNamespace
    # Preview shows stored records; it never marks additional sets completed.
    sessions=row.base_state['facts']['sessions']
    items=[i for s in sessions for i in s['items'] or []]
    cycle=row.base_state['day'].get('workout_cycle_id')
    current=[s for s in sessions if s['status']!='superseded' and next((i.get('workout_cycle_id') for i in s['items'] if i.get('workout_cycle_id')),None)==cycle]
    latest=max(current,key=lambda s:s.get('started_at') or '') if current else None
    progress=progress_for_day(SimpleNamespace(items=latest['items'],agent_log=latest['agent_log']) if latest else None,row.base_state['day']['actions'])
    records=[{'name':i.get('name') or i['action_ref'],'completed_sets':int(i.get('completed_sets') or 0)} for i in items if int(i.get('completed_sets') or 0)>0]
    status=effective_status(row,current_base_revision)
    return {'authorization_id':row.id,'etag':etag(row),'date':str(row.date),'status':status,'stored_status':row.status,'scope':row.scope,
            'usable_for_creation':status=='approved' and current_base_revision is not None,
            'base_matches_current':row.base_revision==current_base_revision if current_base_revision is not None else None,
            'recorded':records,'remaining_sets':sum(i['remaining_sets'] for i in progress['items']),'total_sets':progress['total_sets'],
            'goal_text':row.goal_text,'expires_at':as_utc(row.expires_at).isoformat()}


def owned(db,user,ident):
    from app.domain.day_revision import RevisionError
    row=db.get(WorkoutReplanAuthorization,ident)
    if row is None or row.user_id!=user.id:raise RevisionError('ownership','重排确认不存在或不属于当前用户。')
    return row


def request(db,user,day,*,scope,expected_base_revision,goal_text):
    from app.domain.day_revision import context,digest,SCOPE,RevisionError
    from app.domain.workout_records import lock
    if 'workout' not in scope or not set(scope)<=SCOPE:raise RevisionError('scope','重排确认必须明确训练范围。')
    lock(db,user);base=context(db,user,day)
    if digest(base)!=expected_base_revision:raise RevisionError('conflict','训练记录或安排已变化，请重新核对后再确认。')
    expires=min(utcnow()+dt.timedelta(minutes=20),day_end_utc(day,db.get(Profile,user.id)))
    if expires<=utcnow():raise RevisionError('expired','该日期已结束，请选择新的训练日期。')
    row=WorkoutReplanAuthorization(user_id=user.id,date=day,scope=list(dict.fromkeys(scope)),base_state=base,
        base_revision=digest(base),goal_text=goal_text,
        expires_at=expires)
    db.add(row);db.flush();return payload(row,digest(base))


def approve(db,user,ident,expected):
    from app.domain.day_revision import context,digest,RevisionError
    from app.domain.workout_records import lock
    lock(db,user)
    row=owned(db,user,ident)
    if row.status not in {'pending','approved'} or etag(row)!=expected:raise RevisionError('revision','重排确认已失效或版本已变化。')
    if as_utc(row.expires_at)<=utcnow():raise RevisionError('expired','重排确认已过期。')
    base=digest(context(db,user,row.date))
    if base!=row.base_revision:raise RevisionError('conflict','确认前训练事实或安排发生了变化。')
    row.status='approved';db.flush();return payload(row,base)


def cancel(db,user,ident,expected):
    from app.domain.day_revision import RevisionError
    from app.domain.workout_records import lock
    lock(db,user)
    row=owned(db,user,ident)
    if etag(row)!=expected or row.status not in {'pending','approved','cancelled'}:raise RevisionError('revision','重排确认已结束或版本已变化。')
    row.status='cancelled';db.flush();return payload(row)


def authorized(db,user,ident,day,scope,base_revision):
    from app.domain.day_revision import RevisionError
    row=owned(db,user,ident)
    if row.date!=day or row.status!='approved' or as_utc(row.expires_at)<=utcnow():raise RevisionError('replan_consent','请先确认是否放弃原方案未完成部分，再生成新目标。')
    if not set(scope)<=set(row.scope) or row.base_revision!=base_revision:raise RevisionError('conflict','重排确认的范围或基础训练事实已变化。')
    return row


def archive_sessions(db,user,day):
    """Close the old execution path, persist actual partial work, never fill plans."""
    from app.domain.workout_records import sync_item
    for session in db.scalars(select(WorkoutSession).where(WorkoutSession.user_id==user.id,WorkoutSession.date==day,
            WorkoutSession.status.in_(('active','paused','completed')))):
        session.finished_at=utcnow()
        for item in session.items or []:
            if int(item.get('completed_sets') or 0)>0:
                existing=db.scalar(select(WorkoutLog).where(WorkoutLog.session_id==session.id,WorkoutLog.session_item_id==item['id']))
                if existing is None:sync_item(db,user,session,item)
                elif existing.sets!=item['completed_sets'] or existing.details.get('set_logs')!=(item.get('set_logs') or []):
                    raise ValueError('旧日志与实际组数不一致，需要先核对。')
        session.status='superseded'
        # Ended is not completed; original item counts, weights and sources survive.
    db.flush()
