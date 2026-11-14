"""Continuous SQLite regressions: old facts survive, new cycle has zero credit."""
import copy
import datetime as dt
import pytest
from sqlalchemy import select,func
from app.models import User,WorkoutSession,WorkoutLog,WorkoutReplanAuthorization,PlanAdjustment
from app.seed import DEMO_USER_ID
from app.evals.dialogue_runner import DAY
from app.domain import workout,workout_records as records,workout_replan as replan,day_revision as revisions
from app.domain.day_plan import effective_day
from app.agent.workout_capabilities import FinishSession,RecordProgress
from tests.test_workout_progress import progress_chat
from tests.test_semantic_binding import task
from tests.test_workout_records import confirm


def legacy_finished(env,sid):
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        records.finish(db,u,sid,FinishSession(expected_revision=records.revision(s),completion_basis='recorded_only',
            item_overrides=[{'item_id':i['id'],'actual_sets':1 if i['slot']=='warmup' else 2} for i in s.items]));db.commit()
        # Reproduce stale historical flags without changing the real 7/10 counts.
        items=copy.deepcopy(s.items)
        for i in items:i['status']='done'
        s.items=items;db.commit()


def authorized_draft(db,u):
    ctx=revisions.context(db,u,DAY);request=replan.request(db,u,DAY,scope=['workout'],expected_base_revision=revisions.digest(ctx),goal_text='重排20分钟，保留已有记录')
    replan.approve(db,u,request['authorization_id'],request['etag'])
    proposal={'actions':[{'id':a['id'],'sets':1 if a['slot']=='warmup' else 2,'duration_min':5} for a in ctx['day']['actions']]}
    draft=revisions.create(db,u,DAY,scope=['workout'],expected_base_revision=revisions.digest(ctx),proposal=proposal,
        workout_policy='replace_remaining',replan_authorization_id=request['authorization_id'])
    return request,draft


def test_finished_seventy_percent_is_not_completed_and_stale_done_flags_are_read_only(progress_chat):
    env,model,sid,post=progress_chat;legacy_finished(env,sid)
    with env.factory() as db:before=records.revision(db.get(WorkoutSession,sid))
    data=env.http.get('/api/v1/agent/day',headers=env.headers,params={'date':str(DAY)}).json()['data']
    assert data['finished'] and data['completed'] is False
    assert data['day_progress']['done_sets']==7 and data['day_progress']['total_sets']==10
    assert [i['status'] for i in data['session']['items']]==['done','partial','partial','partial']
    model.responses.append(lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(next(o['id'] for o in d['snapshot']['objects'] if o['type']=='day'),'context.inspect').model_dump()]})
    result=post('帮我安排一下今天的训练')
    assert '尚未全部完成' in result['reply'] and '7/10' in result['reply']
    with env.factory() as db:assert records.revision(db.get(WorkoutSession,sid))==before


def test_confirmed_replace_preserves_old_facts_but_same_actions_start_at_zero(progress_chat):
    env,model,sid,post=progress_chat;legacy_finished(env,sid)
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);old=copy.deepcopy(db.get(WorkoutSession,sid).items)
        log_before=[records.log_data(l) for l in db.scalars(select(WorkoutLog))]
        auth,draft=authorized_draft(db,u);db.commit()
        # Approval and proposal alone do not abandon the current plan.
        assert replan.active_cycle(db,u,DAY) is None
        assert effective_day(db,u,DAY)['actions'][1]['sets']==3
        applied=revisions.apply(db,u,draft['revision_id'],confirmed_etag=draft['etag']);db.commit()
        assert applied['status']=='applied'
        assert db.get(WorkoutSession,sid).items==old
        assert [records.log_data(l) for l in db.scalars(select(WorkoutLog))]==log_before
    day=env.http.get('/api/v1/agent/day',headers=env.headers,params={'date':str(DAY)}).json()['data']
    assert day['session'] is None and day['day_progress']['done_sets']==0 and day['day_progress']['total_sets']==7
    assert day['completed'] is False and sum(i['completed_sets'] for i in day['previous_training'])==7
    active=env.http.get('/api/v1/workout-sessions/active',headers=env.headers,params={'date':str(DAY)}).json()['data']
    assert active['session'] is None and active['day']['day_progress']['done_sets']==0
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);new=workout.start_session(db,u,DAY)
        assert new.id!=sid and all(i['completed_sets']==0 for i in new.items)
        assert all(i['workout_cycle_id']==auth['authorization_id'] for i in new.items)
        records.progress(db,u,new.id,new.items[0]['id'],RecordProgress(expected_revision=records.revision(new)));db.commit()
    day=env.http.get('/api/v1/agent/day',headers=env.headers,params={'date':str(DAY)}).json()['data']
    assert day['day_progress']['done_sets']==1 and sum(i['completed_sets'] for i in day['previous_training'])==7


