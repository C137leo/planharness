"""Continuous authenticated API/SQLite regression, Scripted is not NLU evidence."""
from sqlalchemy import select
from app.evals.dialogue_runner import DAY
from app.models import DayPlanRevision,User,CoachTaskExecution
from app.seed import DEMO_USER_ID
from app.domain import day_revision as service
from tests.test_day_revision import state,shortened,dinner
from tests.test_semantic_binding import task
from tests.test_workout_records import confirm


def load(d):return {'snapshot_id':d['snapshot']['snapshot_id'],'mode':'load_context','context_request':{'day_revision':True}}

def create(d):
    root=next(o for o in d['snapshot']['objects'] if o['type']=='day')
    return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(root['id'],'day_plan.create_revision',
        expected_base_revision=root['revision_base_revision'],scope=['workout','dinner'],
        proposal={**shortened({'day':root['revision_context']['day']}),**dinner()},constraints={'max_workout_minutes':20}).model_dump()]}

def post(env,text,**fields):
    res=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':text,'thread':env.thread,'date':str(DAY),**fields})
    assert res.status_code==200,res.text
    return res.json()['data']


def test_continuous_create_edit_confirm_applies_latest_full_draft_and_preserves_other_resources(state):
    env=state;model=env.client.client
    before=env.snapshot();model.responses.extend([load,create]);first=post(env,'今天训练20分钟，晚餐换豆腐，先展示整份草案')
    assert first['overall_status']=='confirmation_required',first
    assert first['day_revision']['status']=='awaiting_confirmation'
    assert env.snapshot()['days']==before['days']
    ident=first['day_revision']['revision_id'];old=first['day_revision']['etag']
    model.responses.extend([
        lambda d:{'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],'execute_revision':False,'keep_task_ids':[],'edit_task_ids':['apply-day-revision'],'new_request_text':'保留训练草案，只把晚餐豆腐改为210克三文鱼，其余不变，先预览'},
        load,
        lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(ident,'day_plan.update_revision',expected_revision=next(o['etag'] for o in d['snapshot']['objects'] if o['type']=='day_revision' and o['id']==ident),proposal=dinner(210,'三文鱼')).model_dump()]}
    ])
    edited=post(env,'训练照旧，晚餐用210克三文鱼，再看看差异')
    assert edited['overall_status']=='confirmation_required',edited
    assert edited['day_revision']['revision_id']==ident and edited['day_revision']['version']==2
    assert edited['day_revision']['etag']!=old
    assert edited['day_revision']['target_plan']['actions']==first['day_revision']['target_plan']['actions']
    assert env.snapshot()['days']==before['days']
    result=confirm(lambda text,**kw:post(env,text,**kw),edited)
    assert result['overall_status']=='success',result
    assert result['day_revision']['status']=='applied' and result['day_revision']['result']['verified']
    final=env.snapshot();assert final['days'][str(DAY)]['actions']!=before['days'][str(DAY)]['actions']
    assert final['days'][str(DAY)]['meals']['lunch']==before['days'][str(DAY)]['meals']['lunch']
    for table in ['profiles','meal_logs','workout_logs','plan_versions','plans']:assert final['tables'][table]==before['tables'][table]
    replay=confirm(lambda text,**kw:post(env,text,**kw),edited)
    assert replay['confirmation_replayed'] and env.snapshot()['days']==final['days']
    with env.factory() as db:
        assert len(list(db.scalars(select(DayPlanRevision))))==1
        assert len(list(db.scalars(select(CoachTaskExecution).where(CoachTaskExecution.domain=='day_revision'))))==3


def test_cancel_button_cancels_persisted_draft_and_cross_user_read_rejected(state):
    env=state;model=env.client.client;model.responses.extend([load,create]);before=env.snapshot()['days']
    value=post(env,'生成草案先看')
    p=value['pending_confirmation'];cancel=post(env,'取消',confirmation={'action':'cancel','pending_id':p['pending_id'],'version':p['version']})
    assert cancel['day_revision']['status']=='cancelled' and env.snapshot()['days']==before
    ident=value['day_revision']['revision_id'];read=env.http.get('/api/v1/agent/day-revisions/'+ident,headers=env.headers)
    assert read.status_code==200 and read.json()['data']['status']=='cancelled'
    assert env.http.get('/api/v1/agent/day-revisions/'+ident).status_code==401


def test_external_edit_makes_saved_confirmation_stale(state):
    env=state;model=env.client.client;model.responses.extend([load,create]);first=post(env,'草案')
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID)
        # Direct domain edit after the saved confirmation makes that exact token stale.
        service.update_draft(db,user,first['day_revision']['revision_id'],expected=first['day_revision']['etag'],proposal=dinner(220));db.commit()
    stale=confirm(lambda text,**kw:post(env,text,**kw),first)
    assert stale['overall_status']=='failed',stale
    assert not stale['day_revision']['result'].get('verified')


