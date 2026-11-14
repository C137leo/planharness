"""Real SQLite lifecycle regression: persisted approval is not current permission."""
import copy
import datetime as dt
import pytest
from app.agent.business_snapshot import build_snapshot
from app.domain import workout_replan as replan,day_revision
from app.domain.day_plan import effective_day,save_override
from app.models import User,WorkoutReplanAuthorization,utcnow
from app.evals.dialogue_runner import DAY
from app.seed import DEMO_USER_ID
from tests.test_workout_progress import progress_chat


def old_approval(env, *, expired=True):
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx=day_revision.context(db,u,DAY)
        data=replan.request(db,u,DAY,scope=['workout'],expected_base_revision=day_revision.digest(ctx),goal_text='重新安排今天20分钟训练，已有记录保留')
        replan.approve(db,u,data['authorization_id'],data['etag'])
        if expired:db.get(WorkoutReplanAuthorization,data['authorization_id']).expires_at=utcnow()-dt.timedelta(minutes=1)
        db.commit()
    return data


def test_expired_approved_row_is_not_exposed_as_usable_and_reads_do_not_rewrite_it(progress_chat):
    env,model,sid,post=progress_chat;data=old_approval(env);before=env.snapshot()
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);snap=build_snapshot(db,u,env.thread,[str(DAY)],day_revision=True)
        shown=next(o for o in snap.payload['objects'] if o['id']==data['authorization_id'])
        assert shown['status']=='expired' and shown['stored_status']=='approved' and not shown['usable_for_creation']
        assert db.get(WorkoutReplanAuthorization,data['authorization_id']).status=='approved'
    response=env.http.get('/api/v1/agent/workout-replans/'+data['authorization_id'],headers=env.headers).json()['data']
    assert response['status']=='expired' and not response['usable_for_creation']
    assert env.snapshot()['days']==before['days']
    assert env.snapshot()['tables']['workout_replan_authorizations']==before['tables']['workout_replan_authorizations']


def test_old_approved_retry_cannot_bypass_expiry_or_new_facts(progress_chat):
    env,model,sid,post=progress_chat;data=old_approval(env)
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID)
        with pytest.raises(day_revision.RevisionError) as error:replan.approve(db,u,data['authorization_id'],data['etag'])
        assert error.value.code=='expired'
    fresh=old_approval(env,expired=False)
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);day=effective_day(db,u,DAY)
        save_override(db,u,DAY,actions=day['actions'],meals=day['meals'],summary='合成后续改变',constraints={},reason='synthetic mutation',base_version=day['plan_version']);db.commit()
        row=db.get(WorkoutReplanAuthorization,fresh['authorization_id'])
        shown=replan.payload(row,day_revision.digest(day_revision.context(db,u,DAY)))
        assert shown['status']=='stale' and not shown['usable_for_creation']
        with pytest.raises(day_revision.RevisionError) as error:replan.approve(db,u,row.id,fresh['etag'])
        assert error.value.code=='conflict'


def test_applied_cycle_remains_active_after_its_approval_window_ends(progress_chat):
    env,model,sid,post=progress_chat;data=old_approval(env)
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);row=db.get(WorkoutReplanAuthorization,data['authorization_id']);row.status='applied';row.applied_at=utcnow();db.commit()
        assert replan.active_cycle(db,u,DAY)==row.id
        assert replan.payload(row)['status']=='applied' and not replan.payload(row)['usable_for_creation']


def test_past_date_cannot_create_an_immediately_expired_confirmation(progress_chat):
    env,model,sid,post=progress_chat
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);past=DAY-dt.timedelta(days=1);ctx=day_revision.context(db,u,past)
        with pytest.raises(day_revision.RevisionError) as error:replan.request(db,u,past,scope=['workout'],expected_base_revision=day_revision.digest(ctx),goal_text='历史日期')
        assert error.value.code=='expired'


def direct_proposal(data, authorization_id):
    from tests.test_semantic_binding import task
    root=next(o for o in data['snapshot']['objects'] if o['type']=='day')
    actions=[{'id':a['id'],'sets':1 if a['slot']=='warmup' else 2,'duration_min':5} for a in root['revision_context']['day']['actions']]
    return {'snapshot_id':data['snapshot']['snapshot_id'],'tasks':[task(root['id'],'day_plan.create_revision',
        expected_base_revision=root['revision_base_revision'],scope=['workout'],proposal={'actions':actions},
        constraints={'max_workout_minutes':20},workout_policy='replace_remaining',replan_authorization_id=authorization_id).model_dump()]}


