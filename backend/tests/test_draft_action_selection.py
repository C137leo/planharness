"""Incremental contracts: action count and clarification are different from approval."""
import copy
import pytest
from app.domain import day_revision as revision, workout_replan
from app.evals.dialogue_runner import DAY
from app.models import User, DayPlanRevision, CoachPendingPlan
from app.seed import DEMO_USER_ID
from tests.test_day_revision import state
from tests.test_day_revision_chat import load,post
from tests.test_semantic_binding import Scripted,task
from tests.test_workout_records import confirm


def authorized_context(db, user):
    ctx=revision.context(db,user,DAY)
    auth=workout_replan.request(db,user,DAY,scope=['workout'],expected_base_revision=revision.digest(ctx),goal_text='重新制定训练目标')
    workout_replan.approve(db,user,auth['authorization_id'],auth['etag'])
    return ctx,auth


def proposal(ctx, count):
    warmup=next(a for a in ctx['day']['actions'] if a['slot']=='warmup')
    # Only the warmup is retained by ID; generated formal actions use real catalogue refs.
    refs=['goblet-squat','dumbbell-bench-press','dumbbell-bent-over-row','dead-bug','bird-dog','glute-bridge']
    return {'actions':[{'id':warmup['id'],'duration_min':5}]+[
        {'action_ref':ref,'slot':'strength','sets':2,'reps':10,'duration_min':5} for ref in refs[:count]]}


def create_replacement(db,user,ctx,auth,count,**constraints):
    return revision.create(db,user,DAY,scope=['workout'],proposal=proposal(ctx,count),
        expected_base_revision=revision.digest(ctx),workout_policy='replace_remaining',
        replan_authorization_id=auth['authorization_id'],constraints=constraints)


def test_new_cycle_default_limits_formal_actions_and_does_not_fill_time(state):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx,auth=authorized_context(db,u)
        with pytest.raises(revision.RevisionError) as error:create_replacement(db,u,ctx,auth,4,max_workout_minutes=45)
        assert error.value.code=='action_count'
        assert error.value.details=={'actual_actions':4,'max_workout_actions':3,'include_warmup':False}
        assert revision.context(db,u,DAY)==ctx
        draft=create_replacement(db,u,ctx,auth,3,max_workout_minutes=45)
        assert len(draft['target_plan']['actions'])==4 and draft['constraints']['max_workout_actions']==3
        assert draft['workout_minutes_after']==20


def test_excessive_model_proposal_is_repaired_before_draft_is_saved(state):
    env=state
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);_,auth=authorized_context(db,user);db.commit()
    def planned(count):
        def output(data):
            root=next(o for o in data['snapshot']['objects'] if o['type']=='day')
            return {'snapshot_id':data['snapshot']['snapshot_id'],'tasks':[task(root['id'],'day_plan.create_revision',
                scope=['workout'],expected_base_revision=root['revision_base_revision'],
                workout_policy='replace_remaining',replan_authorization_id=auth['authorization_id'],
                proposal=proposal({'day':root['revision_context']['day']},count),constraints={'max_workout_minutes':45}).model_dump()]}
        return output
    env.client.client.responses.extend([load,planned(6),planned(3)])
    before=env.snapshot();result=post(env,'重排训练先给我预览')
    assert result['overall_status']=='confirmation_required',result
    assert len(result['day_revision']['target_plan']['actions'])==4
    assert result['day_revision']['constraints']['max_workout_actions']==3
    assert any(t['stage']=='arguments_schema_repair' for t in result['trace'])
    assert env.snapshot()['days']==before['days']


@pytest.mark.parametrize('include_warmup,count,cap',[(False,4,4),(True,2,3)])
def test_explicit_quantity_and_warmup_count_override_default(state,include_warmup,count,cap):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx,auth=authorized_context(db,u)
        draft=create_replacement(db,u,ctx,auth,count,max_workout_actions=cap,include_warmup_in_action_count=include_warmup)
        assert len(draft['target_plan']['actions'])==count+1
        assert draft['constraints']['include_warmup_in_action_count']==include_warmup


