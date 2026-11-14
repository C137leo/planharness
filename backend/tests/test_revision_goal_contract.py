"""Incremental real SQLite goals: duration, meal scope and semantic/tool agreement."""
import copy
import pytest
from app.domain import day_revision as revision
from app.models import User,DayPlanRevision,MealLog
from app.seed import DEMO_USER_ID
from app.evals.dialogue_runner import DAY
from tests.test_day_revision import state
from tests.test_draft_action_selection import authorized_context,proposal
from tests.test_semantic_binding import Scripted,task
from tests.test_day_revision_chat import post,load
from tests.test_workout_records import confirm


class GoalModel(Scripted):
    def __init__(self,responses,requirements):
        super().__init__(responses);self.requirements={'edit_existing_preview':True,**requirements}
    def complete(self,messages,**kwargs):
        import json
        data=json.loads(messages[-1]['content'])
        if 'revision_goal_input' in data:
            self.calls.append(data)
            return {'message':{'content':json.dumps(self.requirements)},'model':'independent-goal-contract-stub'}
        return super().complete(messages,**kwargs)


def pending_preview(env,draft):
    from app.agent.business_snapshot import build_snapshot
    from app.agent.semantic_runtime import make_pending,pending_result
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);snap=build_snapshot(db,u,env.thread,[str(DAY)],day_revision=True)
        apply=task(draft['revision_id'],'day_plan.apply_revision',expected_revision=draft['etag']);apply.task_id='apply-day-revision'
        p=make_pending(db,u,env.thread,snap,[apply],'确认应用这份草案吗？');p.result={'day_revision':draft};value=pending_result(p);db.commit()
    return value


def edit_control(text):
    return lambda d:{'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],
        'execute_revision':False,'edit_task_ids':['apply-day-revision'],'new_request_text':text}


def find_draft(data):return next(o for o in data['snapshot']['objects'] if o['type']=='day_revision' and o['status']=='awaiting_confirmation')


def edit_time(data,*,correct):
    draft=find_draft(data)
    return {'snapshot_id':data['snapshot']['snapshot_id'],'tasks':[task(draft['id'],'day_plan.update_revision',
        expected_revision=draft['etag'],proposal=fifty_minutes(draft) if correct else {},
        constraints={'max_workout_minutes':50,**({'target_workout_minutes':50} if correct else {})}).model_dump()]}


def dinner_requirement(exclude=False):
    return {'workout_change':False,'action_selection_change':False,'action_count_change':False,'meal_slots':['dinner'],
        'meal_rules':[{'slot':'dinner','ingredient_options':['牛肉'],'requirement':'exclude' if exclude else 'include'}]+
            ([{'slot':'dinner','ingredient_options':['鸡胸肉'],'requirement':'include'}] if exclude else [])}


def training_preview(env):
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);ctx,auth=authorized_context(db,user)
        actions=proposal(ctx,3)['actions']
        for a in actions:a['duration_min']=5 if a.get('id') else 8
        draft=revision.create(db,user,DAY,scope=['workout'],proposal={'actions':actions},
            expected_base_revision=revision.digest(ctx),workout_policy='replace_remaining',
            replan_authorization_id=auth['authorization_id'],constraints={'max_workout_minutes':45})
        db.commit()
    return draft


def fifty_minutes(draft):
    return {'actions':[{'id':a['id'],'duration_min':5 if a['slot']=='warmup' else 15} for a in draft['target_plan']['actions']]}


def beef(grams=150):
    return {'meals':[{'slot':'dinner','items':[{'ingredient_key':'牛肉','grams':grams},{'ingredient_key':'米饭','grams':150}]}]}


