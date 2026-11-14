"""Dated API regressions on isolated SQLite, preserving the mixed legacy table."""
import datetime as dt
from sqlalchemy import select
from app.evals.dialogue_runner import DialogueEnvironment, DAY
from app.models import PlanAction, Plan, PlanVersion, User
from app.seed import DEMO_USER_ID
from app.domain.day_plan import effective_day, save_override
from tests.test_semantic_binding import Scripted


def test_calendar_rest_day_does_not_borrow_training_but_start_entry_keeps_compatibility(tmp_path):
    env=DialogueEnvironment(tmp_path/'calendar.db','calendar',Scripted([]))
    try:
        saturday=DAY+dt.timedelta(days=(6-DAY.isoweekday())%7)
        with env.factory() as db:
            plan=db.scalar(select(Plan).where(Plan.user_id==DEMO_USER_ID))
            version=db.scalar(select(PlanVersion).where(PlanVersion.plan_id==plan.id,PlanVersion.version==plan.current_version))
            mixed=list(db.scalars(select(PlanAction).where(PlanAction.plan_version_id==version.id,PlanAction.day_index==6)))
            assert any(a.action_type=='meal' for a in mixed)
            assert not any(a.action_type=='exercise' for a in mixed)
        exact=env.http.get('/api/v1/agent/day',headers=env.headers,params={'date':str(saturday),'fallback_next':'false'})
        assert exact.status_code==200,exact.text
        data=exact.json()['data'];assert data['weekday']==6 and data['actions']==[] and data['total_minutes']==0 and data['borrowed_from'] is None
        legacy=env.http.get('/api/v1/agent/day',headers=env.headers,params={'date':str(saturday)}).json()['data']
        assert legacy['actions'] and legacy['borrowed_from'] is not None
    finally:env.close()


def test_dated_api_preserves_adjusted_targets_meals_minutes_and_next_week_template(tmp_path):
    env=DialogueEnvironment(tmp_path/'dated.db','dated',Scripted([]))
    try:
        with env.factory() as db:
            user=db.get(User,DEMO_USER_ID);before=effective_day(db,user,DAY)
            actions=[{'action_ref':'dynamic_warmup','slot':'warmup','name':'热身','sets':1,'duration_min':5},
                     {'action_ref':'row','slot':'strength','name':'划船','sets':2,'duration_min':6}]
            save_override(db,user,DAY,actions=actions,meals=[],summary='调整',constraints={},reason='test',base_version=before['plan_version']);db.commit()
        get=lambda date:env.http.get('/api/v1/agent/day',headers=env.headers,params={'date':str(date),'fallback_next':'false'}).json()['data']
        now=get(DAY);assert [a['sets'] for a in now['actions']]==[1,2];assert now['total_minutes']==11 and now['meals']==[]
        assert now['date']==str(DAY) and now['weekday']==DAY.isoweekday() and now['adjustment']
        week=get(DAY+dt.timedelta(days=7));assert week['source']=='plan' and week['adjustment'] is None
        assert week['actions']!=now['actions']
    finally:env.close()