def test_quantity_persists_on_edit_and_legacy_draft_is_not_retroactively_capped(state):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx,auth=authorized_context(db,u)
        draft=create_replacement(db,u,ctx,auth,3)
        with pytest.raises(revision.RevisionError) as error:
            revision.update_draft(db,u,draft['revision_id'],expected=draft['etag'],proposal=proposal(ctx,4))
        # Existing IDs in ctx are not the new-cycle draft IDs; use new refs for the test.
        assert error.value.code=='ownership'
        more=copy.deepcopy(draft['target_plan']['actions'])
        targets=[{'id':a['id']} for a in more]+[{'action_ref':'dead-bug','slot':'strength','sets':2,'duration_min':5}]
        with pytest.raises(revision.RevisionError) as error:
            revision.update_draft(db,u,draft['revision_id'],expected=draft['etag'],proposal={'actions':targets})
        assert error.value.code=='action_count'
        row=db.get(DayPlanRevision,draft['revision_id']);assert row.version==1
        # Old stored constraints are deliberately nullable: saved plans are not migrated on reads/apply.
        assert revision.Constraints.model_validate({'max_workout_minutes':45}).max_workout_actions is None


def seed_clarification(env, *, clarification=True):
    """Reproduce an old wide preview and a persisted incomplete edit, not a fake result."""
    from app.agent.business_snapshot import build_snapshot
    from app.agent.semantic_runtime import make_pending,pending_result
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx,auth=authorized_context(db,u)
        draft=create_replacement(db,u,ctx,auth,6,max_workout_actions=6,max_workout_minutes=45)
        snap=build_snapshot(db,u,env.thread,[str(DAY)],day_revision=True)
        edit=task(draft['revision_id'],'day_plan.update_revision' if clarification else 'day_plan.apply_revision',expected_revision=draft['etag'])
        edit.user_goal='将这份草案精简到3个正式动作，热身与餐食保留，暂不应用' if clarification else '应用已展示的新目标草案'
        edit.parameters_resolved=not clarification
        saved=make_pending(db,u,env.thread,snap,[edit],'请选要保留的3个正式动作；热身另列。' if clarification else '确认应用这份草案吗？')
        saved.result={'day_revision':draft}
        data=pending_result(saved);db.commit()
    return draft,data


class CompletionModel(Scripted):
    """Force the first controller's faulty confirm, independently resolve the goal."""
    def complete(self,messages,**kwargs):
        import json
        data=json.loads(messages[-1]['content'])
        if 'saved_tasks' in data:
            self.calls.append(data)
            return {'message':{'content':json.dumps({'approved_task_ids':[],
                'modified_task_ids':[t['task_id'] for t in data['saved_tasks'] if not t['parameters_resolved']],
                'new_request_text':'继续编辑原草案，保留高脚杯深蹲、哑铃卧推、哑铃俯身划船三个正式动作；热身及其他条件不变，先更新预览，不应用'})},'model':'independent-contract-stub'}
        return super().complete(messages,**kwargs)


def completed_edit(data):
    editing=next(m for m in data['recent_messages'] if 'editing_saved_task' in m)
    assert editing['saved_question'] and editing['editing_saved_task']['parameters_resolved'] is False
    draft=next(o for o in data['snapshot']['objects'] if o['type']=='day_revision' and o['id']==editing['editing_saved_task']['resolution']['selected_id'])
    chosen=[a for a in draft['target_plan']['actions'] if a['slot']=='warmup' or a['action_ref'] in {'goblet-squat','dumbbell-bench-press','dumbbell-bent-over-row'}]
    return {'snapshot_id':data['snapshot']['snapshot_id'],'tasks':[task(draft['id'],'day_plan.update_revision',
        expected_revision=draft['etag'],proposal={'actions':[{'id':a['id']} for a in chosen]},
        constraints={'max_workout_actions':3}).model_dump()]}