def test_target_fifty_rejects_old_twenty_nine_then_saves_actual_fifty(state):
    draft=training_preview(state);assert draft['workout_minutes_after']==29
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);original=revision.context(db,u,DAY)
        with pytest.raises(revision.RevisionError) as error:
            revision.update_draft(db,u,draft['revision_id'],expected=draft['etag'],proposal={},
                constraints={'target_workout_minutes':50,'max_workout_minutes':50})
        assert error.value.code=='time_target' and error.value.details['actual_minutes']==29
        assert db.get(DayPlanRevision,draft['revision_id']).version==1
        changed=revision.update_draft(db,u,draft['revision_id'],expected=draft['etag'],proposal=fifty_minutes(draft),
            constraints={'target_workout_minutes':50,'max_workout_minutes':50})
        assert changed['workout_minutes_after']==50 and len(changed['target_plan']['actions'])==4
        assert changed['constraints']['target_workout_minutes']==50
        assert revision.context(db,u,DAY)==original


@pytest.mark.parametrize('constraints,allowed',[
    ({'max_workout_minutes':50},True),
    ({'min_workout_minutes':30,'max_workout_minutes':50},False),
    ({'target_workout_minutes':30,'workout_time_tolerance_min':2},True),
    ({'target_workout_minutes':30},False)])
def test_duration_target_minimum_maximum_and_approximation_are_distinct(state,constraints,allowed):
    draft=training_preview(state)
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID)
        if allowed:
            value=revision.update_draft(db,u,draft['revision_id'],expected=draft['etag'],proposal={},constraints=constraints)
            assert value['workout_minutes_after']==29
        else:
            with pytest.raises(revision.RevisionError):revision.update_draft(db,u,draft['revision_id'],expected=draft['etag'],proposal={},constraints=constraints)


def test_extend_same_draft_dinner_preserves_fifty_training_and_invalidates_old_etag(state):
    draft=training_preview(state);before=state.snapshot()
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID)
        timed=revision.update_draft(db,u,draft['revision_id'],expected=draft['etag'],proposal=fifty_minutes(draft),constraints={'target_workout_minutes':50,'max_workout_minutes':50})
        changed=revision.extend_draft(db,u,draft['revision_id'],expected=timed['etag'],additional_scope=['dinner'],proposal=beef(),
            constraints={'meal_ingredient_rules':[{'slot':'dinner','ingredient_options':['牛肉'],'grams':150}]})
        assert changed['revision_id']==draft['revision_id'] and changed['scope']==['workout','dinner'] and changed['version']==3
        assert changed['target_plan']['actions']==timed['target_plan']['actions'] and changed['workout_minutes_after']==50
        assert changed['constraints']['target_workout_minutes']==50
        with pytest.raises(revision.RevisionError):revision.apply(db,u,draft['revision_id'],confirmed_etag=timed['etag'])
        assert state.snapshot()['days']==before['days']
        db.commit()
        result=revision.apply(db,u,changed['revision_id'],confirmed_etag=changed['etag']);db.commit()
        assert result['status']=='applied'
    after=state.snapshot()
    for table in ['profiles','meal_logs','workout_logs','plans','plan_versions']:
        assert after['tables'][table]==before['tables'][table]
    assert after['days'][str(DAY)]['meals']['lunch']==before['days'][str(DAY)]['meals']['lunch']


def test_ordinary_edit_cannot_expand_and_extension_requires_its_meal_content(state):
    draft=training_preview(state)
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID)
        with pytest.raises(revision.RevisionError) as error:revision.update_draft(db,u,draft['revision_id'],expected=draft['etag'],proposal=beef())
        assert error.value.code=='scope'
        with pytest.raises(revision.RevisionError):revision.extend_draft(db,u,draft['revision_id'],expected=draft['etag'],additional_scope=['dinner'],proposal={})
        with pytest.raises(revision.RevisionError):revision.extend_draft(db,u,draft['revision_id'],expected=draft['etag'],additional_scope=['workout'],proposal=beef())
        assert db.get(DayPlanRevision,draft['revision_id']).scope==['workout']