def natural_confirm(data):
    p=data['pending'];return {'action':'confirm','pending_id':p['pending_id'],'version':p['version'],'execute_revision':False}


@pytest.mark.parametrize('missing',[False,True])
def test_direct_creation_recovers_real_pending_then_natural_confirmation_and_preview(progress_chat,missing):
    from tests.test_workout_replan import legacy_finished,replacement_plan
    from tests.test_day_revision_chat import load
    from tests.test_workout_records import confirm
    from app.models import DayPlanRevision,CoachPendingPlan
    from sqlalchemy import select
    env,model,sid,post=progress_chat;legacy_finished(env,sid)
    old=old_approval(env);before=env.snapshot()
    model.responses.extend([load,lambda d:direct_proposal(d,None if missing else old['authorization_id'])])
    asked=post('给我重新生成一份训练计划')
    assert asked['overall_status']=='confirmation_required',asked
    assert asked['workout_replan']['status']=='pending' and asked['workout_replan']['authorization_id']!=old['authorization_id']
    assert asked['pending_confirmation']['tasks'][0]['capability_id']=='day_plan.confirm_replan'
    assert '未能执行' not in asked['reply'] and env.snapshot()['days']==before['days']
    with env.factory() as db:
        assert list(db.scalars(select(DayPlanRevision)))==[]
        assert db.get(CoachPendingPlan,asked['pending_confirmation']['pending_id']).status=='pending'
    model.responses.extend([natural_confirm,replacement_plan])
    drafted=post('我确认放弃原来的部分')
    assert drafted['overall_status']=='confirmation_required' and drafted['day_revision']['status']=='awaiting_confirmation',drafted
    assert env.snapshot()['days']==before['days']
    applied=confirm(post,drafted);assert applied['overall_status']=='success'
    day=env.http.get('/api/v1/agent/day',headers=env.headers,params={'date':str(DAY)}).json()['data']
    assert day['day_progress']['done_sets']==0 and sum(i['completed_sets'] for i in day['previous_training'])==7
    assert env.snapshot()['tables']['workout_logs']==before['tables']['workout_logs']


def test_expired_saved_confirmation_is_renewed_once_and_old_button_cannot_replay(progress_chat):
    from tests.test_day_revision_chat import load
    from tests.test_workout_replan import request_plan,replacement_plan
    from tests.test_workout_records import confirm
    from app.models import CoachPendingPlan
    from sqlalchemy import select,func
    env,model,sid,post=progress_chat;model.responses.extend([load,request_plan]);first=post('重新安排今天20分钟训练')
    with env.factory() as db:
        db.get(CoachPendingPlan,first['pending_confirmation']['pending_id']).expires_at=utcnow()-dt.timedelta(seconds=1)
        db.get(WorkoutReplanAuthorization,first['workout_replan']['authorization_id']).expires_at=utcnow()-dt.timedelta(seconds=1);db.commit()
    model.responses.append(natural_confirm);renewed=post('先放弃')
    assert renewed['overall_status']=='confirmation_required' and renewed['pending_confirmation']['pending_id']!=first['pending_confirmation']['pending_id']
    assert renewed['workout_replan']['status']=='pending'
    model.responses.extend([natural_confirm,replacement_plan]);drafted=post('确认放弃旧的未做部分，保留实际记录')
    assert drafted['day_revision']['status']=='awaiting_confirmation',drafted
    replay=confirm(post,first);assert replay['overall_status']=='expired'
    with env.factory() as db:assert db.scalar(select(func.count()).select_from(WorkoutReplanAuthorization))==2


def test_expired_other_user_authorization_is_not_converted_to_fresh_consent(progress_chat):
    from tests.test_day_revision_chat import load
    env,model,sid,post=progress_chat;old=old_approval(env)
    with env.factory() as db:
        db.add(User(id='other-authorization-owner',nickname='合成其他用户'));db.flush()
        db.get(WorkoutReplanAuthorization,old['authorization_id']).user_id='other-authorization-owner';db.commit()
    model.responses.extend([load,lambda d:direct_proposal(d,old['authorization_id'])]);failed=post('重新给我安排训练')
    assert failed['overall_status']=='failed' and not failed.get('pending_confirmation'),failed


