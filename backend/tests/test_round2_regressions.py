"""Screenshot regressions: isolated databases and explicit model/MCP substitutes."""
import copy
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from tests.test_day_revision import state, create, dinner
from tests.test_day_revision_chat import post
from tests.test_semantic_binding import task
from app.models import User, Profile, Plan, PlanVersion, MealPlan, PlanDraft, MealLog
from app.seed import DEMO_USER_ID
from app.evals.dialogue_runner import DAY


@pytest.mark.parametrize('food',['虾','三文鱼'])
def test_day_revision_checks_semantic_allergen_tags(state,food):
    from app.domain.day_revision import RevisionError
    with state.factory() as db:
        db.get(Profile,DEMO_USER_ID).allergies=['乳制品','海鲜过敏'];db.commit()
        with pytest.raises(RevisionError,match='排除'):
            create(db,db.get(User,DEMO_USER_ID),DAY,scope=['dinner'],proposal=dinner(food=food))


def test_new_snack_is_not_added_to_hit_training_or_calorie_targets(state):
    from app.domain.day_revision import RevisionError
    with state.factory() as db:
        with pytest.raises(RevisionError,match='加餐'):
            create(db,db.get(User,DEMO_USER_ID),DAY,scope=['snack'],proposal={'meals':[{'slot':'snack','items':[{'ingredient_key':'虾','grams':100}]}]})


def test_explicit_allergy_additions_persist_union_and_repair_long_term(state):
    def add(d):
        p=next(o for o in d['snapshot']['objects'] if o['type']=='profile')
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(p['id'],'profile.add_allergies',allergies=['海鲜','花生']).model_dump()]}
    with state.factory() as db:
        db.get(Profile,DEMO_USER_ID).allergies=['乳制品']
        plan=db.scalar(select(Plan).where(Plan.user_id==DEMO_USER_ID));old_version=plan.current_version
        version=db.scalar(select(PlanVersion).where(PlanVersion.plan_id==plan.id,PlanVersion.version==old_version))
        db.add(MealPlan(user_id=DEMO_USER_ID,plan_version_id=version.id,day_index=DAY.isoweekday(),slot='dinner',title='晚餐',items=[{'name':'虾','grams':100,'kcal':99,'protein':20,'fat':1,'carb':0}],kcal=99,protein_g=20))
        db.commit()
    before=state.snapshot()
    state.client.client.responses.append(add)
    result=post(state,'我新增海鲜和花生过敏，保留原乳制品限制，长期排除')
    assert result['overall_status']=='success',result
    with state.factory() as db:
        assert db.get(Profile,DEMO_USER_ID).allergies==['乳制品','海鲜','花生']
        plan=db.scalar(select(Plan).where(Plan.user_id==DEMO_USER_ID));assert plan.current_version==old_version+1
        version=db.scalar(select(PlanVersion).where(PlanVersion.plan_id==plan.id,PlanVersion.version==plan.current_version))
        rows=db.scalars(select(MealPlan).where(MealPlan.plan_version_id==version.id)).all()
        assert rows and all(i['name']!='虾' for m in rows for i in m.items)
    after=state.snapshot()
    assert before['tables']['meal_logs']==after['tables']['meal_logs']
    assert before['tables']['workout_logs']==after['tables']['workout_logs']
    assert before['days'][str(DAY)]['actions']==after['days'][str(DAY)]['actions']


def test_local_food_tool_cannot_add_shrimp_to_seafood_allergic_user(state):
    with state.factory() as db:
        db.get(Profile,DEMO_USER_ID).allergies=['海鲜'];db.commit()
    def add(d):
        food=next(o for o in d['snapshot']['objects'] if o['type']=='ingredient' and o['name']=='虾')
        meal=next(o for o in d['snapshot']['objects'] if o['type']=='meal' and o['slot']=='dinner')
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(meal['id'],'meal.add_food',ingredient_id=food['id'],grams=100).model_dump()]}
    before=state.snapshot()['days'];state.client.client.responses.append(add)
    result=post(state,'晚餐加100克虾')
    assert result['overall_status']=='failed',result
    assert state.snapshot()['days']==before