def restore_to_three(env):
    """Actual long-plan rollback; all records and versions are synthetic."""
    from app.models import Plan,PlanVersion,PlanAction
    from app.domain.day_plan import effective_day,save_override
    from app.domain import workout,workout_records
    from app.agent.workout_capabilities import FinishSession
    from app.domain.patches import rollback_plan
    from sqlalchemy import delete
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);current=effective_day(db,u,DAY)
        plan=db.scalar(select(Plan).where(Plan.user_id==u.id))
        first=db.scalar(select(PlanVersion).where(PlanVersion.plan_id==plan.id,PlanVersion.version==1))
        db.execute(delete(PlanAction).where(PlanAction.plan_version_id==first.id,PlanAction.day_index==DAY.isoweekday()))
        for n,a in enumerate(current['actions']):
            db.add(PlanAction(plan_version_id=first.id,day_index=DAY.isoweekday(),slot=a['slot'],seq=n+1,slot_key=f'{DAY.isoweekday()}:{a["slot"]}:{n+1}',action_type='exercise',action_ref=a['action_ref'],sets=1 if a['slot']=='warmup' else 3,reps=10,duration_min=5 if a['slot']=='warmup' else 12,meta=a.get('meta') or {}))
        second=PlanVersion(plan_id=plan.id,version=2,parent_version=1,calorie_target=first.calorie_target,protein_target_g=first.protein_target_g,step_target=first.step_target,summary='synthetic reduced version')
        db.add(second);first.status='superseded';plan.current_version=2;db.flush()
        save_override(db,u,DAY,actions=[{**a,'sets':1 if a['slot']=='warmup' else 2} for a in current['actions']],meals=current['meals'],summary='旧版2组',constraints={},reason='synthetic',base_version=2);db.commit()
        s=workout.start_session(db,u,DAY)
        workout_records.finish(db,u,s.id,FinishSession(expected_revision=workout_records.revision(s),completion_basis='confirm_current_plan'));db.commit()
        rollback_plan(db,u,plan.id,1);db.commit()


def test_real_plan_rollback_current_day_is_three_historical_completed_sets_stay_two(state):
    env=state;restore_to_three(env)
    def inspect(d):
        root=next(o for o in d['snapshot']['objects'] if o['type']=='day')
        assert [a['sets'] for a in root['current_actions']]==[1,3,3,3]
        assert [i['remaining_sets'] for i in root['current_progress']['items']]==[0,1,1,1]
        historic=[o for o in d['snapshot']['objects'] if o['type']=='session_item' and o['slot']=='strength']
        assert all(i['target_sets']==2 and i['completed_sets']==2 and i['target_basis']=='historical_session' for i in historic)
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(root['id'],'context.inspect').model_dump()]}
    env.client.client.responses.append(inspect)
    result=post(env,'我刚回滚，重新读现在的计划和每项剩余组数')
    assert result['overall_status']=='success' and '还差 1 组' in result['reply'] and '3 组' in result['reply']
    assert result['day']['plan_version']==3


def test_invalid_ingredient_uuid_is_schema_error_model_repairs_without_python_guessing(state):
    env=state;model=env.client.client
    def bad(d):
        value=create(d);value['tasks'][0]['arguments']['proposal']['meals'][0]['items'][0]['ingredient_key']=next(o['id'] for o in d['snapshot']['objects'] if o['type']=='ingredient' and o['name']=='豆腐')
        return value
    model.responses.extend([load,bad,create])
    result=post(env,'按目录食材生成协调草案')
    assert result['overall_status']=='confirmation_required',result
    assert any(t['stage']=='arguments_schema_repair' for t in result['trace'])
    assert result['day_revision']['target_plan']['meals'][-1]['items'][0]['name']=='豆腐'


def test_read_snapshot_after_rollback_does_not_write_adjustment_or_rekey_actions(state):
    from app.agent.business_snapshot import build_snapshot
    from app.models import PlanAdjustment
    from sqlalchemy import func
    env=state;restore_to_three(env)
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);count=db.scalar(select(func.count()).select_from(PlanAdjustment))
        before=service.context(db,u,DAY)
        one=build_snapshot(db,u,env.thread,[str(DAY)]);two=build_snapshot(db,u,env.thread,[str(DAY)])
        assert one.revision==two.revision
        assert service.context(db,u,DAY)==before
        assert db.scalar(select(func.count()).select_from(PlanAdjustment))==count
        db.commit()


def test_revision_lifecycle_cannot_be_batched_with_a_local_write_or_old_apply(state):
    env=state;model=env.client.client
    def mixed(d):
        value=create(d);other=task(value['tasks'][0]['resolution']['selected_id'],'context.inspect').model_copy(update={'task_id':'second'}).model_dump();value['tasks'].append(other);return value
    before=env.snapshot()['days'];model.responses.extend([load,mixed])
    result=post(env,'生成草案同时把旧版应用')
    assert result['overall_status']=='clarification_required' and env.snapshot()['days']==before
    with env.factory() as db:assert list(db.scalars(select(DayPlanRevision)))==[]


def test_waiting_for_confirmation_keeps_saved_diff_without_business_writes(state):
    env=state;model=env.client.client;model.responses.extend([load,create]);first=post(env,'生成草案先看')
    before=env.snapshot()['days']
    model.responses.append(lambda d:{'action':'clarify','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],'execute_revision':False,'question':'方案保留，等你确认。'})
    waiting=post(env,'先不要应用，我再看看原方案')
    assert waiting['overall_status']=='confirmation_required'
    assert waiting['day_revision']['etag']==first['day_revision']['etag']
    assert env.snapshot()['days']==before
