"""Continuous real SQLite: independent schema and equipment errors, no text routing."""
from sqlalchemy import select
from app.domain.nutrition_catalog import FOOD_MACROS_PER_100G,PLANNING_FOODS,PLANNING_INGREDIENT_KEYS
from app.models import DayPlanRevision
from tests.test_workout_progress import progress_chat
from tests.test_day_revision import state
from tests.test_workout_replan import legacy_finished,request_plan,replacement_plan
from tests.test_day_revision_chat import load
from tests.test_workout_records import confirm


def request_both(data):
    value=request_plan(data);value['tasks'][0]['arguments']['scope']=['workout','breakfast'];return value


def proposal(data, *, unknown_food=False, unavailable=False):
    value=replacement_plan(data);args=value['tasks'][0]['arguments'];args['scope']=['workout','breakfast']
    args['proposal']['meals']=[{'slot':'breakfast','items':[{'ingredient_key':'目录外合成食材' if unknown_food else '燕麦','grams':60}]}]
    if unavailable:args['proposal']['actions'][1]={'action_ref':'lat-pulldown','slot':'strength','sets':2,'reps':10,'duration_min':5}
    return value


def test_long_plan_foods_have_supported_revision_identity_and_oats_are_not_unknown():
    from app.agent.planning_agent import FOODS
    assert FOODS is PLANNING_FOODS
    for food in FOODS:assert PLANNING_INGREDIENT_KEYS.get(food['name'],food['name']) in FOOD_MACROS_PER_100G
    assert FOOD_MACROS_PER_100G['燕麦']==(389,16.9,6.9,66)
    assert FOOD_MACROS_PER_100G['米饭']==(116,2.6,.3,25.9)


def test_continuous_food_then_equipment_repair_previews_before_applying(progress_chat):
    env,model,sid,post=progress_chat;legacy_finished(env,sid);before=env.snapshot()
    model.responses.extend([load,request_both,lambda d:proposal(d,unknown_food=True,unavailable=True),lambda d:proposal(d,unavailable=True),proposal])
    first=post('重新安排训练和早餐，先看方案，已经练的保留');drafted=confirm(post,first)
    assert drafted['overall_status']=='confirmation_required',drafted
    failures=[t for t in drafted['trace'] if t['stage']=='binding_validation'];assert failures[0]['errors'][0]['type']=='literal_error'
    equipment=failures[1]['errors'][0];assert equipment['type']=='equipment' and equipment['details']['required_equipment']=='cable'
    assert 'cable' not in equipment['details']['allowed_equipment']
    assert len([t for t in drafted['trace'] if t['stage']=='arguments_schema_repair'])==2
    assert env.snapshot()['days']==before['days']
    result=confirm(post,drafted);assert result['overall_status']=='success',result
    after=env.snapshot();breakfast=after['days'][drafted['day_revision']['date']]['meals']['breakfast']
    assert breakfast['items'][0]['name']=='燕麦' and breakfast['items'][0]['grams']==60 and breakfast['items'][0]['kcal']==233.4
    for table in ['workout_logs','meal_logs','profiles','plans','plan_versions']:assert after['tables'][table]==before['tables'][table]


def test_bounded_failure_returns_latest_constraint_and_preserves_consent_for_retry(progress_chat):
    env,model,sid,post=progress_chat;legacy_finished(env,sid);before=env.snapshot()['days']
    model.responses.extend([load,request_both,lambda d:proposal(d,unknown_food=True,unavailable=True),*[lambda d:proposal(d,unavailable=True) for _ in range(3)]])
    first=post('训练和早餐重排，先预览');failed=confirm(post,first)
    assert failed['overall_status']=='clarification_required'
    assert '所需器械不可用' in failed['reply'] and 'Schema' not in failed['reply']
    assert failed['workout_replan']['status']=='approved'
    assert any(t['stage']=='repair_limit' and t['attempts']==3 for t in failed['trace'])
    assert env.snapshot()['days']==before
    with env.factory() as db:assert list(db.scalars(select(DayPlanRevision)))==[]
    model.responses.extend([load,proposal]);retried=post('继续按可用器械生成刚才的目标，早餐要求保持')
    assert retried['overall_status']=='confirmation_required',retried
    assert retried['day_revision']['status']=='awaiting_confirmation' and env.snapshot()['days']==before


