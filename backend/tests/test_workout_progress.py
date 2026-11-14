"""Continuous real SQLite regressions; Scripted model only verifies execution, not NLU."""
import copy
import pytest
from sqlalchemy import select
from app.evals.dialogue_runner import DialogueEnvironment,DAY
from app.agent.business_snapshot import build_snapshot
from app.agent.workout_capabilities import FinishSession
from app.domain import workout,workout_records as records
from app.domain.day_plan import effective_day,save_override
from app.models import User,WorkoutSession,WorkoutLog
from app.seed import DEMO_USER_ID
from tests.test_semantic_binding import Scripted,task
from tests.test_workout_records import confirm

@pytest.fixture
def progress_chat(tmp_path):
    model=Scripted([]);env=DialogueEnvironment(tmp_path/'progress.db','progress-regression',model)
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);current=effective_day(db,user,DAY)
        actions=[{'slot':'warmup','name':'动态热身','action_ref':'dynamic_warmup','sets':1,'duration_min':5}]
        actions +=[{**a,'sets':3,'reps':10,'duration_min':12} for a in current['actions']]
        save_override(db,user,DAY,actions=actions,meals=current['meals'],summary='进度测试安排',constraints={},reason='synthetic fixture',base_version=current['plan_version'])
        db.commit();session=workout.start_session(db,user,DAY);sid=session.id
    def post(text,**fields):
        r=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':text,'thread':env.thread,'date':str(DAY),**fields})
        assert r.status_code==200,r.text
        return r.json()['data']
    yield env,model,sid,post
    env.close()


def report(d,all_items=False):
    s=next(o for o in d['snapshot']['objects'] if o['type']=='session')
    items=[o for o in d['snapshot']['objects'] if o['type']=='session_item' and (all_items or o.get('slot')=='warmup')]
    return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(i['id'],'workout.record_progress',expected_revision=s['record_revision']).model_copy(update={'task_id':'p'+str(n)}).model_dump() for n,i in enumerate(items)]}


def reduce_plan(d):
    items=[o for o in d['snapshot']['objects'] if o['type']=='workout_action' and o.get('slot')=='strength']
    return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(i['id'],'workout.update_action',sets_delta=-1).model_copy(update={'task_id':'r'+str(n)}).model_dump() for n,i in enumerate(items)]}


def finish_defaults(d,override=False):
    s=next(o for o in d['snapshot']['objects'] if o['type']=='session')
    args={'expected_revision':s['record_revision'],'completion_basis':'confirm_current_plan'}
    if override:
        i=next(o for o in d['snapshot']['objects'] if o['type']=='session_item' and o.get('slot')=='strength')
        args['item_overrides']=[{'item_id':i['id'],'actual_sets':1,'duration_min':6}]
    return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(s['id'],'workout.finish_session',**args).model_dump()]}


def test_continuous_warmup_adjustment_default_preview_confirm_and_replay(progress_chat):
    env,model,sid,post=progress_chat;before=env.snapshot()['days']
    model.responses.append(report);r=post('我已经热身完了')
    assert r['overall_status']=='success' and r['workout_records']['session']['progress']['done_sets']==1,r
    assert r['workout_records']['session']['progress']['percent']==10
    model.responses.append(report);r=post('再说一次，热身已经做完了')
    assert r['workout_records']['session']['progress']['done_sets']==1
    model.responses.append(reduce_plan);r=post('今天余下三个正式动作各减一组')
    assert r['overall_status']=='success',r
    with env.factory() as db:
        s=db.get(WorkoutSession,sid);assert [i['target_sets'] for i in s.items]==[1,2,2,2]
        assert workout.session_payload(s)['progress']['total_sets']==7
    model.responses.append(finish_defaults);r=post('我所有的锻炼都做完了')
    assert r['overall_status']=='confirmation_required' and '按今日调整后的安排' in r['reply'] and '2 组' in r['reply'],r
    old=copy.deepcopy(r)
    with env.factory() as db:assert db.get(WorkoutSession,sid).status!='finished';assert list(db.scalars(select(WorkoutLog)))==[]
    r=confirm(post,r);assert r['overall_status']=='success',r
    data=r['workout_records'];assert [i['completed_sets'] for i in data['session']['items']]==[1,2,2,2]
    assert data['session']['progress']['percent']==100 and data['session']['actual_minutes']==41
    assert data['actual_minutes_source']=='confirmed_plan_default'
    assert all(set['weight_kg'] is None for log in data['logs'] for set in log['details']['set_logs'])
    confirm(post,old)
    with env.factory() as db:assert len(list(db.scalars(select(WorkoutLog))))==4
    assert env.snapshot()['days']!=before # only the intentionally adjusted date changes