def test_replacement_requires_real_unexpired_scope_bound_confirmation(progress_chat):
    env,model,sid,post=progress_chat
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx=revisions.context(db,u,DAY)
        kwargs={'scope':['workout'],'proposal':{'actions':[{'id':a['id']} for a in ctx['day']['actions']]},'expected_base_revision':revisions.digest(ctx),'workout_policy':'replace_remaining'}
        with pytest.raises(revisions.RevisionError) as e:revisions.create(db,u,DAY,**kwargs)
        assert e.value.code=='replan_consent'
        auth=replan.request(db,u,DAY,scope=['workout'],expected_base_revision=revisions.digest(ctx),goal_text='重新安排')
        with pytest.raises(revisions.RevisionError):revisions.create(db,u,DAY,**kwargs,replan_authorization_id=auth['authorization_id'])
        replan.approve(db,u,auth['authorization_id'],auth['etag']);db.commit()
        row=db.get(WorkoutReplanAuthorization,auth['authorization_id']);row.expires_at=dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=1);db.commit()
        with pytest.raises(revisions.RevisionError):revisions.create(db,u,DAY,**kwargs,replan_authorization_id=auth['authorization_id'])


def test_partial_live_work_is_persisted_once_when_old_plan_is_superseded(progress_chat):
    env,model,sid,post=progress_chat
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        item=s.items[1]
        records.progress(db,u,sid,item['id'],RecordProgress(expected_revision=records.revision(s),completion_basis='actual',actual_sets=1,reps=8));db.commit()
        old_items=copy.deepcopy(s.items);assert list(db.scalars(select(WorkoutLog)))==[]
        auth,draft=authorized_draft(db,u);db.commit()
        revisions.apply(db,u,draft['revision_id'],confirmed_etag=draft['etag']);db.commit()
        assert db.get(WorkoutSession,sid).status=='superseded' and db.get(WorkoutSession,sid).items==old_items
        logs=list(db.scalars(select(WorkoutLog)));assert len(logs)==1 and logs[0].sets==1
        assert logs[0].details['set_logs'][0]['reps']==8 and logs[0].details['set_logs'][0]['weight_kg'] is None
        revisions.apply(db,u,draft['revision_id'],confirmed_etag=draft['etag']);db.commit()
        assert db.scalar(select(func.count()).select_from(WorkoutLog))==1


def test_cancel_and_stale_approval_do_not_discard_or_complete_training(progress_chat):
    env,model,sid,post=progress_chat
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);before=revisions.context(db,u,DAY)
        auth=replan.request(db,u,DAY,scope=['workout'],expected_base_revision=revisions.digest(before),goal_text='重新排')
        replan.cancel(db,u,auth['authorization_id'],auth['etag']);db.commit()
        assert revisions.context(db,u,DAY)==before
        auth=replan.request(db,u,DAY,scope=['workout'],expected_base_revision=revisions.digest(before),goal_text='重新排')
        s=db.get(WorkoutSession,sid);records.progress(db,u,sid,s.items[0]['id'],RecordProgress(expected_revision=records.revision(s)));db.commit()
        with pytest.raises(revisions.RevisionError) as e:replan.approve(db,u,auth['authorization_id'],auth['etag'])
        assert e.value.code=='conflict'
        assert db.get(WorkoutReplanAuthorization,auth['authorization_id']).status=='pending'


def test_replan_apply_failure_rolls_back_authorization_archive_logs_and_target(progress_chat,monkeypatch):
    env,model,sid,post=progress_chat
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        records.progress(db,u,sid,s.items[1]['id'],RecordProgress(expected_revision=records.revision(s),completion_basis='actual',actual_sets=1));db.commit()
        auth,draft=authorized_draft(db,u);db.commit();before=revisions.context(db,u,DAY)
        actual=revisions.save_override
        def broken(*a,**kw):
            out=actual(*a,**kw);db.get(PlanAdjustment,out['adjustment_id']).actions=[];db.flush();return out
        monkeypatch.setattr(revisions,'save_override',broken)
        with pytest.raises(revisions.RevisionError):revisions.apply(db,u,draft['revision_id'],confirmed_etag=draft['etag'])
        db.commit()
        assert revisions.context(db,u,DAY)==before and replan.active_cycle(db,u,DAY) is None
        assert db.get(WorkoutReplanAuthorization,auth['authorization_id']).status=='approved'
        assert db.get(WorkoutSession,sid).status=='active'


