"""Real domain/SQLite tests, not NLU scores or a fake chat implementation."""
import copy
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select,func
from app.evals.dialogue_runner import DialogueEnvironment,DAY
from app.models import User,PlanAdjustment,DayPlanRevision,MealLog,WorkoutSession,WorkoutLog
from app.seed import DEMO_USER_ID
from app.domain import day_revision as r,workout
from app.domain.day_plan import effective_day,save_override
from app.agent.workout_capabilities import RecordProgress
from app.domain import workout_records
from tests.test_semantic_binding import Scripted

@pytest.fixture
def state(tmp_path):
    env=DialogueEnvironment(tmp_path/'revision.db','day-revision-tests',Scripted([]))
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);day=effective_day(db,user,DAY)
        actions=[{'id':'warmup','action_ref':'dynamic_warmup','name':'动态热身','slot':'warmup','sets':1,'duration_min':5}]+day['actions']
        save_override(db,user,DAY,actions=actions,meals=day['meals'],summary='draft fixture',constraints={},reason='synthetic',base_version=day['plan_version']);db.commit()
    yield env
    env.close()

def create(db,user,day,**kwargs):
    return r.create(db,user,day,expected_base_revision=r.digest(r.context(db,user,day)),**kwargs)


def shortened(ctx):
    return {'actions':[{'id':a['id'],'sets':1 if a['slot']=='warmup' else 2,'duration_min':5} for a in ctx['day']['actions']]}

def dinner(grams=180,food='豆腐'):
    return {'meals':[{'slot':'dinner','items':[{'ingredient_key':food,'grams':grams},{'ingredient_key':'米饭','grams':150}]}]}

def test_continuous_draft_preview_edit_apply_exact_revision_and_idempotent_replay(state):
    with state.factory() as db:
        user=db.get(User,DEMO_USER_ID);original=r.context(db,user,DAY)
        draft=create(db,user,DAY,scope=['workout','dinner'],proposal={**shortened(original),**dinner()},constraints={'max_workout_minutes':20})
        assert r.context(db,user,DAY)==original
        assert any(c['kind']=='updated' for c in draft['changes']) and any(c['kind']=='unchanged' for c in draft['changes'])
        db.commit();ident=draft['revision_id'];old=draft['etag']
    with state.factory() as db:
        user=db.get(User,DEMO_USER_ID);assert r.preview(db,user,ident)['etag']==old
        edited=r.update_draft(db,user,ident,expected=old,proposal=dinner(210,'三文鱼'))
        assert edited['version']==2 and edited['constraints']['max_workout_minutes']==20
        assert edited['target_plan']['actions']==draft['target_plan']['actions']
        db.commit()
    with state.factory() as db:
        user=db.get(User,DEMO_USER_ID)
        with pytest.raises(r.RevisionError,match='版本'):r.apply(db,user,ident,confirmed_etag=old)
        db.rollback();applied=r.apply(db,user,ident,confirmed_etag=edited['etag']);db.commit()
        assert applied['status']=='applied' and applied['result']['verified']
        after=r.context(db,user,DAY)
        assert after['day']['actions']==edited['target_plan']['actions']
        assert next(m for m in after['day']['meals'] if m['slot']=='lunch')==next(m for m in original['day']['meals'] if m['slot']=='lunch')
        assert after['facts']==original['facts']
        count=db.scalar(select(func.count()).select_from(PlanAdjustment));db.commit()
        assert r.apply(db,user,ident,confirmed_etag=edited['etag'])['result']==applied['result'];db.commit()
        assert db.scalar(select(func.count()).select_from(PlanAdjustment))==count


def test_scope_cannot_expand_on_create_or_later_update(state):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx=r.context(db,u,DAY)
        with pytest.raises(r.RevisionError,match='训练修改权限'):create(db,u,DAY,scope=['dinner'],proposal=shortened(ctx))
        draft=create(db,u,DAY,scope=['workout'],proposal=shortened(ctx));db.commit()
        with pytest.raises(r.RevisionError,match='餐次'):r.update_draft(db,u,draft['revision_id'],expected=draft['etag'],proposal=dinner())
        assert r.context(db,u,DAY)==ctx


def test_completed_training_and_eaten_meal_cannot_be_removed_or_modified(state):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);s=workout.start_session(db,u,DAY)
        workout_records.progress(db,u,s.id,s.items[0]['id'],RecordProgress(expected_revision=workout_records.revision(s)))
        db.add(MealLog(user_id=u.id,date=DAY,slot='dinner',title='已吃',kcal=400));db.commit();ctx=r.context(db,u,DAY)
        proposal=shortened(ctx);proposal['actions']=proposal['actions'][1:]
        with pytest.raises(r.RevisionError) as exc:create(db,u,DAY,scope=['workout'],proposal=proposal)
        assert exc.value.code=='completed_workout'
        with pytest.raises(r.RevisionError) as exc:create(db,u,DAY,scope=['dinner'],proposal=dinner())
        assert exc.value.code=='completed_meal'
        assert r.context(db,u,DAY)==ctx