def test_edit_pending_defaults_keeps_other_defaults_and_uses_user_actual_values(progress_chat):
    env,model,sid,post=progress_chat
    model.responses.extend([reduce_plan,finish_defaults]);post('正式动作各减一组');r=post('全做完了，给默认摘要')
    model.responses.extend([
        lambda d:{'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],
            'keep_task_ids':[],'execute_revision':False,'new_request_text':'其余按刚才完成摘要，深蹲实际只做1组用了6分钟，修改完成摘要再确认'},
        lambda d:finish_defaults(d,True)])
    r=post('其余照旧，深蹲只做了一组用了6分钟，先改摘要')
    assert r['overall_status']=='confirmation_required',r
    r=confirm(post,r);data=r['workout_records']
    assert [i['completed_sets'] for i in data['session']['items']]==[1,1,2,2]
    assert data['session']['items'][1]['reported_duration_min']==6
    assert data['session']['items'][2]['set_logs'][0]['source']=='confirmed_plan_default'


def test_multiple_progress_reports_in_same_plan_rebase_only_own_revision(progress_chat):
    env,model,sid,post=progress_chat;model.responses.append(lambda d:report(d,True))
    r=post('这些动作都做完了，更新每个动作进度')
    assert r['overall_status']=='success',r
    assert r['workout_records']['session']['progress']['done_sets']==10


def test_direct_session_target_override_is_not_reset_by_unchanged_day(progress_chat):
    env,model,sid,post=progress_chat
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        items=copy.deepcopy(s.items);items[1]['target_sets']=2;s.items=items;db.commit()
        build_snapshot(db,user,env.thread,[str(DAY)])
        assert db.get(WorkoutSession,sid).items[1]['target_sets']==2


def test_reduced_target_cannot_erase_already_recorded_sets(progress_chat):
    env,model,sid,post=progress_chat
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        workout.complete_set(db,user,sid)
        workout.complete_set(db,user,sid,reps=8);workout.complete_set(db,user,sid,reps=9)
        value=effective_day(db,user,DAY);actions=copy.deepcopy(value['actions']);actions[1]['sets']=1
        with pytest.raises(ValueError):
            with db.begin_nested():save_override(db,user,DAY,actions=actions,meals=value['meals'],summary='invalid',constraints={},reason='test',base_version=value['plan_version'])
        assert db.get(WorkoutSession,sid).items[1]['completed_sets']==2


def test_default_confirmation_stale_after_other_plan_change(progress_chat):
    env,model,sid,post=progress_chat;model.responses.append(finish_defaults);r=post('全做完了')
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);v=effective_day(db,user,DAY);actions=copy.deepcopy(v['actions']);actions[1]['sets']=2
        save_override(db,user,DAY,actions=actions,meals=v['meals'],summary='changed',constraints={},reason='test',base_version=v['plan_version']);db.commit()
    r=confirm(post,r);assert r['overall_status']=='stale'
    with env.factory() as db:assert list(db.scalars(select(WorkoutLog)))==[]