def request_plan(d):
    root=next(o for o in d['snapshot']['objects'] if o['type']=='day')
    return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(root['id'],'day_plan.request_replan',expected_base_revision=root['revision_base_revision'],scope=['workout']).model_dump()]}


def replacement_plan(d):
    root=next(o for o in d['snapshot']['objects'] if o['type']=='day')
    auth=next(o for o in d['snapshot']['objects'] if o['type']=='workout_replan' and o['status']=='approved')
    actions=[{'id':a['id'],'sets':1 if a['slot']=='warmup' else 2,'duration_min':5} for a in root['revision_context']['day']['actions']]
    return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(root['id'],'day_plan.create_revision',
        expected_base_revision=root['revision_base_revision'],scope=['workout'],proposal={'actions':actions},
        workout_policy='replace_remaining',replan_authorization_id=auth['id']).model_dump()]}


def test_continuous_request_confirm_generate_editable_draft_apply_and_new_progress(progress_chat):
    from tests.test_day_revision_chat import load
    env,model,sid,post=progress_chat;legacy_finished(env,sid)
    before=env.snapshot()['days'];model.responses.extend([load,request_plan])
    first=post('保留已经记录的，重新安排今天20分钟训练')
    assert first['overall_status']=='confirmation_required' and first['workout_replan']['remaining_sets']==3,first
    assert first.get('day_revision') is None and env.snapshot()['days']==before
    model.responses.append(replacement_plan)
    drafted=confirm(post,first)
    assert drafted['overall_status']=='confirmation_required' and drafted['day_revision']['workout_policy']=='replace_remaining',drafted
    assert env.snapshot()['days']==before
    assert drafted['workout_replan']['status']=='approved'
    done=confirm(post,drafted)
    assert done['overall_status']=='success' and done['day_revision']['status']=='applied',done
    data=env.http.get('/api/v1/agent/day',headers=env.headers,params={'date':str(DAY)}).json()['data']
    assert data['day_progress']['done_sets']==0 and data['completed'] is False
    assert sum(r['completed_sets'] for r in data['previous_training'])==7
    confirm(post,first);confirm(post,drafted)
    with env.factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkoutReplanAuthorization))==1


def test_cancel_replan_keeps_original_plan_and_all_recorded_facts(progress_chat):
    from tests.test_day_revision_chat import load
    env,model,sid,post=progress_chat;legacy_finished(env,sid);before=env.snapshot()
    model.responses.extend([load,request_plan]);asked=post('重排今日剩余')
    p=asked['pending_confirmation'];cancelled=post('继续旧安排',confirmation={'action':'cancel','pending_id':p['pending_id'],'version':p['version']})
    assert cancelled['workout_replan']['status']=='cancelled'
    after=env.snapshot();assert after['days']==before['days']
    for name in ['workout_sessions','workout_logs','meal_logs']:assert after['tables'][name]==before['tables'][name]


def test_authorization_cannot_be_used_for_other_user_or_expanded_meal_scope(progress_chat):
    env,model,sid,post=progress_chat
    from types import SimpleNamespace
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx=revisions.context(db,u,DAY)
        auth=replan.request(db,u,DAY,scope=['workout'],expected_base_revision=revisions.digest(ctx),goal_text='重新排');replan.approve(db,u,auth['authorization_id'],auth['etag']);db.commit()
        with pytest.raises(revisions.RevisionError):replan.owned(db,SimpleNamespace(id='other-user'),auth['authorization_id'])
        with pytest.raises(revisions.RevisionError):replan.authorized(db,u,auth['authorization_id'],DAY,['workout','dinner'],revisions.digest(ctx))


def test_cycle_progress_never_falls_back_to_old_catalog_reference(progress_chat):
    env,model,sid,post=progress_chat;legacy_finished(env,sid)
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);old=db.get(WorkoutSession,sid)
        current=effective_day(db,u,DAY)
        new=[{**a,'workout_cycle_id':'new-cycle','id':'new-'+str(n)} for n,a in enumerate(current['actions'])]
        projected=workout.progress_for_day(old,new)
        assert projected['done_sets']==0 and projected['remaining_minutes']>0