def test_argument_repairs_cannot_expand_authorized_scope(progress_chat):
    env,model,sid,post=progress_chat;legacy_finished(env,sid);before=env.snapshot()['days']
    def expanded(data):
        value=proposal(data);value['tasks'][0]['arguments']['scope'].append('dinner');return value
    model.responses.extend([load,request_both,lambda d:proposal(d,unknown_food=True),expanded])
    first=post('只重排训练和早餐');failed=confirm(post,first)
    assert failed['overall_status']=='clarification_required'
    assert '授权范围' in failed['reply'] and env.snapshot()['days']==before
    with env.factory() as db:assert list(db.scalars(select(DayPlanRevision)))==[]


def test_empty_current_day_can_generate_new_workout_and_breakfast_without_zero_remaining_claim(state):
    from tests.test_day_revision_chat import post
    from app.domain.day_plan import effective_day,save_override
    from app.models import User
    from app.seed import DEMO_USER_ID
    from app.evals.dialogue_runner import DAY
    env=state;model=env.client.client
    with env.factory() as db:
        u=db.get(User,DEMO_USER_ID);day=effective_day(db,u,DAY)
        save_override(db,u,DAY,actions=[],meals=day['meals'],constraints={},summary='合成休息日',reason='synthetic rest day',base_version=day['plan_version']);db.commit()
    def fresh(data):
        value=proposal(data);value['tasks'][0]['arguments']['proposal']['actions']=[
            {'action_ref':'worlds-greatest-stretch','slot':'warmup','sets':1,'duration_min':5},
            {'action_ref':'goblet-squat','slot':'strength','sets':2,'reps':10,'duration_min':5}]
        return value
    model.responses.extend([load,request_both,fresh])
    first=post(env,'今天重新制定训练和早餐目标，先预览')
    assert first['workout_replan']['total_sets']==0
    assert '今天暂无有效训练安排' in first['reply'] and '0 组未达到' not in first['reply']
    drafted=confirm(lambda text,**kw:post(env,text,**kw),first)
    assert drafted['overall_status']=='confirmation_required',drafted
    assert drafted['day_revision']['workout_minutes_after']==10
    assert next(m for m in drafted['day_revision']['target_plan']['meals'] if m['slot']=='breakfast')['title']=='早餐'


def test_shared_food_tags_remain_enforced_for_newly_supported_ingredients(state):
    import pytest
    from app.models import Profile,User
    from app.seed import DEMO_USER_ID
    from app.evals.dialogue_runner import DAY
    from app.domain import day_revision as service
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);db.get(Profile,u.id).allergies=['gluten'];db.flush()
        ctx=service.context(db,u,DAY)
        with pytest.raises(service.RevisionError) as error:
            service.create(db,u,DAY,scope=['breakfast'],expected_base_revision=service.digest(ctx),
                proposal={'meals':[{'slot':'breakfast','items':[{'ingredient_key':'燕麦','grams':60}]}]})
        assert error.value.code=='ingredient_constraint'


def test_exhausted_food_repairs_explain_actual_catalog_limit_without_schema_jargon(progress_chat):
    env,model,sid,post=progress_chat;before=env.snapshot()['days']
    model.responses.extend([load,request_both,*[lambda d:proposal(d,unknown_food=True) for _ in range(4)]])
    first=post('只重排训练和早餐，先给预览');failed=confirm(post,first)
    assert failed['overall_status']=='clarification_required' and failed['workout_replan']['status']=='approved'
    assert '食材目录' in failed['reply'] and 'Schema' not in failed['reply']
    assert env.snapshot()['days']==before