def test_eaten_meal_and_cross_user_draft_still_protected(state):
    draft=training_preview(state)
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID)
        other=User(id='different-meal-scope-user',nickname='合成用户');db.add(other);db.flush()
        with pytest.raises(revision.RevisionError) as error:revision.extend_draft(db,other,draft['revision_id'],expected=draft['etag'],additional_scope=['dinner'],proposal=beef())
        assert error.value.code=='ownership'
        db.add(MealLog(user_id=u.id,date=DAY,slot='dinner',title='已吃',kcal=400));db.commit()
        with pytest.raises(revision.RevisionError) as error:revision.extend_draft(db,u,draft['revision_id'],expected=draft['etag'],additional_scope=['dinner'],proposal=beef())
        assert error.value.code=='conflict'


def test_persisted_food_requirement_and_quantity_cannot_be_silently_dropped(state):
    draft=training_preview(state)
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID)
        changed=revision.extend_draft(db,u,draft['revision_id'],expected=draft['etag'],additional_scope=['dinner'],proposal=beef(),
            constraints={'meal_ingredient_rules':[{'slot':'dinner','ingredient_options':['牛肉'],'grams':150}]})
        with pytest.raises(revision.RevisionError) as error:revision.update_draft(db,u,changed['revision_id'],expected=changed['etag'],proposal=beef(100))
        assert error.value.code=='meal_quantity'
        wrong=beef();wrong['meals'][0]['items'][0]['ingredient_key']='豆腐'
        with pytest.raises(revision.RevisionError) as error:revision.update_draft(db,u,changed['revision_id'],expected=changed['etag'],proposal=wrong)
        assert error.value.code=='meal_goal'


def test_independent_target_semantics_repairs_upper_bound_only_tool_call(state):
    env=state;draft=training_preview(env);pending_preview(env,draft);before=env.snapshot()
    requirements={'workout_change':True,'action_selection_change':False,'action_count_change':False,
        'duration':{'kind':'target','minutes':50,'tolerance_minutes':0},'meal_slots':[],'meal_rules':[]}
    model=GoalModel([edit_control('把训练上限提升至50'),load,lambda d:edit_time(d,correct=False),lambda d:edit_time(d,correct=True)],requirements)
    env.client.client=model
    result=post(env,'两个改动，训练时长给我提升到50')
    assert result['overall_status']=='confirmation_required',result
    assert result['day_revision']['workout_minutes_after']==50
    assert result['day_revision']['constraints']['target_workout_minutes']==50
    assert result['day_revision']['revision_id']==draft['revision_id']
    assert env.snapshot()['days']==before['days']
    goal_call=next(c for c in model.calls if 'revision_goal_input' in c)
    assert goal_call['revision_goal_input']=='两个改动，训练时长给我提升到50'
    assert any(t['stage']=='revision_goal_repair' for t in result['trace'])


def timed_preview(env):
    draft=training_preview(env)
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);draft=revision.update_draft(db,u,draft['revision_id'],expected=draft['etag'],proposal=fifty_minutes(draft),constraints={'target_workout_minutes':50,'max_workout_minutes':50});db.commit()
    return draft


def extend_beef(data,*,beef_meal=True):
    draft=find_draft(data);meal=beef()
    if not beef_meal:meal['meals'][0]['items'][0]['ingredient_key']='鸡胸肉'
    return {'snapshot_id':data['snapshot']['snapshot_id'],'tasks':[task(draft['id'],'day_plan.extend_revision',expected_revision=draft['etag'],
        additional_scope=['dinner'],proposal=meal,constraints={'meal_ingredient_rules':dinner_requirement(exclude=not beef_meal)['meal_rules']}).model_dump()]}


