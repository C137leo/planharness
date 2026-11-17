"""Continuous workflow regressions, isolated SQLite and scripted model, no live health data."""
import copy
import json
import pytest
from sqlalchemy import select
from tests.test_day_revision import state
from tests.test_day_revision_chat import post
from tests.test_semantic_binding import task,Scripted
from app.models import PlanDraft,CoachPendingPlan,Profile,User,Plan
from app.seed import DEMO_USER_ID
from app.agent.initial_plan_runtime import draft_revision
from app.agent.business_snapshot import build_snapshot
from app.domain.meal_safety import check_meals
from app.evals.dialogue_runner import DAY


def create_preview(d):
    p=next(o for o in d['snapshot']['objects'] if o['type']=='profile')
    return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(p['id'],'plan.create_replacement_draft',instruction='按档案重建长期训练与三餐，只预览').model_dump()]}


def revise_preview(d):
    p=next(o for o in d['snapshot']['objects'] if o['type']=='replacement_plan_draft')
    return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(p['id'],'plan.revise_replacement_draft',instruction='每次50分钟，其他不变',expected_revision=p['etag']).model_dump()]}


def handoff(d):
    return {'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],
        'execute_revision':False,'keep_task_ids':[],'edit_task_ids':[t['task_id'] for t in d['pending']['tasks']],
        'new_request_text':'修改当前长期预览，保留其他要求'}


def allergy(d):
    p=next(o for o in d['snapshot']['objects'] if o['type']=='profile')
    return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(p['id'],'profile.add_allergies',allergies=['海鲜']).model_dump()]}


def first(state):
    state.client.client.responses.append(create_preview)
    r=post(state,'换一份长期训练和三餐计划，先预览')
    assert r['overall_status']=='confirmation_required',r
    return r