def test_new_actual_fact_after_preview_invalidates_confirmation(state):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx=r.context(db,u,DAY)
        draft=create(db,u,DAY,scope=['workout'],proposal=shortened(ctx));db.commit()
        db.add(MealLog(user_id=u.id,date=DAY,slot='lunch',title='new fact',kcal=500));db.commit()
        before=r.context(db,u,DAY)
        with pytest.raises(r.RevisionError) as exc:r.apply(db,u,draft['revision_id'],confirmed_etag=draft['etag'])
        assert exc.value.code=='conflict';db.commit()
        assert r.preview(db,u,draft['revision_id'])['status']=='conflicted'
        assert r.context(db,u,DAY)==before


def test_plan_version_or_adjustment_change_invalidates_old_draft(state):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx=r.context(db,u,DAY)
        draft=create(db,u,DAY,scope=['dinner'],proposal=dinner());db.commit()
        day=ctx['day'];save_override(db,u,DAY,actions=day['actions'],meals=day['meals'],summary='other edit',constraints={},reason='new',base_version=day['plan_version']);db.commit()
        with pytest.raises(r.RevisionError) as exc:r.apply(db,u,draft['revision_id'],confirmed_etag=draft['etag'])
        assert exc.value.code=='conflict'


@pytest.mark.parametrize('constraints,proposal,code',[
    ({'max_workout_minutes':10},None,'time_budget'),
    ({'excluded_ingredients':['豆腐']},'food','ingredient_constraint'),
    ({},'unknown','nutrition_source'),
    ({},'foreign','ownership'),
    ({},'duplicate','duplicate'),
])
def test_constraints_and_identity_are_structurally_enforced(state,constraints,proposal,code):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx=r.context(db,u,DAY)
        desired=dinner(food='not-in-catalog') if proposal=='unknown' else dinner() if proposal=='food' else shortened(ctx)
        if proposal=='foreign':desired['actions'][1]['id']='foreign-owned-id'
        if proposal=='duplicate':desired['actions'].append(copy.deepcopy(desired['actions'][1]))
        if proposal=='unknown':
            from pydantic import ValidationError
            with pytest.raises(ValidationError) as exc:create(db,u,DAY,scope=['workout','dinner'],proposal=desired,constraints=constraints)
            assert exc.value.errors()[0]['loc'][-1]=='ingredient_key'
        else:
            with pytest.raises(r.RevisionError) as exc:create(db,u,DAY,scope=['workout','dinner'],proposal=desired,constraints=constraints)
            assert exc.value.code==code
        assert r.context(db,u,DAY)==ctx


def test_cancel_and_expiry_do_not_modify_daily_plan(state):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx=r.context(db,u,DAY)
        draft=create(db,u,DAY,scope=['dinner'],proposal=dinner());db.commit()
        r.cancel(db,u,draft['revision_id'],expected=draft['etag']);db.commit()
        with pytest.raises(r.RevisionError):r.apply(db,u,draft['revision_id'],confirmed_etag=draft['etag'])
        db.rollback();draft=create(db,u,DAY,scope=['dinner'],proposal=dinner());db.commit()
        row=db.get(DayPlanRevision,draft['revision_id']);row.expires_at=dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=1);db.commit()
        assert r.preview(db,u,row.id)['status']=='expired'
        assert r.context(db,u,DAY)==ctx


def test_foreign_user_cannot_preview_update_cancel_or_apply(state):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);draft=create(db,u,DAY,scope=['dinner'],proposal=dinner());db.commit()
        from types import SimpleNamespace
        other=SimpleNamespace(id='another-user')
        with pytest.raises(r.RevisionError):r.preview(db,other,draft['revision_id'])
        # The lock user row doesn't exist; ownership still rejects every write path.
        for call in [lambda:r.cancel(db,other,draft['revision_id'],expected=draft['etag']),lambda:r.update_draft(db,other,draft['revision_id'],expected=draft['etag'],proposal=dinner()),lambda:r.apply(db,other,draft['revision_id'],confirmed_etag=draft['etag'])]:
            with pytest.raises(r.RevisionError) as exc:call()
            assert exc.value.code=='ownership'
            db.rollback()