def test_dinner_followup_wrong_create_is_repaired_to_same_draft_scope_extension(state):
    env=state;draft=timed_preview(env);pending_preview(env,draft);before=env.snapshot()
    def wrong_create(d):
        root=next(o for o in d['snapshot']['objects'] if o['type']=='day');current=find_draft(d)
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(root['id'],'day_plan.create_revision',scope=['workout','dinner'],
            expected_base_revision=root['revision_base_revision'],workout_policy='replace_remaining',
            replan_authorization_id=current['target_plan']['replan_authorization_id'],proposal={**fifty_minutes({'target_plan':root['revision_context']['day']}),**beef()},
            constraints={'target_workout_minutes':50,'max_workout_minutes':50}).model_dump()]}
    env.client.client=GoalModel([edit_control('继续编辑整日草案：晚餐吃牛肉'),load,wrong_create,extend_beef],dinner_requirement())
    result=post(env,'然后餐食晚上我想吃牛肉')
    assert result['overall_status']=='confirmation_required',result
    edited=result['day_revision'];assert edited['revision_id']==draft['revision_id'] and edited['scope']==['workout','dinner']
    assert edited['target_plan']['actions']==draft['target_plan']['actions'] and edited['workout_minutes_after']==50
    assert '牛肉' in result['reply'] and env.snapshot()['days']==before['days']
    applied=confirm(lambda text,**kw:post(env,text,**kw),result);assert applied['overall_status']=='success'
    assert applied['day_revision']['workout_minutes_after']==50
    assert env.snapshot()['days'][str(DAY)]['meals']['lunch']==before['days'][str(DAY)]['meals']['lunch']


def test_negated_food_requirement_repairs_positive_food_plan_without_touching_training(state):
    env=state;draft=timed_preview(env);pending_preview(env,draft);before=env.snapshot()
    env.client.client=GoalModel([edit_control('晚餐不要牛肉，换鸡肉，训练保持'),load,extend_beef,lambda d:extend_beef(d,beef_meal=False)],dinner_requirement(exclude=True))
    result=post(env,'晚餐别放牛肉，鸡肉就行，训练别动')
    assert result['overall_status']=='confirmation_required',result
    assert result['day_revision']['workout_minutes_after']==50
    assert result['day_revision']['target_plan']['actions']==draft['target_plan']['actions']
    dinner=next(m for m in result['day_revision']['target_plan']['meals'] if m['slot']=='dinner')
    assert '牛肉' not in [i['name'] for i in dinner['items']]
    assert env.snapshot()['days']==before['days']


def test_bad_repairs_cannot_drop_prior_time_or_change_actions_and_keep_pending_for_retry(state):
    from app.models import CoachPendingPlan
    env=state;draft=timed_preview(env);p=pending_preview(env,draft);before=env.snapshot()
    def bad(d):
        plan=extend_beef(d);args=plan['tasks'][0]['arguments']
        args['constraints'].update(target_workout_minutes=None,max_workout_minutes=50)
        args['proposal']['actions']=[{'id':a['id'],'duration_min':5 if a['slot']=='warmup' else 8} for a in draft['target_plan']['actions']]
        return plan
    env.client.client=GoalModel([edit_control('加晚餐牛肉'),load,bad,bad,bad,bad],dinner_requirement())
    result=post(env,'晚上安排牛肉，其他别改')
    assert result['overall_status']=='clarification_required'
    with env.factory() as db:
        assert db.get(CoachPendingPlan,p['pending_confirmation']['pending_id']).status=='pending'
        assert db.get(DayPlanRevision,draft['revision_id']).version==draft['version']
    assert env.snapshot()['days']==before['days']


def test_wrong_snapshot_reference_is_repaired_without_changing_context_request(state):
    env=state;draft=training_preview(env);pending_preview(env,draft)
    def wrong(d):return {**load(d),'snapshot_id':'wrong-snapshot-reference'}
    def fix(d):return {**d['previous_plan'],'snapshot_id':d['snapshot']['snapshot_id']}
    req={'workout_change':True,'action_selection_change':False,'action_count_change':False,'duration':{'kind':'target','minutes':50},'meal_slots':[]}
    env.client.client=GoalModel([edit_control('训练提升到50'),wrong,fix,lambda d:edit_time(d,correct=True)],req)
    value=post(env,'训练时间调整为50分钟')
    assert value['overall_status']=='confirmation_required' and value['day_revision']['workout_minutes_after']==50
    assert any(t['stage']=='snapshot_reference_repair' for t in value['trace'])


