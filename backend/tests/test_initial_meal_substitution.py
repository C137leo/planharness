"""Initial preview meal selection: temporary SQLite and scripted model only."""
import copy
import json
import pytest
from sqlalchemy import select
from tests.test_day_revision import state
from tests.test_day_revision_chat import post
from tests.test_semantic_binding import task
from app.models import Plan, PlanDraft, User
from app.seed import DEMO_USER_ID
from app.agent.business_snapshot import business_state
from app.evals.dialogue_runner import DAY
from app.domain.nutrition_catalog import ingredient_key


@pytest.mark.parametrize('count', [2, 3])
def test_select_actual_chicken_meals_without_applying_before_confirmation(state, count):
    with state.factory() as db:
        for plan in db.scalars(select(Plan).where(Plan.user_id == DEMO_USER_ID)):
            plan.status = 'inactive'
        db.commit()
    def create(data):
        profile = next(o for o in data['snapshot']['objects'] if o['type'] == 'profile')
        return {'snapshot_id': data['snapshot']['snapshot_id'], 'tasks': [task(profile['id'], 'plan.create_initial_draft', instruction='生成首份周计划').model_dump()]}
    model = state.client.client
    model.responses.append(create)
    first = post(state, '生成首份周计划，先预览')
    assert first['overall_status'] == 'confirmation_required', first
    before_meals = copy.deepcopy(first['plan_draft']['meals'])
    chicken = {(m['day_index'], m['slot']) for m in before_meals if any(ingredient_key(i['name']) == '鸡胸肉' for i in m['items'])}
    assert len(chicken) >= count
    with state.factory() as db:
        before = business_state(db, db.get(User, DEMO_USER_ID), [str(DAY)])
    original = model.complete
    def complete(messages, **kwargs):
        data = json.loads(messages[-1]['content'])
        if 'initial_goal_input' in data:
            assert data['current_meals'] == before_meals
            return {'message': {'content': json.dumps({'meal_substitutions': [{'source': '鸡胸肉', 'target': '牛肉', **({'count': count} if count == 3 else {}), 'selected_meals': [[7, 'snack']]}]})}}
        return original(messages, **kwargs)
    model.complete = complete
    def handoff(data):
        return {'action': 'new_request', 'pending_id': data['pending']['pending_id'], 'version': data['pending']['version'], 'execute_revision': False, 'keep_task_ids': [], 'edit_task_ids': [t['task_id'] for t in data['pending']['tasks']], 'new_request_text': '鸡胸肉太多了选几顿换牛肉'}
    def revise(data):
        draft = next(o for o in data['snapshot']['objects'] if o['type'] == 'initial_plan_draft')
        return {'snapshot_id': data['snapshot']['snapshot_id'], 'tasks': [task(draft['id'], 'plan.revise_initial_draft', instruction='鸡胸肉太多了选几顿换牛肉', expected_revision=draft['etag']).model_dump()]}
    model.responses.extend([handoff, revise])
    revised = post(state, '鸡胸肉太多了选几顿换牛肉')
    assert revised['overall_status'] == 'confirmation_required', revised
    assert '未指定餐次' not in revised['reply']
    changed = []
    for old, new in zip(before_meals, revised['plan_draft']['meals'], strict=True):
        if old != new:
            identity = (new['day_index'], new['slot'])
            changed.append(identity)
            assert identity in chicken
            assert '鸡胸肉' not in {ingredient_key(i['name']) for i in new['items']}
            assert '牛肉' in {ingredient_key(i['name']) for i in new['items']}
    assert len(changed) == count
    assert revised['plan_draft']['days'] == first['plan_draft']['days']
    with state.factory() as db:
        assert business_state(db, db.get(User, DEMO_USER_ID), [str(DAY)]) == before
        assert db.get(PlanDraft, first['plan_draft']['draft_id']).status == 'superseded'
    old_pending = first['pending_confirmation']
    stale = post(state, '确认旧版', confirmation={'action': 'confirm', 'pending_id': old_pending['pending_id'], 'version': old_pending['version']})
    assert stale['overall_status'] == 'superseded', stale
    pending = revised['pending_confirmation']
    done = post(state, '确认新版', confirmation={'action': 'confirm', 'pending_id': pending['pending_id'], 'version': pending['version']})
    assert done['overall_status'] == 'success', done
    with state.factory() as db:
        assert db.scalar(select(Plan).where(Plan.user_id == DEMO_USER_ID, Plan.status == 'active')) is not None