@pytest.mark.parametrize('boolean_resolution',[True,False])
def test_clarification_answer_materializes_parameters_before_preview_and_separate_apply(state,boolean_resolution):
    env=state;draft,pending=seed_clarification(env);before=env.snapshot()
    def faulty_confirm(d):
        return {'action':'confirm','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],
            'execute_revision':True,'parameter_resolutions':['t1'] if boolean_resolution else []}
    model=CompletionModel([faulty_confirm,load,completed_edit]);env.client.client=model
    edited=post(env,'高脚杯深蹲、哑铃卧推、哑铃俯身划船这三个保留即可')
    assert edited['overall_status']=='confirmation_required',edited
    current=edited['day_revision'];assert current['revision_id']==draft['revision_id'] and current['version']==2
    assert [a['action_ref'] for a in current['target_plan']['actions'] if a['slot']!='warmup']==['goblet-squat','dumbbell-bench-press','dumbbell-bent-over-row']
    assert current['constraints']['max_workout_actions']==3 and current['constraints']['max_workout_minutes']==45
    assert current['target_plan']['meals']==draft['target_plan']['meals']
    assert env.snapshot()['days']==before['days'] and current['status']=='awaiting_confirmation'
    with env.factory() as db:assert db.get(CoachPendingPlan,pending['pending_confirmation']['pending_id']).status=='cancelled'
    applied=confirm(lambda text,**kw:post(env,text,**kw),edited)
    assert applied['overall_status']=='success' and applied['day_revision']['status']=='applied'
    for table in ['profiles','meal_logs','workout_logs','plans','plan_versions']:
        assert env.snapshot()['tables'][table]==before['tables'][table]


def test_unresolved_parameter_button_cannot_approve_boolean_or_execute_missing_arguments(state):
    env=state;draft,pending=seed_clarification(env);before=env.snapshot()
    assert pending['pending_confirmation']['stage']=='clarification'
    assert not any(a['type']=='confirm_tasks' for a in pending['actions'])
    p=pending['pending_confirmation']
    result=post(env,'确认',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version'],'parameter_resolutions':['t1']})
    assert result['overall_status']=='confirmation_required' and result['pending_confirmation']['stage']=='clarification'
    assert env.snapshot()==before


def test_plain_yes_without_missing_information_keeps_clarification(state):
    env=state;draft,pending=seed_clarification(env);before=env.snapshot()
    env.client.client.responses.append(lambda d:{'action':'confirm','pending_id':d['pending']['pending_id'],
        'version':d['pending']['version'],'execute_revision':True,'parameter_resolutions':['t1']})
    result=post(env,'对')
    assert result['overall_status']=='confirmation_required'
    assert result['pending_confirmation']['pending_id']==pending['pending_confirmation']['pending_id']
    assert env.snapshot()==before


@pytest.mark.parametrize('bad',['invalid_arguments','wrong_operation'])
def test_completion_failure_preserves_original_pending_and_can_retry(state,bad):
    env=state;draft,pending=seed_clarification(env);before=env.snapshot()
    def control(d):return {'action':'confirm','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],'execute_revision':True}
    def invalid(d):
        value=completed_edit(d)
        if bad=='wrong_operation':
            value['tasks'][0]['capability_id']='day_plan.apply_revision'
            value['tasks'][0]['arguments']={'expected_revision':draft['etag']}
        else:value['tasks'][0]['parameters_resolved']=True;value['tasks'][0]['arguments']['proposal']={'actions':[{'action_ref':'not-a-catalog-action'}]}
        return value
    env.client.client=CompletionModel([control,load,invalid,invalid,invalid,invalid])
    result=post(env,'保留深蹲卧推和划船，其他不选')
    assert result['overall_status'] in {'failed','clarification_required'},result
    with env.factory() as db:
        assert db.get(CoachPendingPlan,pending['pending_confirmation']['pending_id']).status=='pending'
        assert db.get(DayPlanRevision,draft['revision_id']).version==1
    assert env.snapshot()['days']==before['days']
    env.client.client=CompletionModel([control,load,completed_edit])
    retry=post(env,'按刚才三个保留，先给我预览')
    assert retry['overall_status']=='confirmation_required' and retry['day_revision']['version']==2