def test_expired_authorization_with_unexpired_pending_also_renews_instead_of_failing(progress_chat):
    from tests.test_day_revision_chat import load
    from tests.test_workout_replan import request_plan,replacement_plan
    env,model,sid,post=progress_chat;model.responses.extend([load,request_plan]);first=post('保留记录，重新安排今日训练')
    with env.factory() as db:
        db.get(WorkoutReplanAuthorization,first['workout_replan']['authorization_id']).expires_at=utcnow()-dt.timedelta(seconds=1);db.commit()
    model.responses.append(natural_confirm);renewed=post('我确认')
    assert renewed['overall_status']=='confirmation_required' and renewed['workout_replan']['status']=='pending',renewed
    assert renewed['pending_confirmation']['pending_id']!=first['pending_confirmation']['pending_id']
    model.responses.extend([natural_confirm,replacement_plan]);drafted=post('按最新安排确认放弃旧未做部分')
    assert drafted['day_revision']['status']=='awaiting_confirmation',drafted


def test_valid_existing_approval_creates_preview_without_redundant_new_consent(progress_chat):
    from tests.test_workout_replan import legacy_finished
    from tests.test_day_revision_chat import load
    from sqlalchemy import select,func
    env,model,sid,post=progress_chat;legacy_finished(env,sid);data=old_approval(env,expired=False)
    model.responses.extend([load,lambda d:direct_proposal(d,data['authorization_id'])])
    drafted=post('继续生成我刚才确认的新目标')
    assert drafted['overall_status']=='confirmation_required' and drafted['day_revision']['status']=='awaiting_confirmation',drafted
    with env.factory() as db:assert db.scalar(select(func.count()).select_from(WorkoutReplanAuthorization))==1


def test_stale_approval_recovers_confirmation_against_latest_targets(progress_chat):
    from tests.test_workout_replan import legacy_finished
    from tests.test_day_revision_chat import load
    env,model,sid,post=progress_chat;legacy_finished(env,sid);data=old_approval(env,expired=False)
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);day=effective_day(db,u,DAY)
        actions=[{**a,'sets':1 if a['slot']=='warmup' else 2} for a in day['actions']]
        save_override(db,u,DAY,actions=actions,meals=day['meals'],summary='合成最新目标',constraints={},reason='synthetic change',base_version=day['plan_version']);db.commit()
    before=env.snapshot();model.responses.extend([load,lambda d:direct_proposal(d,data['authorization_id'])])
    asked=post('重新生成训练目标')
    assert asked['overall_status']=='confirmation_required' and asked['workout_replan']['status']=='pending',asked
    assert asked['workout_replan']['total_sets']==7 and asked['workout_replan']['remaining_sets']==0
    assert asked['task_results'][0]['reason']=='stale'
    assert env.snapshot()['days']==before['days'] and env.snapshot()['tables']['workout_logs']==before['tables']['workout_logs']


def seed_failed_history(env):
    from app.models import AgentMessage
    lines=[('user','请重新生成今天20分钟的训练，餐食不要动。'),
           ('agent','草案未能执行：请先确认是否放弃原方案未完成部分，再生成新目标。'),
           ('user','先放弃'),('agent','目前没有新草案可以取消，请说明放弃的对象。')]
    with env.factory() as db:
        for role,text in lines:
            db.add(AgentMessage(user_id=DEMO_USER_ID,thread=env.thread,role=role,text=text,result={}));db.flush()
        db.commit()


def test_resume_in_existing_failed_conversation_keeps_original_goal_and_history(progress_chat):
    from tests.test_day_revision_chat import load
    from tests.test_workout_replan import legacy_finished,request_plan,replacement_plan
    from tests.test_workout_records import confirm
    env,model,sid,post=progress_chat;legacy_finished(env,sid);old_approval(env);seed_failed_history(env)
    model.responses.extend([load,request_plan]);first=post('我确认放弃原来的部分')
    def with_history(data):
        assert any('20分钟' in str(m.get('text','')) for m in data['recent_messages'])
        assert any('没有新草案' in str(m.get('text','')) for m in data['recent_messages'])
        assert data['recent_messages'][-1]['confirmation_text']=='对，按刚才的要求来'
        return replacement_plan(data)
    model.responses.extend([natural_confirm,with_history]);drafted=post('对，按刚才的要求来')
    assert drafted['overall_status']=='confirmation_required' and drafted['day_revision']['workout_minutes_after']==20,drafted
    applied=confirm(post,drafted);assert applied['overall_status']=='success'