def test_continue_partial_finished_session_only_prescribes_remaining_sets(progress_chat):
    env,model,sid,post=progress_chat;legacy_finished(env,sid)
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);new=workout.start_session(db,u,DAY)
        assert new.id!=sid and [i['target_sets'] for i in new.items]==[1,1,1]
        assert all(i['completed_sets']==0 for i in new.items)
        goal=effective_day(db,u,DAY)['actions'];p=workout.progress_for_day(new,goal)
        assert p['done_sets']==7 and p['total_sets']==10
        assert [i['remaining_sets'] for i in p['items']]==[0,1,1,1]
        records.finish(db,u,new.id,FinishSession(expected_revision=records.revision(new),completion_basis='confirm_current_plan'));db.commit()
        p=workout.progress_for_day(new,goal);assert p['done_sets']==10 and p['total_sets']==10
        logs=list(db.scalars(select(WorkoutLog).where(WorkoutLog.session_id==new.id)));assert sum(i.sets for i in logs)==3
        with pytest.raises(ValueError,match='确认重排'):workout.start_session(db,u,DAY)


def test_live_partial_chat_replan_archives_actual_set_and_starts_new_cycle(progress_chat):
    from tests.test_day_revision_chat import load
    env,model,sid,post=progress_chat
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        records.progress(db,u,sid,s.items[1]['id'],RecordProgress(expected_revision=records.revision(s),completion_basis='actual',actual_sets=1,reps=8));db.commit()
        original=copy.deepcopy(s.items)
    model.responses.extend([load,request_plan,replacement_plan])
    requested=post('旧的剩余训练不继续了，已有一组留档，重排今天20分钟')
    drafted=confirm(post,requested);assert drafted['day_revision']['status']=='awaiting_confirmation'
    applied=confirm(post,drafted);assert applied['overall_status']=='success',applied
    with env.factory() as db:
        assert db.get(WorkoutSession,sid).status=='superseded'
        assert db.get(WorkoutSession,sid).items==original
        logs=list(db.scalars(select(WorkoutLog)));assert len(logs)==1 and logs[0].sets==1
    day=env.http.get('/api/v1/agent/day',headers=env.headers,params={'date':str(DAY)}).json()['data']
    assert day['day_progress']['done_sets']==0 and sum(i['completed_sets'] for i in day['previous_training'])==1


def test_existing_noncatalog_identity_error_is_repaired_by_model_before_execution(progress_chat):
    from tests.test_day_revision_chat import load
    env,model,sid,post=progress_chat;legacy_finished(env,sid);before=env.snapshot()['days']
    def missing_identity(data):
        result=replacement_plan(data)
        action=result['tasks'][0]['arguments']['proposal']['actions'][0]
        root=next(o for o in data['snapshot']['objects'] if o['type']=='day')
        source=root['revision_context']['day']['actions'][0]
        action.pop('id',None);action['action_ref']=source['action_ref']
        return result
    model.responses.extend([load,request_plan,missing_identity,replacement_plan])
    requested=post('剩下的旧内容先不练，已经做的留着，给今天另定一套十五分钟的新目标。')
    drafted=confirm(post,requested)
    assert drafted['overall_status']=='confirmation_required',drafted
    assert any(t['stage']=='arguments_schema_repair' for t in drafted['trace'])
    assert env.snapshot()['days']==before
    assert drafted['day_revision']['workout_policy']=='replace_remaining'
    assert sum(i['completed_sets'] for i in drafted['day_revision']['previous_training'])==7