def test_replacement_plan_has_real_lifecycle_and_preserves_facts(state):
    def propose(d):
        p=next(o for o in d['snapshot']['objects'] if o['type']=='profile')
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(p['id'],'plan.create_replacement_draft',instruction='按档案重建长期计划，先预览').model_dump()]}
    before=state.snapshot();state.client.client.responses.append(propose)
    preview=post(state,'重新生成长期计划，只预览')
    assert preview['overall_status']=='confirmation_required',preview
    assert state.snapshot()['days']==before['days']
    p=preview['pending_confirmation'];done=post(state,'确认启用新版',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert done['overall_status']=='success',done
    for name in ('workout_logs','meal_logs','profiles'):
        assert state.snapshot()['tables'][name]==before['tables'][name]
    assert state.snapshot()['tables']['plans'][0]['current_version']>before['tables']['plans'][0]['current_version']


def test_generated_default_program_spaces_days_and_avoids_unrequested_snacks():
    from app.agent.planning_agent import _build_days,_build_meals
    from app.domain.constraint_guard import enforce_plan_constraints
    program={'sessions':[{'focus':'full_body_a','exercises':[{'slug':'wall-sit','movement_mode':'knee_dominant','equipment':'Bodyweight'},{'slug':'push-up','movement_mode':'horizontal_push','equipment':'Bodyweight'},{'slug':'glute-bridge','movement_mode':'hip_dominant','equipment':'Bodyweight'}]}]*3}
    days=_build_days(program,start_weekday=1)
    assert [d['day_index'] for d in days]==[1,3,5]
    profile=SimpleNamespace(equipment=['mat'],allergies=['海鲜','乳制品'],diet_preferences=[],weekly_days=3,session_minutes=45)
    payload={'status':'ready','days':days,'meals':[],'program':{'session_minutes':45}}
    enforce_plan_constraints(payload,profile)
    assert len({tuple(a['action_ref'] for a in d['actions']) for d in payload['days']})>1
    meals=_build_meals({'goal':'增肌','allergies':['海鲜'],'diet_preferences':[]},{'target_kcal':2200,'macros':{'protein_g':{'low_g':110}}})
    assert len(meals)==21 and not any(m['slot']=='snack' for m in meals)
    assert all(i['name'] not in {'虾','三文鱼'} for m in meals for i in m['items'])


def test_all_catalog_exercises_have_chinese_display_names():
    from app.domain.exercise_catalog import EXERCISE_CATALOG
    from app.domain.catalog import name_of
    assert all(any('\u4e00'<=c<='\u9fff' for c in name_of(k)) for k in EXERCISE_CATALOG)
    assert name_of('wall-sit')=='靠墙静蹲'
    assert name_of('hip-airplane')=='站立髋部飞机式'


def test_empty_or_alias_mcd_token_never_uses_server_credential(monkeypatch):
    from app.food.mcd_mcp import resolve_token
    from app.api.coach_routes import coach_commerce_dependency
    from starlette.requests import Request
    from fastapi import FastAPI
    app=FastAPI();app.state.settings=SimpleNamespace(mcd_mcp_token='server-secret',mcd_mcp_token_alias='alias',mcd_mcp_url='https://mcp.invalid',mcd_mcp_timeout=3,mcd_mcp_allow_order=False)
    r=Request({'type':'http','headers':[],'app':app})
    assert coach_commerce_dependency(r) is None
    assert resolve_token('')=='' and resolve_token('alias')=='alias'


def test_pending_replacement_shows_proposed_name_before_actual_write(state):
    def load(d):return {'mode':'load_context','snapshot_id':d['snapshot']['snapshot_id'],'context_request':{'exercise_catalog':True}}
    def replace(d):
        a=next(o for o in d['snapshot']['objects'] if o['type']=='workout_action' and o['action_ref']=='goblet-squat')
        e=next(o for o in d['snapshot']['objects'] if o['type']=='exercise' and o['action_ref']=='wall-sit')
        change=task(a['id'],'workout.replace_action',exercise_id=e['id']);change.needs_confirmation=True
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[change.model_dump()]}
    state.client.client.responses.extend([load,replace]);before=state.snapshot()['days']
    r=post(state,'把深蹲换成靠墙静蹲，先确认')
    assert r['overall_status']=='confirmation_required',r
    assert '→ 靠墙静蹲' in r['reply'] and '尚未改变' in r['reply']
    assert state.snapshot()['days']==before


