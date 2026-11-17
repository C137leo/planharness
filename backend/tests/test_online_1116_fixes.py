"""Online regressions: real SQLite and domain writes, scripted model (not NLU proof)."""
import pytest
from sqlalchemy import select
from tests.test_day_revision import state
from tests.test_day_revision_chat import post, load, create
from app.models import CoachPendingPlan, DayPlanRevision


def test_failed_edit_cannot_confirm_old_revision(state):
    env=state;env.client.client.responses.extend([load,create])
    first=post(env,'生成整日草案')
    before=env.snapshot()
    with env.factory() as db:
        p=db.get(CoachPendingPlan,first['pending_confirmation']['pending_id'])
        # Same persisted failure metadata as semantic_runtime.finish after repair exhaustion.
        p.result={**p.result,'unresolved_edits':[{'user_text':'只留两个正式动作','status':'not_materialized'}]}
        db.commit()
    p=first['pending_confirmation']
    result=post(env,'确认两个动作',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert result['overall_status']=='confirmation_required'
    assert not any(a['type']=='confirm_tasks' for a in result['actions'])
    assert env.snapshot()['days']==before['days']
    with env.factory() as db:
        assert db.get(DayPlanRevision,first['day_revision']['revision_id']).status=='awaiting_confirmation'


def test_unmarked_preview_handoff_cannot_write_actual_meal(state):
    env=state;model=env.client.client;model.responses.extend([load,create])
    first=post(env,'训练和晚餐先预览');before=env.snapshot()['days']
    def wrong_local(d):
        from tests.test_semantic_binding import task
        food=next(o for o in d['snapshot']['objects'] if o['type']=='food_item')
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(food['id'],'meal.set_quantity',grams=220).model_dump()]}
    model.responses.extend([lambda d:{'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],'execute_revision':False,'new_request_text':'晚餐改一下先不应用','edit_task_ids':[]},wrong_local,wrong_local])
    result=post(env,'同一份预览晚餐改一下，先不应用')
    assert result['overall_status']=='clarification_required'
    assert result.get('error') in {'draft_edit_boundary','repair_contract'}, (result['reply'],[(t['stage'],t.get('message'),t.get('error')) for t in result['trace']])
    assert env.snapshot()['days']==before


def test_initial_nutrition_aggregates_canonical_fields():
    from types import SimpleNamespace
    from app.agent.initial_requirements import InitialRequirements,prepare
    meal={'slot':'dinner','protein_g':29.1,'items':[{'name':'菠菜','grams':150,'kcal':34,'protein':4.3,'fat':0,'carb':1},{'name':'红薯','grams':300,'kcal':258,'protein':4.8,'fat':0,'carb':2}]}
    payload={'status':'ready','meals':[meal]}
    contract=InitialRequirements(meal_rules=[{'slot':'dinner','requirement':'include','ingredient_options':['牛肉'],'grams':150}])
    prepare(payload,contract,SimpleNamespace(allergies=[],diet_preferences=[]))
    assert meal['protein_g']==pytest.approx(sum(i['protein'] for i in meal['items']))
    assert meal['fat_g']==pytest.approx(sum(i['fat'] for i in meal['items']))


def test_flatten_preserves_preview_order_instead_of_stale_seq():
    from app.domain.coach import _flatten_actions
    actions=[{'action_ref':r,'slot':'strength','seq':s} for r,s in [('a',3),('b',1),('c',2)]]
    rows=_flatten_actions([{'day_index':1,'actions':actions}])
    assert [a['action_ref'] for a in sorted(rows,key=lambda a:a['seq'])]==['a','b','c']


def test_training_query_returns_actual_set_facts_without_filling_unknown():
    from app.agent.workout_runtime import record_details
    data={'session':{'items':[{'name':'深蹲','completed_sets':2,'set_logs':[{'set_no':1,'reps':10,'weight_kg':None},{'set_no':2,'reps':8,'weight_kg':10}]}],'actual_minutes':20},'actual_minutes_known':True}
    text=record_details(data)
    assert '第2组：8次，10公斤' in text and '重量未记录' in text and '20分钟' in text


def test_named_catalog_choices_do_not_require_internal_ids():
    from app.agent.initial_requirements import InitialRequirements
    assert InitialRequirements(selected_action_refs=['高脚杯深蹲','臀桥','单臂哑铃划船']).selected_action_refs


def test_unknown_duration_is_distinct_from_known_zero():
    from types import SimpleNamespace
    import datetime
    from app.domain.calories import burn_for_log
    log=SimpleNamespace(id='qa',date=datetime.date(2026,11,16),action_ref='goblet-squat',kind='strength',duration_min=0,details={'actual_duration_known':False})
    assert burn_for_log(log,78)['actual_duration_known'] is False
    log.details={'actual_duration_known':True}
    assert burn_for_log(log,78)['actual_duration_known'] is True


def test_two_action_goal_commits_exact_updated_preview(state):
    from tests.test_revision_goal_contract import training_preview,pending_preview,edit_control,GoalModel,find_draft
    from tests.test_semantic_binding import task
    draft=training_preview(state);pending_preview(state,draft)
    before=state.snapshot()
    def two(d):
        r=find_draft(d)
        chosen=[{'id':a['id']} for a in r['target_plan']['actions'][:3]]
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(r['id'],'day_plan.update_revision',expected_revision=r['etag'],proposal={'actions':chosen},constraints={'max_workout_actions':2}).model_dump()]}
    state.client.client=GoalModel([edit_control('只留两个正式动作，热身保留'),two],{'formal_actions':2,'workout_change':True,'action_selection_change':True,'action_count_change':True,'meal_slots':[]})
    updated=post(state,'只留两个正式动作，热身保留')
    assert updated['overall_status']=='confirmation_required',updated
    assert len(updated['day_revision']['target_plan']['actions'])==3
    assert state.snapshot()['days']==before['days']
    p=updated['pending_confirmation']
    result=post(state,'就这两个，确认',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert result['overall_status']=='success',result
    assert sum(a['slot']!='warmup' for a in result['day']['actions'])==2
    for key in ['meal_logs','workout_logs','profiles']:
        assert state.snapshot()['tables'][key]==before['tables'][key]


def test_meal_proposal_is_persisted_then_one_confirmation_records_once(state):
    from tests.test_semantic_binding import task
    from app.models import MealLog
    def preview(d):
        day=next(o for o in d['snapshot']['objects'] if o['type']=='day')
        return {'mode':'propose','snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(day['id'],'nutrition.record_meal',slot='lunch',title='鸡肉饭',kcal=650,protein_g=40).model_dump()]}
    state.client.client.responses.append(preview)
    first=post(state,'实际午餐650千卡40克蛋白，先核对别保存')
    assert first['overall_status']=='confirmation_required',first
    with state.factory() as db:assert not list(db.scalars(select(MealLog)))
    p=first['pending_confirmation'];params={'action':'confirm','pending_id':p['pending_id'],'version':p['version']}
    done=post(state,'确认保存',confirmation=params)
    assert done['overall_status']=='success'
    post(state,'确认保存',confirmation=params)
    with state.factory() as db:
        rows=list(db.scalars(select(MealLog)));assert len(rows)==1 and rows[0].kcal==650


def test_user_reply_formats_internal_references_without_changing_task_data():
    from types import SimpleNamespace
    from app.agent.reply_evidence import public_reply
    ident='00000000-0000-0000-0000-000000000001'
    snap=SimpleNamespace(payload={'objects':[{'id':ident,'name':'高脚杯深蹲'}]})
    assert public_reply(f'{ident} display_position 2，selected_action_refs，10公斤',snap)=='高脚杯深蹲 动作顺序 2，已选动作，10公斤'


from tests.test_workout_progress import progress_chat


def test_consent_stage_confirmation_is_not_mistaken_for_actual_plan_application(progress_chat):
    import json
    from tests.test_workout_replan import legacy_finished,request_plan,replacement_plan
    from tests.test_semantic_binding import Scripted
    env,model,sid,send=progress_chat;legacy_finished(env,sid)
    model.responses.extend([load,request_plan]);first=send('保留已有记录，重新预览20分钟')
    before=env.snapshot()['days']
    class ReviewedConsent(Scripted):
        def complete(self,messages,**kwargs):
            data=json.loads(messages[-1]['content'])
            if data.get('saved_effects'):
                assert all(e['consent_only'] for e in data['saved_effects'])
                return {'message':{'content':json.dumps({'approved_task_ids':[t['task_id'] for t in data['saved_tasks']], 'new_request_text':'','modified_task_ids':[]})}}
            return super().complete(messages,**kwargs)
    env.client.client=ReviewedConsent([lambda d:{'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'], 'execute_revision':False,'new_request_text':'确认保留记录，只生成预览不应用'},replacement_plan])
    result=send('确认保留旧记录、放弃未完成部分，先生成预览，不应用')
    assert result['overall_status']=='confirmation_required',result
    assert result['workout_replan']['status']=='approved'
    assert result['day_revision']['status']=='awaiting_confirmation'
    assert env.snapshot()['days']==before


def test_quote_evidence_clears_only_pricing_not_allergen_or_order_goals():
    import json
    from app.agent.reply_evidence import remaining_commerce_goals
    class Review:
        def available(self):return True
        def complete(self,*a,**kw):return {'message':{'content':json.dumps({'kinds':['quote','allergens','other']})}}
    goals=['鸡翅报价','核实海鲜过敏安全','创建真实订单']
    result={'checkout':{'status':'awaiting_confirmation','expires_at':'2099-01-01T00:00:00+00:00','cart':[{'name':'鸡翅'}],'quote':{'total_cents':1500},'take_way_code':'out'}}
    assert remaining_commerce_goals(Review(),goals,result,[])==goals[1:]
    result['checkout']['status']='cancelled'
    assert remaining_commerce_goals(Review(),goals,result,[])==goals


def test_controller_failure_also_invalidates_old_draft_confirmation(state):
    env=state;env.client.client.responses.extend([load,create])
    first=post(env,'先生成草案');before=env.snapshot()['days']
    failed=post(env,'只留两个动作，20分钟以内')  # Scripted controller has no response: semantic failure.
    assert failed['overall_status']=='clarification_required'
    p=first['pending_confirmation']
    result=post(env,'确认旧按钮',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert result['overall_status']=='confirmation_required'
    assert result['pending_confirmation']['version']>p['version']
    assert env.snapshot()['days']==before


def test_committed_food_reply_uses_saved_quantity_not_historical_preview_goal(state):
    from tests.test_semantic_binding import task
    def preview(d):
        food=next(o for o in d['snapshot']['objects'] if o['type']=='food_item')
        change=task(food['id'],'meal.set_quantity',grams=220)
        change.user_goal='仅生成预览暂不保存'
        return {'mode':'propose','snapshot_id':d['snapshot']['snapshot_id'],'tasks':[change.model_dump()]}
    state.client.client.responses.append(preview)
    first=post(state,'这一项改为220克，先预览不要保存')
    before=state.snapshot()['days']
    p=first['pending_confirmation']
    result=post(state,'确认保存220克',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert result['overall_status']=='success',result
    assert '已保存' in result['reply'] and '220克' in result['reply']
    assert '暂不保存' not in result['reply'] and '仅生成预览' not in result['reply']
    assert state.snapshot()['days']!=before