def test_controller_cannot_only_change_question_while_retaining_old_completion_args(progress_chat,monkeypatch):
    from app.agent import semantic_planner
    env,model,sid,post=progress_chat;model.responses.extend([reduce_plan,finish_defaults]);post('正式动作减组');r=post('所有都完成，生成摘要')
    original=semantic_planner.review_mixed_authorization
    def review(client,text,pending,command,trace):
        if command.action=='revise':
            return command.model_copy(update={'keep_task_ids':[],'new_request_text':'其余照刚才完成摘要，深蹲只做1组用了6分钟，重新预览'} )
        return original(client,text,pending,command,trace)
    monkeypatch.setattr(semantic_planner,'review_mixed_authorization',review)
    model.responses.extend([
        lambda d:{'action':'revise','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],
            'keep_task_ids':[d['pending']['tasks'][0]['task_id']],'execute_revision':False,'question':'已将深蹲改成1组6分钟，请确认'},
        lambda d:finish_defaults(d,True)])
    r=post('深蹲只做1组用了6分钟，其他按默认完成，先改摘要')
    assert r['pending_confirmation']['tasks'][0]['arguments']['item_overrides'][0]['actual_sets']==1,r
    r=confirm(post,r);assert r['workout_records']['session']['items'][1]['completed_sets']==1


def test_invalid_argument_is_repaired_by_model_without_python_dropping_fields(progress_chat):
    env,model,sid,post=progress_chat
    def invalid(d):
        plan=finish_defaults(d);plan['tasks'][0]['arguments']['unsupported_field']=True;return plan
    model.responses.extend([invalid,lambda d:finish_defaults(d)])
    r=post('全做完，给我摘要')
    assert r['overall_status']=='confirmation_required',r
    assert any(x['stage']=='arguments_schema_repair' for x in r['trace'])
    assert 'unsupported_field' not in r['pending_confirmation']['tasks'][0]['arguments']


def test_workflow_edit_context_preserves_only_the_explicitly_modified_saved_task(progress_chat):
    env,model,sid,post=progress_chat;model.responses.append(finish_defaults);r=post('已经全部完成，先给摘要')
    old=r['pending_confirmation']['tasks'][0]['task_id']
    def replan(d):
        refs=[x['editing_saved_task'] for x in d['recent_messages'] if x.get('editing_saved_task')]
        assert len(refs)==1 and refs[0]['capability_id']=='workout.finish_session'
        assert refs[0]['arguments']['completion_basis']=='confirm_current_plan'
        return finish_defaults(d,True)
    model.responses.extend([
        lambda d:{'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],
            'keep_task_ids':[old],'edit_task_ids':[old],'execute_revision':False,'new_request_text':'深蹲只1组6分钟，其它照旧，先改摘要'},replan])
    r=post('深蹲只1组6分钟，其它照旧，先改摘要')
    assert r['overall_status']=='confirmation_required' and len(r['pending_confirmation']['tasks'])==1,r
    assert r['pending_confirmation']['tasks'][0]['capability_id']=='workout.finish_session'
    assert r['pending_confirmation']['tasks'][0]['arguments']['item_overrides'][0]['actual_sets']==1


def test_explicit_repetitions_override_existing_and_default_values_in_actual_summary(progress_chat):
    from app.agent.workout_capabilities import CompletionOverride
    env,model,sid,post=progress_chat
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        workout.complete_set(db,user,sid);workout.complete_set(db,user,sid,reps=10)
        s=db.get(WorkoutSession,sid);iid=s.items[1]['id']
        data=records.finish(db,user,sid,FinishSession(expected_revision=records.revision(s),completion_basis='confirm_current_plan',
            item_overrides=[CompletionOverride(item_id=iid,actual_sets=2,reps=8,duration_min=4)]))
        item=next(i for i in data['session']['items'] if i['id']==iid)
        assert [x['reps'] for x in item['set_logs']]==[8,8]
        assert item['reported_reps']==8 and item['target_reps']==10