def test_read_old_records_also_disambiguates_new_zero_progress(progress_chat):
    from tests.test_day_revision_chat import load
    env,model,sid,post=progress_chat
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        records.progress(db,u,sid,s.items[1]['id'],RecordProgress(expected_revision=records.revision(s),completion_basis='actual',actual_sets=1,reps=8));db.commit()
    model.responses.extend([load,request_plan,replacement_plan])
    first=post('之前没做的先放弃，重新排十六分钟，已练一组保留')
    drafted=confirm(post,first);confirm(post,drafted);before=env.snapshot()
    model.responses.append(lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(sid,'workout.read_records').model_dump()]})
    result=post('新安排现在应当还是零吧？前面那组单独查得到吗？')
    assert '重排前记录' in result['reply'] and '不计入当前新方案进度' in result['reply']
    assert '当前有效目标已记录 0/7 组' in result['reply']
    assert result['workout_records']['record_scope']=='prior_cycle'
    assert result['workout_records']['current_day_progress']['done_sets']==0
    after=env.snapshot();assert after['days']==before['days']
    for table in ['workout_sessions','workout_logs','meal_logs']:assert after['tables'][table]==before['tables'][table]
    model.responses.append(lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(next(o['id'] for o in d['snapshot']['objects'] if o['type']=='day'),'context.inspect').model_dump()]})
    inspected=post('新进度和留底组数分别是什么？')
    assert '0/7' in inspected['reply'] and '重排前记录单独保留' in inspected['reply'] and '合计 1 组' in inspected['reply']


def test_missing_warmup_is_preflight_constraint_and_model_repairs_without_fact_writes(progress_chat):
    from tests.test_day_revision_chat import load
    env,model,sid,post=progress_chat;legacy_finished(env,sid);before=env.snapshot()['days']
    def no_warmup(data):
        value=replacement_plan(data)
        value['tasks'][0]['arguments']['proposal']['actions'].pop(0)
        return value
    model.responses.extend([load,request_plan,no_warmup,replacement_plan])
    requested=post('不补原来的剩余了，已有组数保留，另定新目标')
    drafted=confirm(post,requested)
    assert drafted['overall_status']=='confirmation_required',drafted
    assert any(t['stage']=='arguments_schema_repair' for t in drafted['trace'])
    assert drafted['day_revision']['target_plan']['actions'][0]['slot']=='warmup'
    assert env.snapshot()['days']==before


def test_draft_request_policy_does_not_add_an_extra_confirmation_before_consent(progress_chat):
    from tests.test_day_revision_chat import load
    env,model,sid,post=progress_chat
    def overly_cautious(data):
        value=request_plan(data);value['tasks'][0]['needs_confirmation']=True
        return value
    model.responses.extend([load,overly_cautious,replacement_plan])
    asked=post('旧目标剩下的放弃，已练照存，重排新目标')
    assert asked['workout_replan']['status']=='pending'
    assert asked['pending_confirmation']['tasks'][0]['capability_id']=='day_plan.confirm_replan'
    drafted=confirm(post,asked)
    assert drafted['day_revision']['status']=='awaiting_confirmation'
    assert drafted['pending_confirmation']['tasks'][0]['capability_id']=='day_plan.apply_revision'


def test_temporary_withholding_is_semantically_reviewed_before_discard(progress_chat):
    from tests.test_day_revision_chat import load
    env,model,sid,post=progress_chat;legacy_finished(env,sid)
    model.responses.extend([load,request_plan,replacement_plan])
    first=post('保留旧记录重排新目标');drafted=confirm(post,first);before=env.snapshot()['days']
    model.responses.extend([
        lambda d:{'action':'reject','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],'execute_revision':False,'question':'暂不应用，保留预览'},
        lambda d:{'action':'retain','reply':'已保留草案预览，尚未应用。'}])
    waiting=post('暂时别应用，留在预览这里。')
    assert waiting['overall_status']=='confirmation_required' and waiting['day_revision']['status']=='awaiting_confirmation'
    assert waiting['day_revision']['revision_id']==drafted['day_revision']['revision_id']
    assert any(t['stage']=='draft_disposition_review' for t in waiting['trace'])
    assert env.snapshot()['days']==before
    applied=confirm(post,waiting);assert applied['overall_status']=='success'


def test_fully_completed_old_goal_does_not_block_a_separate_new_goal(progress_chat):
    env,model,sid,post=progress_chat
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        records.finish(db,u,sid,FinishSession(expected_revision=records.revision(s),completion_basis='confirm_current_plan'));db.commit()
        original=copy.deepcopy(s.items);auth,draft=authorized_draft(db,u)
        revisions.apply(db,u,draft['revision_id'],confirmed_etag=draft['etag']);db.commit()
        assert db.get(WorkoutSession,sid).items==original
    day=env.http.get('/api/v1/agent/day',headers=env.headers,params={'date':str(DAY)}).json()['data']
    assert not day['completed'] and day['day_progress']['done_sets']==0
    assert sum(i['completed_sets'] for i in day['previous_training'])==10