def test_verification_failure_rolls_back_plan_and_session_targets(state,monkeypatch):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);workout.start_session(db,u,DAY)
        before=r.context(db,u,DAY);draft=create(db,u,DAY,scope=['workout'],proposal=shortened(before));db.commit()
        actual=r.save_override
        def corrupt(*args,**kwargs):
            result=actual(*args,**kwargs)
            row=db.get(PlanAdjustment,result['adjustment_id']);row.actions=[];db.flush()
            return result
        monkeypatch.setattr(r,'save_override',corrupt)
        with pytest.raises(r.RevisionError) as exc:r.apply(db,u,draft['revision_id'],confirmed_etag=draft['etag'])
        assert exc.value.code=='verification'
        db.commit() # even a caller committing after catching cannot keep half a plan
        assert r.context(db,u,DAY)==before
        assert r.preview(db,u,draft['revision_id'])['status']=='awaiting_confirmation'


def test_concurrent_repeated_confirmation_applies_only_one_adjustment(state):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);draft=create(db,u,DAY,scope=['dinner'],proposal=dinner());db.commit()
        count=db.scalar(select(func.count()).select_from(PlanAdjustment))
    def apply_once(_):
        with state.factory() as db:
            u=db.get(User,DEMO_USER_ID);out=r.apply(db,u,draft['revision_id'],confirmed_etag=draft['etag']);db.commit();return out['result']['adjustment_id']
    with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(apply_once,range(2)))
    assert len(set(ids))==1
    with state.factory() as db:assert db.scalar(select(func.count()).select_from(PlanAdjustment))==count+1


def test_base_template_warmup_ids_are_stable_without_writing_an_override(state):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID)
        next_day=next(DAY+dt.timedelta(days=n) for n in range(1,8) if r.context(db,u,DAY+dt.timedelta(days=n))['day']['actions'])
        before=db.scalar(select(func.count()).select_from(PlanAdjustment))
        ctx=r.context(db,u,next_day);assert ctx['day']['actions']
        assert ctx==r.context(db,u,next_day)
        proposal={'actions':[{'id':a['id']} for a in ctx['day']['actions']]}
        draft=create(db,u,next_day,scope=['workout'],proposal=proposal);db.commit()
        assert db.scalar(select(func.count()).select_from(PlanAdjustment))==before
        assert r.apply(db,u,draft['revision_id'],confirmed_etag=draft['etag'])['result']['verified'];db.commit()


def test_unlinked_legacy_log_protects_completed_action_without_blocking_dinner_scope(state):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx=r.context(db,u,DAY)
        a=ctx['day']['actions'][1]
        db.add(WorkoutLog(user_id=u.id,date=DAY,action_ref=a['action_ref'],sets=2,duration_min=6));db.commit()
        with pytest.raises(r.RevisionError) as exc:create(db,u,DAY,scope=['workout'],proposal=shortened(ctx))
        assert exc.value.code=='completed_workout'
        assert create(db,u,DAY,scope=['dinner'],proposal=dinner())['status']=='awaiting_confirmation'


def test_invalid_nutrition_and_fake_action_fields_are_rejected_at_schema_boundary(state):
    from pydantic import ValidationError
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID)
        with pytest.raises(ValidationError):create(db,u,DAY,scope=['dinner'],proposal={'meals':[{'slot':'dinner','items':[{'ingredient_key':'豆腐','grams':100,'kcal':9999}]}]})
        ctx=r.context(db,u,DAY)
        with pytest.raises(r.RevisionError) as exc:create(db,u,DAY,scope=['workout'],proposal={'actions':[{'id':ctx['day']['actions'][0]['id'],'action_ref':None}]})
        assert exc.value.code=='schema'


def test_equipment_constraint_rejects_new_catalog_exercise_that_needs_unavailable_gear(state):
    from app.domain.day_plan import _action_equipment,_norm_equipment
    from app.domain.catalog import all_exercise_refs
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx=r.context(db,u,DAY)
        ref=next(ref for ref in all_exercise_refs() if _norm_equipment(_action_equipment(ref)) not in ctx['allowed_equipment'])
        desired=shortened(ctx);desired['actions'][1]['action_ref']=ref
        with pytest.raises(r.RevisionError) as exc:create(db,u,DAY,scope=['workout'],proposal=desired)
        assert exc.value.code=='equipment'


def test_draft_creation_rejects_context_that_changed_while_model_was_planning(state):
    with state.factory() as db:
        u=db.get(User,DEMO_USER_ID);ctx=r.context(db,u,DAY);original=r.digest(ctx)
        db.add(MealLog(user_id=u.id,date=DAY,slot='lunch',title='during planning',kcal=200));db.commit()
        with pytest.raises(r.RevisionError) as exc:r.create(db,u,DAY,expected_base_revision=original,scope=['workout'],proposal=shortened(ctx))
        assert exc.value.code=='conflict'
        assert db.scalar(select(func.count()).select_from(DayPlanRevision))==0