class GoalModel:
    """Scripted independent requirement extraction; no network/model provider."""
    def __init__(self, delta):
        self.delta = delta

    def available(self):
        return True

    def complete(self, messages, **kwargs):
        data = json.loads(messages[-1]['content'])
        assert 'initial_goal_input' in data
        return {'message': {'content': json.dumps(self.delta)}}


def goal_delta(delta, parent=None):
    from app.agent.initial_requirements import extract_goal
    return extract_goal(GoalModel(delta), '脚本中的本轮要求',
                        parent.payload['meta']['goal_contract'] if parent else {}, {}, [],
                        current_meals=parent.payload['meals'] if parent else ())


@pytest.fixture
def initial_db(state):
    from app.models import Profile
    with state.factory() as db:
        user = db.get(User, DEMO_USER_ID)
        for plan in db.scalars(select(Plan).where(Plan.user_id == user.id)):
            plan.status = 'inactive'
        profile = db.get(Profile, user.id)
        profile.allergies = []
        profile.diet_preferences = []
        db.commit()
        yield db, user


def preview(db, user, delta, parent=None):
    from app.domain.coach import create_draft
    return create_draft(db, user, '脚本中的本轮要求',
                        parent_draft_id=parent.id if parent else None,
                        goal_contract=goal_delta(delta, parent), commit=False)


def meal_map(row):
    return {(m['day_index'], m['slot']): m for m in row.payload['meals']}


def test_additive_food_limits_and_explicit_partial_removal(initial_db):
    from app.agent.initial_requirements import profile_inputs
    from app.models import Profile
    from app.domain.allergens import resolve_tags
    from app.domain.nutrition_catalog import PLANNING_FOODS
    db, user = initial_db
    before = business_state(db, user, [str(DAY)])
    first = preview(db, user, {'allergies': ['牛奶', '鸡蛋'], 'food_exclusions': ['牛肉', '猪肉']})
    second = preview(db, user, {'allergies': ['虾', '牛奶'], 'food_exclusions': ['羊肉']}, first)
    contract = goal_delta({}, second)
    assert contract.allergies == ['牛奶', '鸡蛋', '虾']
    assert contract.food_exclusions == ['牛肉', '猪肉', '羊肉']
    empty_delta = goal_delta({'allergies': [], 'food_exclusions': []}, second)
    assert empty_delta.allergies == contract.allergies
    assert empty_delta.food_exclusions == contract.food_exclusions
    effective = profile_inputs(db.get(Profile, user.id), contract)
    avoid, _ = resolve_tags(effective.allergies + effective.diet_preferences)
    foods = {ingredient_key(f['name']): f for f in PLANNING_FOODS}
    assert all(not (foods[ingredient_key(i['name'])]['tags'] & avoid)
               for meal in second.payload['meals'] for i in meal['items'])
    # Explicit clear + remaining entries removes just the requested draft limits.
    third = preview(db, user, {'clear_fields': ['allergies', 'food_exclusions'],
                              'allergies': ['虾'], 'food_exclusions': ['羊肉']}, second)
    saved = third.payload['meta']['goal_contract']
    assert saved['allergies'] == ['虾']
    assert saved['food_exclusions'] == ['羊肉']
    fourth = preview(db, user, {'clear_fields': ['allergies', 'food_exclusions']}, third)
    assert fourth.payload['meta']['goal_contract']['allergies'] is None
    assert fourth.payload['meta']['goal_contract']['food_exclusions'] is None
    assert business_state(db, user, [str(DAY)]) == before
    # Clearing draft restrictions cannot clear permanent profile restrictions.
    profile = db.get(Profile, user.id)
    profile.allergies = ['花生']
    assert profile_inputs(profile, goal_delta({}, fourth)).allergies == ['花生']
    db.rollback()