def test_confirming_preview_authorizes_saved_effect_without_treating_wait_state_as_parameter_edit(progress_chat,monkeypatch):
    from app.agent import semantic_planner
    env,model,sid,post=progress_chat
    def preview(d):
        plan=finish_defaults(d,True);plan['tasks'][0]['user_goal']='仅生成完成摘要，暂不保存，等待确认';return plan
    model.responses.append(preview);post('给我完成摘要先别保存')
    model.responses.append(lambda d:{'action':'confirm','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],'execute_revision':False})
    def forbidden_review(*args):raise AssertionError('ordinary confirmation is not a parameter change')
    monkeypatch.setattr(semantic_planner,'review_mixed_authorization',forbidden_review)
    r=post('这次摘要没问题，确认完成并保存')
    assert r['overall_status']=='success',r
    assert r['workout_records']['session']['status']=='finished' and r['workout_records']['session']['items'][1]['completed_sets']==1


def test_finished_legacy_targets_project_to_today_without_rewriting_training_facts(progress_chat):
    env,model,sid,post=progress_chat
    from app.agent.workout_capabilities import FinishSession
    from app.domain.workout_records import revision
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        # Reproduce an older session: actual warmup + two sets each, old targets 1/3/3/3.
        args=FinishSession(expected_revision=revision(s),completion_basis='confirm_current_plan',
            item_overrides=[{'item_id':i['id'],'actual_sets':1 if i['slot']=='warmup' else 2} for i in s.items])
        records.finish(db,user,sid,args);db.commit();s=db.get(WorkoutSession,sid)
        before=revision(s);assert workout.session_payload(s)['progress']['total_sets']==10
        current=effective_day(db,user,DAY)
        actions=[{**a,'sets':1 if a['slot']=='warmup' else 2} for a in current['actions']]
        save_override(db,user,DAY,actions=actions,meals=current['meals'],summary='调整后7组',constraints={},reason='synthetic',base_version=current['plan_version']);db.commit()
    r=env.http.get('/api/v1/agent/day',headers=env.headers,params={'date':str(DAY),'fallback_next':'false'})
    assert r.status_code==200,r.text
    day=r.json()['data'];assert day['day_progress']['done_sets']==7 and day['day_progress']['total_sets']==7
    assert day['day_progress']['percent']==100 and day['day_progress']['remaining_minutes']==0
    assert day['session']['progress']['total_sets']==10 # original session snapshot remains historical
    with env.factory() as db:
        s=db.get(WorkoutSession,sid);assert revision(s)==before
        assert sum(l.sets for l in db.scalars(select(WorkoutLog).where(WorkoutLog.session_id==sid)))==7


def test_daily_progress_excludes_removed_work_from_plan_and_does_not_credit_replacements(progress_chat):
    env,model,sid,post=progress_chat
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        items=copy.deepcopy(s.items)
        for i in items:i.update(completed_sets=i['target_sets'],status='done')
        s.items=items
        current=effective_day(db,user,DAY)
        reduced=[{**a,'sets':1 if a['slot']=='warmup' else 2} for a in current['actions']]
        view=workout.progress_for_day(s,reduced)
        assert (view['done_sets'],view['total_sets'],view['percent'],view['additional_recorded_sets'])==(7,7,100,3)
        replaced=[{**reduced[0],'action_ref':'different-action'}]
        view=workout.progress_for_day(s,replaced)
        assert view['done_sets']==0 and view['additional_recorded_sets']==10
        assert workout.progress_for_day(s,[])['total_sets']==0


def test_per_action_remaining_sets_use_current_day_after_restore(progress_chat):
    env,model,sid,post=progress_chat
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        items=copy.deepcopy(s.items)
        for i in items:i.update(completed_sets=1 if i['slot']=='warmup' else 2,status='done')
        s.items=items;s.status='finished';db.commit()
        day=effective_day(db,u,DAY);view=workout.progress_for_day(s,day['actions'])
        assert view['total_sets']==10 and [i['remaining_sets'] for i in view['items']]==[0,1,1,1]
        assert [i['completed_sets'] for i in view['items']]==[1,2,2,2]
        snap=build_snapshot(db,u,env.thread,[str(DAY)]);d=next(o for o in snap.payload['objects'] if o['type']=='day')
        assert d['current_actions'][1]['sets']==3
        assert next(o for o in snap.payload['objects'] if o['type']=='session')['target_basis']=='historical_session'
    model.responses.append(lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(next(o['id'] for o in d['snapshot']['objects'] if o['type']=='day'),'context.inspect').model_dump()]})
    result=post('恢复之后重新看看当前安排和剩余组数')
    assert '还差 1 组' in result['reply'] and '3 组' in result['reply']