def test_revision_supersedes_old_preview_and_old_confirmation(state):
    a=first(state)
    state.client.client.responses.extend([handoff,revise_preview])
    b=post(state,'这份长期计划修改一下，其他要求保留')
    assert b['overall_status']=='confirmation_required',b
    with state.factory() as db:
        assert db.get(PlanDraft,a['plan_draft']['draft_id']).status=='superseded'
        assert db.get(CoachPendingPlan,a['pending_confirmation']['pending_id']).status!='pending'
    p=a['pending_confirmation']
    r=post(state,'确认旧版',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert r['overall_status'] in {'superseded','cancelled'}
    assert '已结束' in r['reply']


def test_cancel_after_failed_edit_accepts_old_button_and_does_not_resurrect(state):
    a=first(state);p=a['pending_confirmation']
    with state.factory() as db:
        row=db.get(CoachPendingPlan,p['pending_id']);row.version+=1
        row.result={**row.result,'unresolved_goal_input':'排除虾'};db.commit()
    r=post(state,'取消',confirmation={'action':'cancel','pending_id':p['pending_id'],'version':p['version']})
    assert r['overall_status']=='cancelled',r
    before=state.snapshot()
    again=post(state,'确认',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert again['overall_status']=='cancelled' and state.snapshot()['days']==before['days']
    with state.factory() as db:
        assert db.get(PlanDraft,a['plan_draft']['draft_id']).status=='rejected'
        fresh=build_snapshot(db,db.get(User,DEMO_USER_ID),state.thread,[str(DAY)])
        assert not any(o['type']=='replacement_plan_draft' for o in fresh.payload['objects'])


def test_allergy_while_previewing_repairs_preview_without_asking_if_shrimp_should_be_removed(state):
    a=first(state);old=a['pending_confirmation']
    state.client.client.responses.extend([handoff,allergy])
    r=post(state,'我海鲜过敏，修改一下')
    assert r['overall_status']=='success',r
    assert r.get('plan_draft') and r.get('pending_confirmation'),r
    assert r['pending_confirmation']['pending_id']!=old['pending_id']
    assert '海鲜' in r['reply'] and '是否' not in r['reply']
    with state.factory() as db:
        profile=db.get(Profile,DEMO_USER_ID)
        assert '海鲜' in profile.allergies
        row=db.get(PlanDraft,r['plan_draft']['draft_id']);check_meals(row.payload['meals'],profile)
        assert db.get(CoachPendingPlan,old['pending_id']).status!='pending'
    p=r['pending_confirmation']
    done=post(state,'确认安全新版',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert done['overall_status']=='success',done


def test_cancel_then_allergy_does_not_reopen_the_cancelled_plan(state):
    a=first(state);p=a['pending_confirmation']
    post(state,'取消',confirmation={'action':'cancel','pending_id':p['pending_id'],'version':p['version']})
    state.client.client.responses.append(allergy)
    r=post(state,'我海鲜过敏，修改长期餐饮')
    assert r['overall_status']=='success' and not r.get('pending_confirmation'),r
    with state.factory() as db:assert db.get(PlanDraft,a['plan_draft']['draft_id']).status=='rejected'


def test_safety_and_long_term_preview_are_executed_in_separate_stages(state):
    first(state)
    model=state.client.client;original_complete=model.complete
    def with_goal(messages,**kwargs):
        data=json.loads(messages[-1]['content'])
        if 'initial_goal_input' in data:
            return {'message':{'content':json.dumps({'duration':{'kind':'target','minutes':50}})}}
        return original_complete(messages,**kwargs)
    model.complete=with_goal
    def mixed(d):
        a=allergy(d)['tasks'][0];b=revise_preview(d)['tasks'][0]
        a['task_id']='save-allergy';b['task_id']='edit-preview';b['dependencies']=['save-allergy']
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[a,b]}
    state.client.client.responses.extend([handoff,mixed])
    r=post(state,'海鲜过敏加入长期档案，并修改这份长期预览')
    assert r['overall_status']=='confirmation_required',r
    assert {t['capability_id'] for t in r['task_results']}=={'profile.add_allergies','plan.revise_replacement_draft'}
    assert all(sum(a['duration_min'] for a in d['actions'])==50 for d in r['plan_draft']['days'])
    with state.factory() as db:check_meals(r['plan_draft']['meals'],db.get(Profile,DEMO_USER_ID))


def test_mixed_day_lifecycle_plan_gets_bounded_semantic_repair(state):
    def invalid(d):
        a=allergy(d)['tasks'][0]
        b=task(next(o['id'] for o in d['snapshot']['objects'] if o['type']=='day'),'day_plan.create_revision').model_dump()
        b['task_id']='bad-mixed-day'
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[a,b]}
    state.client.client.responses.extend([invalid,allergy])
    r=post(state,'我海鲜过敏，修改长期餐饮')
    assert r['overall_status']=='success',r
    with state.factory() as db:assert '海鲜' in db.get(Profile,DEMO_USER_ID).allergies


def test_excluding_food_does_not_require_creating_a_missing_snack(state):
    from app.agent.initial_requirements import InitialRequirements,apply_and_validate
    a=first(state)
    with state.factory() as db:
        row=db.get(PlanDraft,a['plan_draft']['draft_id']);payload=copy.deepcopy(row.payload)
        payload['meals']=[m for m in payload['meals'] if m['slot']!='snack']
        goal=InitialRequirements(meal_rules=[{'slot':'snack','requirement':'exclude','ingredient_options':['虾']}])
        apply_and_validate(payload,goal,db.get(Profile,DEMO_USER_ID))
        assert not any(m['slot']=='snack' for m in payload['meals'])


def test_legacy_ready_ancestor_does_not_reappear_after_newer_draft_cancelled(state):
    a=first(state)
    state.client.client.responses.extend([handoff,revise_preview])
    b=post(state,'改这份长期草案')
    p=b['pending_confirmation']
    post(state,'取消',confirmation={'action':'cancel','pending_id':p['pending_id'],'version':p['version']})
    # Reproduce rows left by the old replacement-plan implementation.
    with state.factory() as db:
        db.get(PlanDraft,a['plan_draft']['draft_id']).status='ready'
        db.get(CoachPendingPlan,a['pending_confirmation']['pending_id']).status='pending'
        db.commit()
    state.client.client.responses.append(allergy)
    r=post(state,'我海鲜过敏，长期排除')
    assert r['overall_status']=='success' and not r.get('pending_confirmation'),r
    with state.factory() as db:
        assert db.get(CoachPendingPlan,a['pending_confirmation']['pending_id']).status=='superseded'
        snap=build_snapshot(db,db.get(User,DEMO_USER_ID),state.thread,[str(DAY)])
        assert not any(o['type']=='replacement_plan_draft' for o in snap.payload['objects'])
        assert any(w['status']=='rejected' for w in snap.payload['long_term_workflows'])


def test_allergy_removes_conflicting_old_food_requirement_but_keeps_training_goal(state):
    a=first(state)
    with state.factory() as db:
        row=db.get(PlanDraft,a['plan_draft']['draft_id'])
        payload=copy.deepcopy(row.payload);contract=payload['meta']['goal_contract']
        contract['meal_rules']=[{'slot':'dinner','ingredient_options':['虾'],'requirement':'include','grams':100}]
        contract['duration']={'kind':'target','minutes':50,'tolerance_minutes':0}
        row.payload=payload;db.commit()
    state.client.client.responses.extend([handoff,allergy])
    r=post(state,'新增海鲜过敏，原来要虾的要求作废，训练目标保留')
    assert r['overall_status']=='success',r
    with state.factory() as db:
        row=db.get(PlanDraft,a['plan_draft']['draft_id']);goal=row.payload['meta']['goal_contract']
        assert goal['meal_rules']==[] and goal['duration']['minutes']==50
        check_meals(row.payload['meals'],db.get(Profile,DEMO_USER_ID))


def test_preview_failure_does_not_undo_allergy_or_allow_old_plan_confirmation(state,monkeypatch):
    from app.domain import coach
    from app.domain.patches import PatchValidationError
    first(state)
    def broken(*args,**kwargs):raise PatchValidationError(['模拟预览失败'])
    monkeypatch.setattr(coach,'create_draft',broken)
    def mixed(d):
        a=allergy(d)['tasks'][0];b=revise_preview(d)['tasks'][0]
        a['task_id']='save-allergy';b['task_id']='edit-preview'
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[a,b]}
    state.client.client.responses.extend([handoff,mixed])
    r=post(state,'海鲜过敏长期排除，草案改为50分钟')
    assert r['overall_status']=='partial_success',r
    assert not any(a['type']=='confirm_tasks' for a in r['actions'])
    with state.factory() as db:assert '海鲜' in db.get(Profile,DEMO_USER_ID).allergies
    p=r['pending_confirmation']
    result=post(state,'确认',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert result['overall_status']=='confirmation_required' and '尚未满足' in result['reply']