def test_goal_change_keeps_exact_substituted_meals_without_reapplying(initial_db):
    from app.domain.coach import confirm_draft
    from app.models import MealPlan
    db, user = initial_db
    before = business_state(db, user, [str(DAY)])
    first = preview(db, user, {})
    second = preview(db, user, {'meal_substitutions': [{'source': '鸡胸肉', 'target': '牛肉', 'count': 2}]}, first)
    substitutions = copy.deepcopy(second.payload['meta']['goal_contract']['meal_substitutions'])
    selected = {tuple(identity) for s in substitutions for identity in s['selected_meals']}
    assert len(selected) == 2
    assert {key for key, meal in meal_map(first).items() if meal != meal_map(second)[key]} == selected
    third = preview(db, user, {'goal': 'health'}, second)
    fourth = preview(db, user, {}, third)
    for row in (third, fourth):
        assert row.payload['meta']['goal_contract']['meal_substitutions'] == substitutions
        assert all(meal_map(row)[key] == meal_map(second)[key] for key in selected)
    assert fourth.payload['meals'] == third.payload['meals']
    assert business_state(db, user, [str(DAY)]) == before
    plan, version = confirm_draft(db, user, fourth.id, commit=False)
    assert plan.status == 'active'
    saved = {(m.day_index, m.slot): m for m in db.scalars(select(MealPlan).where(MealPlan.plan_version_id == version.id))}
    assert all(saved[key].items == meal_map(fourth)[key]['items'] for key in selected)


def test_chained_substitution_updates_only_overlapping_identities(initial_db):
    db, user = initial_db
    first = preview(db, user, {})
    second = preview(db, user, {'meal_substitutions': [{'source': '鸡胸肉', 'target': '牛肉', 'count': 2}]}, first)
    third = preview(db, user, {'meal_substitutions': [{'source': '牛肉', 'target': '豆腐', 'count': 1}]}, second)
    effects = third.payload['meta']['goal_contract']['meal_substitutions']
    assert len(effects) == 2
    assert sum(s['count'] for s in effects) == 2
    assert len({tuple(identity) for s in effects for identity in s['selected_meals']}) == 2
    fourth = preview(db, user, {'goal': 'health'}, third)
    assert all(meal_map(fourth)[tuple(identity)] == meal_map(third)[tuple(identity)]
               for s in effects for identity in s['selected_meals'])


def test_substitution_conflict_fails_and_explicit_withdrawal_is_possible(initial_db):
    from app.domain.patches import PatchValidationError
    db, user = initial_db
    first = preview(db, user, {})
    second = preview(db, user, {'meal_substitutions': [{'source': '鸡胸肉', 'target': '牛肉', 'count': 2}]}, first)
    before = business_state(db, user, [str(DAY)])
    with pytest.raises(PatchValidationError, match='冲突'):
        preview(db, user, {'food_exclusions': ['牛肉']}, second)
    assert second.status == 'ready'
    assert business_state(db, user, [str(DAY)]) == before
    third = preview(db, user, {'clear_fields': ['meal_substitutions'], 'food_exclusions': ['牛肉']}, second)
    assert third.payload['meta']['goal_contract']['meal_substitutions'] == []
    assert all(ingredient_key(i['name']) != '牛肉' for m in third.payload['meals'] for i in m['items'])
    assert business_state(db, user, [str(DAY)]) == before


def test_independent_ingredient_effects_on_same_meal_both_survive_goal_change(initial_db):
    db, user = initial_db
    first = preview(db, user, {})
    second = preview(db, user, {'meal_substitutions': [{'source': '鸡胸肉', 'target': '牛肉', 'count': 2}]}, first)
    previous = copy.deepcopy(second.payload['meta']['goal_contract']['meal_substitutions'])
    third = preview(db, user, {'meal_substitutions': [{'source': '米饭', 'target': '红薯', 'count': 2}]}, second)
    effects = third.payload['meta']['goal_contract']['meal_substitutions']
    assert effects[0] == previous[0]
    assert set(map(tuple, effects[0]['selected_meals'])) & set(map(tuple, effects[1]['selected_meals']))
    fourth = preview(db, user, {'goal': 'health'}, third)
    assert fourth.payload['meta']['goal_contract']['meal_substitutions'] == effects
    assert all(meal_map(fourth)[tuple(identity)] == meal_map(third)[tuple(identity)]
               for s in effects for identity in s['selected_meals'])