def test_cancel_stops_inference_before_business_execution(state):
    import threading,time
    from concurrent.futures import ThreadPoolExecutor
    from app.llm.deadline import remaining_timeout
    from tests.test_semantic_binding import Scripted
    entered=threading.Event()
    class Slow(Scripted):
        def complete(self,*args,**kwargs):
            entered.set()
            for _ in range(100):
                remaining_timeout();time.sleep(0.02)
            raise AssertionError('cancellation did not interrupt model work')
    state.client.client=Slow([])
    before=state.snapshot()['days'];payload={'text':'重新生成计划','thread':state.thread,'date':str(DAY),'request_id':'cancel-model-test'}
    with ThreadPoolExecutor(max_workers=1) as pool:
        job=pool.submit(state.http.post,'/api/v1/agent/chat',headers=state.headers,json=payload)
        assert entered.wait(3)
        stopped=state.http.post('/api/v1/agent/requests/cancel-model-test/cancel',headers=state.headers)
        assert stopped.json()['data']['status']=='cancel_requested',stopped.text
        reply=job.result(timeout=5)
    assert reply.status_code==200,reply.text
    assert reply.json()['data']['overall_status']=='cancelled'
    assert state.snapshot()['days']==before
    replay=state.http.post('/api/v1/agent/chat',headers=state.headers,json=payload)
    assert replay.json()['data']==reply.json()['data']


def test_cancellation_cannot_claim_already_dispatched_work_is_undone(state):
    from app.models import AgentRun
    from uuid import uuid5,NAMESPACE_URL
    key=str(uuid5(NAMESPACE_URL,f'coach:{DEMO_USER_ID}:executing-test'))
    with state.factory() as db:
        db.add(AgentRun(id=key,user_id=DEMO_USER_ID,kind='coach_request',mode='agent',status='running',input={'phase':'executing'}));db.commit()
    reply=state.http.post('/api/v1/agent/requests/executing-test/cancel',headers=state.headers)
    assert reply.json()['data']['status']=='too_late'
    with state.factory() as db:assert db.get(AgentRun,key).status=='running'


def test_cancellation_closes_only_the_active_http_request(monkeypatch):
    import httpx,asyncio
    from app.llm.cancellation import cancellation_scope,cancellable_post,ModelCancelled
    real=httpx.AsyncClient;was_cancelled=[];probes=[]
    async def wait(request):
        try:await asyncio.sleep(10)
        except asyncio.CancelledError:
            was_cancelled.append(True);raise
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real(transport=httpx.MockTransport(wait),**kw))
    def check():probes.append(1);return len(probes)>3
    with pytest.raises(ModelCancelled),cancellation_scope(check,lambda:None):
        asyncio.run(cancellable_post('https://test.invalid/model',headers={},body={},timeout=15))
    assert was_cancelled==[True]


def test_registered_execution_labels_fit_postgres_contract():
    from app.agent.capabilities import REGISTRY
    from app.agent.initial_plan_runtime import CAPABILITIES as a
    from app.agent.day_plan_capabilities import CAPABILITIES as b
    from app.agent.workout_capabilities import CAPABILITIES as c
    from app.agent.commerce_capabilities import CAPABILITIES as d
    from app.models import CoachTaskExecution
    columns=CoachTaskExecution.__table__.c
    for cap in [cap for group in (REGISTRY,a,b,c,d) for cap in group.values()]:
        assert len(cap.capability_id)<=columns.operation.type.length
        assert len(cap.domain)<=columns.domain.type.length
        assert len(cap.executor)<=columns.tool.type.length
        assert all(len(t)<=columns.scope.type.length for t in cap.target_types)