def test_active_preview_cannot_be_bypassed_by_local_meal_tool_even_if_first_controller_loses_edit_id(state):
    env=state;draft=timed_preview(env);pending_preview(env,draft);before=env.snapshot()
    def independent(d):return {'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],'execute_revision':False,'new_request_text':'晚餐吃牛肉'}
    def local_write(d):
        meal=next(o for o in d['snapshot']['objects'] if o['type']=='meal' and o['slot']=='dinner')
        food=next(o for o in d['snapshot']['objects'] if o['type']=='ingredient' and o['name']=='牛肉')
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(meal['id'],'meal.add_food',ingredient_id=food['id'],grams=150).model_dump()]}
    env.client.client=GoalModel([independent,load,local_write,extend_beef],dinner_requirement())
    value=post(env,'然后晚上想吃牛肉，先看看')
    assert value['overall_status']=='confirmation_required',value
    assert value['day_revision']['revision_id']==draft['revision_id'] and value['day_revision']['workout_minutes_after']==50
    assert env.snapshot()['days']==before['days']
    assert not any(r.get('capability_id')=='meal.add_food' and r.get('status')=='success' for r in value['task_results'])


def test_failed_unmaterialized_goal_survives_next_meal_followup(state):
    from app.models import CoachPendingPlan
    env=state;draft=training_preview(env);pending=pending_preview(env,draft)
    req={'workout_change':True,'action_selection_change':False,'action_count_change':False,'duration':{'kind':'target','minutes':50},'meal_slots':[]}
    bad=lambda d:edit_time(d,correct=False)
    env.client.client=GoalModel([edit_control('训练提升到50'),load,bad,bad,bad,bad],req)
    failed=post(env,'训练时长提升到50分钟')
    assert failed['overall_status']=='clarification_required'
    with env.factory() as db:
        saved=db.get(CoachPendingPlan,pending['pending_confirmation']['pending_id'])
        assert saved.result['unresolved_edits'][-1]['user_text']=='训练时长提升到50分钟'
    def combined(d):
        value=extend_beef(d);value['tasks'][0]['arguments']['proposal'].update(fifty_minutes(find_draft(d)))
        value['tasks'][0]['arguments']['constraints'].update(target_workout_minutes=50,max_workout_minutes=50)
        return value
    # Even if this turn's extractor reports only the new meal, omitted prior
    # typed requirements must survive independently of another interpretation.
    req=dinner_requirement()
    model=GoalModel([edit_control('再加晚餐牛肉'),load,combined],req);env.client.client=model
    value=post(env,'然后晚餐吃牛肉，刚才的训练要求继续')
    assert value['overall_status']=='confirmation_required' and value['day_revision']['workout_minutes_after']==50
    call=next(c for c in model.calls if 'revision_goal_input' in c)
    assert any(m.get('active_saved_workflow',{}).get('unresolved_edits') for m in call['editing_context'])
    assert any(t.get('stage')=='revision_requirements_merged' and t['parsed']['duration']['minutes']==50 for t in value['trace'])


def test_combined_minimum_and_maximum_are_both_checked_against_tool_arguments(state):
    env=state;draft=training_preview(env);pending_preview(env,draft)
    req={'workout_change':True,'action_selection_change':False,'action_count_change':False,
        'duration':{'kind':'minimum','minutes':35,'minimum_minutes':35,'maximum_minutes':40},'meal_slots':[]}
    def plan(d,maximum):
        row=find_draft(d)
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(row['id'],'day_plan.update_revision',expected_revision=row['etag'],
            proposal={'actions':[{'id':a['id'],'duration_min':5 if a['slot']=='warmup' else 10} for a in row['target_plan']['actions']]},
            constraints={'min_workout_minutes':35,'max_workout_minutes':maximum}).model_dump()]}
    env.client.client=GoalModel([edit_control('训练35到40分钟'),lambda d:plan(d,50),lambda d:plan(d,40)],req)
    value=post(env,'至少35分钟，但不能超过40分钟，动作保持')
    assert value['overall_status']=='confirmation_required',value
    assert value['day_revision']['workout_minutes_after']==35
    assert value['day_revision']['constraints']['max_workout_minutes']==40