def test_allergy_template_version_preserves_current_session_target_ids(state):
    from app.models import PlanAdjustment,PlanAction,WorkoutSession
    from app.domain.day_plan import effective_day
    from app.domain.meal_safety import repair_saved_future_meals
    with state.factory() as db:
        user=db.get(User,DEMO_USER_ID);profile=db.get(Profile,user.id)
        for row in db.scalars(select(PlanAdjustment).where(PlanAdjustment.user_id==user.id)):row.status='reset'
        plan=db.scalar(select(Plan).where(Plan.user_id==user.id))
        version=db.scalar(select(PlanVersion).where(PlanVersion.plan_id==plan.id,PlanVersion.version==plan.current_version))
        actions=db.scalars(select(PlanAction).where(PlanAction.plan_version_id==version.id)).all()
        first=min(a.day_index for a in actions)
        for a in actions:
            if a.day_index==first:a.day_index=DAY.isoweekday()
        db.add(MealPlan(user_id=user.id,plan_version_id=version.id,day_index=DAY.isoweekday(),slot='dinner',title='晚餐',items=[{'name':'虾','grams':100,'kcal':99,'protein':20,'fat':1,'carb':0}]))
        db.commit()
        before=effective_day(db,user,DAY,fallback_next=False)
        items=[{'id':'record-'+str(n),'action_ref':a['action_ref'],'source_action_id':a['id'],'completed_sets':1,'set_logs':[{'set_no':1,'reps':8}]} for n,a in enumerate(before['actions'])]
        session=WorkoutSession(user_id=user.id,date=DAY,status='active',items=copy.deepcopy(items));db.add(session);db.flush()
        profile.allergies=['海鲜'];repair_saved_future_meals(db,user,profile);db.commit()
        after=effective_day(db,user,DAY,fallback_next=False)
        assert after['actions']==before['actions']
        assert db.get(WorkoutSession,session.id).items==items
        assert all(i['name']!='虾' for m in after['meals'] for i in m['items'])


def test_allergy_update_does_not_reactivate_stale_day_override(state):
    from app.models import PlanAdjustment
    from app.domain.day_plan import effective_day
    from app.domain.meal_safety import repair_saved_future_meals
    with state.factory() as db:
        user=db.get(User,DEMO_USER_ID);profile=db.get(Profile,user.id)
        plan=db.scalar(select(Plan).where(Plan.user_id==user.id))
        stale=db.scalar(select(PlanAdjustment).where(PlanAdjustment.user_id==user.id,PlanAdjustment.status=='active'))
        stale.base_version=plan.current_version-1
        version=db.scalar(select(PlanVersion).where(PlanVersion.plan_id==plan.id,PlanVersion.version==plan.current_version))
        db.add(MealPlan(user_id=user.id,plan_version_id=version.id,day_index=DAY.isoweekday(),slot='dinner',title='晚餐',items=[{'name':'虾','grams':100}]))
        db.commit();before=effective_day(db,user,DAY,fallback_next=False)
        profile.allergies=['海鲜'];repair_saved_future_meals(db,user,profile);db.commit()
        assert stale.base_version!=plan.current_version
        after=effective_day(db,user,DAY,fallback_next=False)
        assert after['source']=='plan'
        assert [a['action_ref'] for a in after['actions']]==[a['action_ref'] for a in before['actions']]


def test_multi_allergy_repair_uses_canonical_safe_substitute_and_recalculates():
    from app.domain.meal_safety import repair_meals,check_meals
    p=SimpleNamespace(allergies=['海鲜','鸡胸肉过敏','豆腐'],diet_preferences=[])
    meals,changed=repair_meals([{'slot':'dinner','items':[{'name':'虾','grams':100}]}],p)
    assert changed and meals[0]['items'][0]['name']=='牛肉'
    assert meals[0]['kcal']==250
    check_meals(meals,p)
