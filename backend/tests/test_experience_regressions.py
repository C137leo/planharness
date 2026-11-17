"""Regressions from the actual office-worker journey; temporary DB, no live MCP."""
import json
from sqlalchemy import select
from tests.test_semantic_binding import env, request, task, Scripted, state, command
from app.models import CoachTaskExecution, Profile, User
from app.seed import DEMO_USER_ID


class Authorize(Scripted):
    def __init__(self, responses, approvals):
        super().__init__(responses)
        self.approvals = iter(approvals)

    def complete(self, messages, **kwargs):
        data = json.loads(messages[-1]['content'])
        if 'saved_tasks' in data:
            approved = next(self.approvals)
            return {'message': {'content': json.dumps({'approved_task_ids': approved,
                'new_request_text': '', 'modified_task_ids': []})}}
        return super().complete(messages, **kwargs)


def profile_preview(d):
    profile = next(o for o in d['snapshot']['objects'] if o['type'] == 'profile')
    return {'snapshot_id': d['snapshot']['snapshot_id'], 'tasks': [
        task(profile['id'], 'profile.update', session_minutes=30).model_dump()]}


def test_explicit_hold_does_not_write_even_when_controller_confirms(env):
    with env.factory() as db:
        db.get(Profile, DEMO_USER_ID).session_minutes = 25
        db.commit()
    client = Authorize([profile_preview,
        lambda d: {'action': 'confirm', 'pending_id': d['pending']['pending_id'], 'version': d['pending']['version'], 'execute_revision': False},
        lambda d: {'action': 'confirm', 'pending_id': d['pending']['pending_id'], 'version': d['pending']['version'], 'execute_revision': False}], [[], ['t1']])
    request(env, '先只预览把长期训练时长恢复三十分钟，暂时不要执行。', client)
    before = state(env)
    held = request(env, '范围只限每次训练时长，仍然不要保存，先让我核对。', client)
    assert held['overall_status'] == 'confirmation_required'
    assert state(env) == before
    with env.factory() as db:
        assert db.scalar(select(CoachTaskExecution)) is None
    done = request(env, '确认用修改档案的办法恢复每次三十分钟，只改时长。', client)
    assert done['overall_status'] == 'success', done
    assert state(env)['profile']['session_minutes'] == 30


def test_first_plan_is_preview_then_confirmed_for_a_fresh_user(env, monkeypatch):
    from app.agent import coach_agent
    from app.agent.planning_agent import PlanningAgent
    from app.models import Plan, PlanDraft, MealPlan, PlanVersion, new_id
    from app.evals.dialogue_runner import DAY
    # Real local planning/constraint gate, no model/network for the proposal itself.
    monkeypatch.setattr(PlanningAgent, 'propose', lambda self, profile, instruction, **kw:
                        self.propose_deterministic(profile, instruction, memory_context=kw.get('memory_context')))
    uid = new_id()
    with env.factory() as db:
        db.add(User(id=uid, nickname='fresh-office-worker'))
        db.flush()
        db.add(Profile(user_id=uid, birth_year=1999, height_cm=175, weight_kg=78,
            weekly_days=3, session_minutes=30, goal='fat_loss', equipment=['dumbbell', 'band', 'mat']))
        db.commit()
    def create(d):
        profile = next(o for o in d['snapshot']['objects'] if o['type'] == 'profile')
        assert any(c['capability_id'] == 'plan.create_initial_draft' for c in d['capabilities'])
        return {'snapshot_id': d['snapshot']['snapshot_id'], 'tasks': [task(profile['id'],
            'plan.create_initial_draft', instruction='根据档案生成首份计划，先预览，不改档案').model_dump()]}
    with env.factory() as db:
        first = coach_agent.run(db, db.get(User, uid), '根据档案生成首份训练和饮食计划，先预览', day=DAY,
            thread='fresh-user', client=Scripted([create]))
        assert first['overall_status'] == 'confirmation_required', first
        assert first['plan_draft']['days'] and first['plan_draft']['meals']
        assert db.scalar(select(Plan).where(Plan.user_id == uid)) is None
        assert db.get(Profile, uid).goal == 'fat_loss'
        p = first['pending_confirmation']
    with env.factory() as db:
        result = coach_agent.run(db, db.get(User, uid), '确认', day=DAY, thread='fresh-user',
            confirmation={'action': 'confirm', 'pending_id': p['pending_id'], 'version': p['version']})
        assert result['overall_status'] == 'success', result
        plan = db.scalar(select(Plan).where(Plan.user_id == uid))
        assert plan and plan.current_version == 1
        version = db.scalar(select(PlanVersion).where(PlanVersion.plan_id == plan.id))
        assert db.scalar(select(MealPlan).where(MealPlan.plan_version_id == version.id))
        assert db.get(PlanDraft, first['plan_draft']['draft_id']).status == 'confirmed'
    with env.factory() as db:
        replay = coach_agent.run(db, db.get(User, uid), '确认', day=DAY, thread='fresh-user',
            confirmation={'action': 'confirm', 'pending_id': p['pending_id'], 'version': p['version']})
        assert replay['confirmation_replayed']
        assert db.scalar(select(Plan).where(Plan.user_id == uid)).current_version == 1


def test_fact_snapshot_distinguishes_checkin_and_manual_log_from_profile(env):
    from app.agent.business_snapshot import build_snapshot
    from app.models import Checkin, WorkoutLog
    from app.evals.dialogue_runner import DAY
    with env.factory() as db:
        db.add(Checkin(user_id=DEMO_USER_ID, date=DAY, weight_kg=77.8, sleep_hours=6.5))
        db.add(WorkoutLog(user_id=DEMO_USER_ID, date=DAY, action_ref='brisk_walk', kind='cardio', duration_min=20))
        db.commit()
        snap = build_snapshot(db, db.get(User, DEMO_USER_ID), env.thread, [str(DAY)])
        day = next(o for o in snap.payload['objects'] if o['type'] == 'day')
        assert day['recorded_facts']['checkins'][0]['weight_kg'] == 77.8
        assert day['recorded_facts']['workout_logs'][0]['duration_min'] == 20
        profile = next(o for o in snap.payload['objects'] if o['type'] == 'profile')
        assert profile['weight_kg'] != 77.8
        assert 'goal_note' in profile


def test_profile_update_syncs_canonical_memory_and_can_be_restored(env):
    from app.models import Memory
    from app.domain.profile_sync import sync_profile_change
    from app.evals.dialogue_runner import DAY
    with env.factory() as db:
        profile = db.get(Profile, DEMO_USER_ID)
        profile.session_minutes = 25
        sync_profile_change(db, db.get(User, DEMO_USER_ID), profile, ['session_minutes'])
        db.commit()
    preview = request(env, '先预览恢复30', Scripted([profile_preview]))
    written = request(env, '确认', None, confirmation=command(preview['pending_confirmation']))
    assert written['overall_status'] == 'success', written
    with env.factory() as db:
        assert [r.value for r in db.scalars(select(Memory).where(Memory.user_id == DEMO_USER_ID,
            Memory.category == 'session_minutes', Memory.status == 'active'))] == ['每次 30 分钟']
    def undo(d):
        execution = next(o for o in d['snapshot']['objects'] if o['type'] == 'execution' and o['operation'] == 'profile.update')
        assert execution['undo_supported']
        return {'snapshot_id': d['snapshot']['snapshot_id'], 'tasks': [task(execution['id'], 'history.undo').model_dump()]}
    pending = request(env, '先预览撤销上次时长修改', Scripted([undo]))
    restored = request(env, '确认', None, confirmation=command(pending['pending_confirmation']))
    assert restored['overall_status'] == 'success', restored
    assert state(env)['profile']['session_minutes'] == 25
    with env.factory() as db:
        assert [r.value for r in db.scalars(select(Memory).where(Memory.user_id == DEMO_USER_ID,
            Memory.category == 'session_minutes', Memory.status == 'active'))] == ['每次 25 分钟']


def test_request_result_is_owner_scoped_and_never_reexecutes(env):
    from app.agent import coach_agent
    from app.evals.dialogue_runner import DAY
    from app.models import AgentRun, new_id
    from uuid import NAMESPACE_URL, uuid5
    with env.factory() as db:
        result = coach_agent.run(db, db.get(User, DEMO_USER_ID), '预览改动', request_id='office-timeout',
            day=DAY, thread=env.thread, client=Scripted([profile_preview]))
        claim = db.get(AgentRun, str(uuid5(NAMESPACE_URL, f'coach:{DEMO_USER_ID}:office-timeout')))
        assert claim.status == 'succeeded'
    recovered = env.http.get('/api/v1/agent/requests/office-timeout', headers=env.headers)
    assert recovered.status_code == 200
    assert recovered.json()['data']['result']['pending_confirmation'] == result['pending_confirmation']
    with env.factory() as db:
        claim = db.get(AgentRun, str(uuid5(NAMESPACE_URL, f'coach:{DEMO_USER_ID}:running')))
        db.add(AgentRun(id=str(uuid5(NAMESPACE_URL, f'coach:{DEMO_USER_ID}:running')), user_id=DEMO_USER_ID,
            kind='coach_request', mode='agent', status='running', input={'request_id': 'running'}, output={}))
        other = new_id()
        db.add(User(id=other, nickname='other'))
        db.flush()
        db.add(AgentRun(id=str(uuid5(NAMESPACE_URL, f'coach:{other}:foreign')), user_id=other,
            kind='coach_request', mode='agent', status='succeeded', input={}, output={'secret': 'never-return'}))
        db.commit()
    assert env.http.get('/api/v1/agent/requests/foreign', headers=env.headers).status_code == 404
    assert env.http.get('/api/v1/agent/requests/running', headers=env.headers).json()['data'] == {'request_id': 'running', 'status': 'running', 'thread':'coach', 'result': None}
    assert env.http.get('/api/v1/agent/requests/office-timeout').status_code == 401
    assert state(env)['profile']['session_minutes'] != 30  # Only a preview, not dispatched by recovery.


def test_model_budget_expires_across_calls_and_resets(monkeypatch):
    from app.llm import deadline
    clock = [0.0]
    monkeypatch.setattr(deadline.time, 'monotonic', lambda: clock[0])
    with deadline.model_budget(20):
        assert deadline.remaining_timeout(30) == 20
        clock[0] = 19
        assert deadline.remaining_timeout(30) == 1
        clock[0] = 21
        import pytest
        with pytest.raises(TimeoutError):
            deadline.remaining_timeout()
    assert deadline.remaining_timeout(30) == 30


from tests.test_commerce_chat import chat, prepare_chat, make_plan


def test_read_only_switch_never_carries_checkout_purchase_even_if_controller_keeps_it(chat):
    env, model, fake, post = chat
    preview = prepare_chat(chat)
    checkout_task_id = preview['pending_confirmation']['tasks'][0]['task_id']
    model.responses.extend([
        lambda d: {'action': 'new_request', 'pending_id': d['pending']['pending_id'], 'version': d['pending']['version'],
            'new_request_text': '只读核实今天快走是否重复保存', 'execute_revision': False,
            'keep_task_ids': [checkout_task_id]},
        lambda d: make_plan(d, 'context.inspect', 'day')])
    result = post('今天快走二十分钟有没有重复记录？别创建订单。')
    assert result['overall_status'] == 'success', result
    assert fake.submits == 0
    assert not result.get('pending_confirmation')
    assert [t['capability_id'] for t in result['task_plan']] == ['context.inspect']


def test_order_status_query_does_not_reoffer_purchase(chat):
    env, model, fake, post = chat
    preview = prepare_chat(chat)
    result = post('查看订单状态', checkout_action={'action': 'query',
        'checkout_id': preview['checkout']['id'], 'version': preview['checkout']['version']})
    assert '尚未提交订单' in result['reply']
    assert '本次将创建' not in result['reply']
    assert not result.get('pending_confirmation')
    assert fake.submits == 0


def test_three_consecutive_handoffs_use_unique_task_ids(env):
    from tests.test_semantic_binding import binding_plan
    request(env, '先选择查询目标', Scripted([binding_plan(['深蹲'], status='candidate', capability='context.inspect')]))
    before = state(env)
    def handoff(d):
        return {'action': 'new_request', 'pending_id': d['pending']['pending_id'], 'version': d['pending']['version'],
            'keep_task_ids': [t['task_id'] for t in d['pending']['tasks']], 'execute_revision': False,
            'new_request_text': '只读查看当前安排'}
    def inspect(d):
        day = next(o for o in d['snapshot']['objects'] if o['type'] == 'day')
        return {'snapshot_id': d['snapshot']['snapshot_id'], 'tasks': [task(day['id'], 'context.inspect').model_dump()]}
    client = Scripted([handoff, inspect, handoff, inspect, handoff, inspect])
    for count in range(2, 5):
        value = request(env, '保留待选择的查询，再查看当前安排', client)
        assert value['overall_status'] == 'confirmation_required', value
        ids = [t['task_id'] for t in value['task_plan']]
        assert len(ids) == len(set(ids)) == count
        assert state(env) == before


import pytest

@pytest.mark.parametrize('kind,days', [('long_term', 7), ('short_term', 1)])
def test_note_layer_and_local_day_expiry_are_persisted(env, kind, days):
    from app.models import Memory
    from app.domain.timeutil import as_utc, user_today, day_start_utc
    import datetime as dt
    def note(d):
        profile = next(o for o in d['snapshot']['objects'] if o['type'] == 'profile')
        return {'snapshot_id': d['snapshot']['snapshot_id'], 'tasks': [task(profile['id'], 'memory.create',
            category='note', value='期限回归备注', kind=kind, retention_days=days).model_dump()]}
    preview = request(env, '先预览备注', Scripted([note]))
    saved = request(env, '确认', None, confirmation=command(preview['pending_confirmation']))
    assert saved['overall_status'] == 'success', saved
    with env.factory() as db:
        note = db.scalar(select(Memory).where(Memory.user_id == DEMO_USER_ID, Memory.value == '期限回归备注'))
        assert note.kind == kind
        if kind == 'long_term':
            assert note.expires_at is None
        else:
            profile = db.get(Profile, DEMO_USER_ID)
            assert as_utc(note.expires_at) == day_start_utc(user_today(profile) + dt.timedelta(days=days), profile)


def test_failed_authorization_review_keeps_pending_without_write(env, monkeypatch):
    from app.agent import semantic_planner
    client = Scripted([profile_preview, lambda d: {'action': 'confirm', 'pending_id': d['pending']['pending_id'],
                       'version': d['pending']['version'], 'execute_revision': False}])
    request(env, '先看预览', client)
    before = state(env)
    monkeypatch.setattr(semantic_planner, 'review_mixed_authorization', lambda *a: (_ for _ in ()).throw(TimeoutError('review timeout')))
    result = request(env, '确认', client)
    assert result['overall_status'] == 'clarification_required'
    assert state(env) == before


def test_profile_change_invalidates_an_initial_plan_even_on_legacy_confirmation_api(env, monkeypatch):
    from app.domain import coach
    from app.agent.initial_plan_runtime import profile_revision
    from app.agent.planning_agent import PlanningAgent
    from app.models import PlanDraft, new_id
    from app.domain.patches import PatchValidationError
    monkeypatch.setattr(PlanningAgent, 'propose', lambda self, profile, instruction, **kw:
                        self.propose_deterministic(profile, instruction, memory_context=kw.get('memory_context')))
    uid = new_id()
    with env.factory() as db:
        user = User(id=uid, nickname='stale-first-preview')
        db.add(user); db.flush()
        db.add(Profile(user_id=uid, height_cm=175, weight_kg=78, birth_year=1999, weekly_days=3,
            session_minutes=30, equipment=['dumbbell', 'band', 'mat']))
        db.flush()
        draft = coach.create_draft(db, user, '只预览，不改档案', commit=False)
        draft.payload = {**draft.payload, 'meta': {**draft.payload.get('meta', {}), 'initial_profile_revision': profile_revision(db, user)}}
        db.commit()
        db.get(Profile, uid).weight_kg = 77
        db.commit()
        with pytest.raises(PatchValidationError):
            coach.confirm_draft(db, user, draft.id)
        assert db.get(PlanDraft, draft.id).status == 'ready'


def test_first_plan_confirmation_failure_rolls_back_plan_and_closes_pending(env, monkeypatch):
    from app.agent import coach_agent
    from app.agent.initial_plan_runtime import draft_revision, profile_revision
    from app.agent.business_snapshot import build_snapshot
    from app.agent.semantic_runtime import make_pending
    from app.domain import coach
    from app.agent.planning_agent import PlanningAgent
    from app.models import Plan, PlanDraft, CoachPendingPlan, new_id
    from app.evals.dialogue_runner import DAY
    monkeypatch.setattr(PlanningAgent, 'propose', lambda self, profile, instruction, **kw:
                        self.propose_deterministic(profile, instruction, memory_context=kw.get('memory_context')))
    uid = new_id()
    with env.factory() as db:
        user = User(id=uid, nickname='rollback-initial')
        db.add(user); db.flush()
        db.add(Profile(user_id=uid, height_cm=175, weight_kg=78, birth_year=1999,
                       weekly_days=3, session_minutes=30, equipment=['dumbbell', 'band', 'mat']))
        db.flush()
        draft = coach.create_draft(db, user, '先预览', commit=False)
        draft.payload = {**draft.payload, 'meta': {**draft.payload.get('meta', {}), 'initial_profile_revision': profile_revision(db, user)}}
        db.flush()
        snapshot = build_snapshot(db, user, 'initial-rollback', [str(DAY)])
        apply = task(draft.id, 'plan.confirm_initial_draft', expected_revision=draft_revision(draft))
        pending = make_pending(db, user, 'initial-rollback', snapshot, [apply], '')
        pid = pending.id; did = draft.id
        db.commit()
    real = coach.confirm_draft
    def broken(db, user, ident, **kwargs):
        real(db, user, ident, **kwargs)
        db.get(Profile, user.id).weight_kg = 99  # Inject an illegal side effect after the commit stage.
        db.flush()
        raise ValueError('injected failure')
    monkeypatch.setattr(coach, 'confirm_draft', broken)
    with env.factory() as db:
        result = coach_agent.run(db, db.get(User, uid), '确认', day=DAY, thread='initial-rollback',
            confirmation={'action':'confirm','pending_id':pid,'version':1})
        assert result['overall_status'] == 'failed', result
        assert db.scalar(select(Plan).where(Plan.user_id == uid)) is None
        assert db.get(Profile, uid).weight_kg == 78
        assert db.get(PlanDraft, did).status == 'ready'
        assert db.get(CoachPendingPlan, pid).status == 'failed'


def test_hold_checkout_cannot_describe_purchase_confirmation_as_preserving_a_preview(chat):
    env, model, fake, post = chat
    prepare_chat(chat)
    model.responses.append(lambda d: {'action':'clarify', 'pending_id':d['pending']['pending_id'],
        'version':d['pending']['version'], 'execute_revision':False,
        'question':'确认保留预览？确认后不会创建订单。'})
    held = post('只保留预览，不创建订单')
    assert '保留预览无需确认' in held['reply']
    assert next(a for a in held['actions'] if a['type']=='confirm_tasks')['label']=='确认创建待支付订单'
    assert fake.submits == 0


@pytest.fixture
def fresh_draft_api(env, monkeypatch):
    from app.api.routes import router
    from app.auth import issue_token
    from app.agent.planning_agent import PlanningAgent
    from app.models import new_id
    env.app.include_router(router)
    monkeypatch.setattr('app.llm.guard.is_llm_paused', lambda *a, **kw: False)
    monkeypatch.setattr(PlanningAgent, 'propose', lambda self, profile, instruction, **kw:
        self.propose_deterministic(profile, instruction, memory_context=kw.get('memory_context')))
    uid = new_id()
    with env.factory() as db:
        user = User(id=uid, nickname='remaining-first-plan')
        db.add(user); db.flush()
        db.add(Profile(user_id=uid, birth_year=1999, height_cm=175, weight_kg=78,
            goal='fat_loss', weekly_days=3, session_minutes=30, equipment=['dumbbell','band','mat']))
        db.commit()
        headers = {'Authorization': 'Bearer ' + issue_token(user, env.app.state.settings)}
    return env, uid, headers


def test_direct_draft_preview_uses_requested_constraints_without_saving_profile(fresh_draft_api):
    env, uid, headers = fresh_draft_api
    result = env.http.post('/api/v1/plans/drafts', headers=headers,
        json={'instruction':'只预览增肌计划，海鲜过敏，不吃红薯，不保存档案'})
    assert result.status_code == 200, result.text
    draft = result.json()['data']
    assert draft['status'] == 'ready'
    assert draft['program']['goal'] == '增肌'
    with env.factory() as db:
        profile = db.get(Profile, uid)
        assert profile.goal == 'fat_loss'
        assert profile.allergies == [] and profile.diet_preferences == []
    assert draft['meta'].get('initial_profile_revision')
    assert not {'虾','三文鱼','红薯'} & {item['name'] for meal in draft['meals'] for item in meal['items']}


def test_feedback_api_keeps_initial_fingerprint_and_does_not_write_profile(fresh_draft_api):
    env, uid, headers = fresh_draft_api
    first = env.http.post('/api/v1/plans/drafts', headers=headers,
                         json={'instruction':'先预览训练和饮食'}).json()['data']
    edited = env.http.post('/api/v1/plans/drafts/'+first['draft_id']+'/feedback', headers=headers,
        json={'text':'只预览增肌方案，海鲜过敏，不吃红薯，不改档案'})
    assert edited.status_code == 200, edited.text
    child = edited.json()['data']
    with env.factory() as db:
        profile = db.get(Profile, uid)
        assert profile.goal == 'fat_loss' and profile.allergies == [] and profile.diet_preferences == []
        profile.weight_kg = 77
        db.commit()
    assert child['meta']['initial_profile_revision']
    assert env.http.post('/api/v1/plans/drafts/'+child['draft_id']+'/confirm', headers=headers,
                         json={'comment':'旧档案预览'}).status_code == 422


def test_failed_initial_regeneration_retains_the_last_ready_preview(fresh_draft_api, monkeypatch):
    from app.agent.planning_agent import PlanningAgent, PlanProposal
    from app.agent.business_snapshot import build_snapshot
    from app.agent.initial_plan_runtime import draft_revision
    from app.agent.semantic_runtime import execute_plan
    from app.models import PlanDraft
    from app.evals.dialogue_runner import DAY
    env, uid, headers = fresh_draft_api
    first = env.http.post('/api/v1/plans/drafts', headers=headers,
                         json={'instruction':'根据已保存资料先预览'}).json()['data']
    with env.factory() as db:
        old_etag = draft_revision(db.get(PlanDraft, first['draft_id']))
    monkeypatch.setattr(PlanningAgent, 'propose', lambda *a, **kw: PlanProposal(
        status='needs_input', payload={'status':'needs_input','question':'希望调整哪部分？','days':[],'meals':[]}))
    with env.factory() as db:
        user = db.get(User, uid)
        snapshot = build_snapshot(db, user, 'retained-first', [str(DAY)])
        value = execute_plan(db, user, snapshot, [task(uid, 'plan.create_initial_draft', instruction='先问我一个问题')])
        db.commit()
        old = db.get(PlanDraft, first['draft_id'])
        assert old.status == 'ready' and draft_revision(old) == old_etag
        assert value['overall_status'] == 'clarification_required'
        assert '保留' in value['reply']
    assert env.http.post('/api/v1/plans/drafts/'+first['draft_id']+'/confirm', headers=headers,
                         json={'comment':'继续使用原预览'}).status_code == 200


def test_canonical_note_cannot_be_archived_independently_of_profile(env):
    from app.domain.profile_sync import sync_profile_change
    from app.models import Memory
    with env.factory() as db:
        profile = db.get(Profile, DEMO_USER_ID)
        profile.preferred_time_window = '19:00-20:00'
        profile.preferred_time_label = '晚上'
        sync_profile_change(db, db.get(User, DEMO_USER_ID), profile, ['preferred_time_window'])
        db.commit()
    def archive(d):
        memory = next(o for o in d['snapshot']['objects'] if o['type']=='memory' and o['category']=='schedule')
        return {'snapshot_id':d['snapshot']['snapshot_id'], 'tasks':[task(memory['id'], 'memory.archive').model_dump()]}
    before = state(env)
    value = request(env, '取消长期晚上训练偏好，先让我核对', Scripted([archive]))
    assert value['overall_status'] == 'clarification_required'
    assert not value.get('pending_confirmation')
    assert state(env) == before


def test_clearing_profile_time_also_clears_its_label_and_mirror(env):
    from app.domain.profile_sync import sync_profile_change
    from app.models import Memory
    with env.factory() as db:
        profile = db.get(Profile, DEMO_USER_ID)
        profile.preferred_time_window = '19:00-20:00'
        profile.preferred_time_label = '晚上'
        sync_profile_change(db, db.get(User, DEMO_USER_ID), profile, ['preferred_time_window'])
        db.commit()
    def clear(d):
        profile = next(o for o in d['snapshot']['objects'] if o['type']=='profile')
        return {'snapshot_id':d['snapshot']['snapshot_id'], 'tasks':[task(profile['id'], 'profile.update', preferred_time_window='').model_dump()]}
    value = request(env, '先预览取消长期训练时段', Scripted([clear]))
    saved = request(env, '确认', None, confirmation=command(value['pending_confirmation']))
    assert saved['overall_status']=='success', saved
    with env.factory() as db:
        profile = db.get(Profile, DEMO_USER_ID)
        assert profile.preferred_time_window == profile.preferred_time_label == ''
        assert not list(db.scalars(select(Memory).where(Memory.user_id==DEMO_USER_ID,
                            Memory.category=='schedule', Memory.status=='active')))


def test_profile_api_rolls_back_when_mirror_sync_fails(fresh_draft_api, monkeypatch):
    from app.domain.profile_sync import sync_profile_change
    from app.models import Memory
    env, uid, headers = fresh_draft_api
    def broken(*a, **kw):
        sync_profile_change(*a, **kw)
        raise ValueError('injected mirror failure')
    monkeypatch.setattr('app.domain.profile_sync.sync_profile_change', broken)
    response = env.http.put('/api/v1/me/profile', headers=headers, json={'session_minutes':45})
    assert response.status_code == 500, response.text
    with env.factory() as db:
        assert db.get(Profile, uid).session_minutes == 30
        assert not list(db.scalars(select(Memory).where(Memory.user_id == uid)))


def test_profile_api_clears_derived_time_label(fresh_draft_api):
    env, uid, headers = fresh_draft_api
    assert env.http.put('/api/v1/me/profile', headers=headers,
        json={'preferred_time_window':'19:00-20:00','preferred_time_label':'晚上'}).status_code == 200
    response = env.http.put('/api/v1/me/profile', headers=headers, json={'preferred_time_window':''})
    assert response.status_code == 200, response.text
    assert response.json()['data']['preferred_time_label'] == ''


def test_feedback_preserves_proposal_constraints_without_persisting_them(fresh_draft_api):
    env, uid, headers = fresh_draft_api
    first = env.http.post('/api/v1/plans/drafts', headers=headers,
        json={'instruction':'只预览增肌计划，海鲜过敏，不吃红薯'}).json()['data']
    child = env.http.post('/api/v1/plans/drafts/'+first['draft_id']+'/feedback', headers=headers,
        json={'text':'新增一个练背动作，其他保持'}).json()['data']
    assert child['status'] == 'ready' and child['program']['goal'] == '增肌'
    assert not {'虾','三文鱼','红薯'} & {item['name'] for meal in child['meals'] for item in meal['items']}
    with env.factory() as db:
        profile = db.get(Profile, uid)
        assert profile.goal == 'fat_loss' and profile.allergies == [] and profile.diet_preferences == []


def test_failed_feedback_keeps_previous_ready_draft(fresh_draft_api, monkeypatch):
    from app.agent.planning_agent import PlanningAgent, PlanProposal
    from app.models import PlanDraft
    env, uid, headers = fresh_draft_api
    first = env.http.post('/api/v1/plans/drafts', headers=headers,
        json={'instruction':'根据已保存档案先预览'}).json()['data']
    monkeypatch.setattr(PlanningAgent, 'propose', lambda *a, **kw: PlanProposal(
        status='needs_input', payload={'status':'needs_input','question':'希望调整哪部分？','days':[],'meals':[]}))
    failed = env.http.post('/api/v1/plans/drafts/'+first['draft_id']+'/feedback', headers=headers,
        json={'text':'先问我一个问题'}).json()['data']
    assert failed['status'] == 'needs_input'
    with env.factory() as db:
        assert db.get(PlanDraft, first['draft_id']).status == 'ready'


def test_undo_profile_time_restores_window_label_and_single_active_mirror(env):
    from app.domain.profile_sync import sync_profile_change
    from app.models import Memory
    with env.factory() as db:
        profile = db.get(Profile, DEMO_USER_ID)
        profile.preferred_time_window = '19:00-20:00'
        profile.preferred_time_label = '晚上'
        sync_profile_change(db, db.get(User, DEMO_USER_ID), profile, ['preferred_time_window'])
        db.commit()
    def change(d):
        p = next(o for o in d['snapshot']['objects'] if o['type']=='profile')
        return {'snapshot_id':d['snapshot']['snapshot_id'], 'tasks':[task(p['id'], 'profile.update', preferred_time_window='07:00-08:00').model_dump()]}
    preview = request(env, '先预览以后早上训练', Scripted([change]))
    saved = request(env, '确认', None, confirmation=command(preview['pending_confirmation']))
    assert saved['overall_status'] == 'success', saved
    assert state(env)['profile']['preferred_time_label'] == '早上'
    def undo(d):
        e = next(o for o in d['snapshot']['objects'] if o['type']=='execution' and o['operation']=='profile.update')
        return {'snapshot_id':d['snapshot']['snapshot_id'], 'tasks':[task(e['id'], 'history.undo').model_dump()]}
    preview = request(env, '先预览撤销刚才的时段修改', Scripted([undo]))
    restored = request(env, '确认', None, confirmation=command(preview['pending_confirmation']))
    assert restored['overall_status'] == 'success', restored
    with env.factory() as db:
        profile = db.get(Profile, DEMO_USER_ID)
        assert profile.preferred_time_window == '19:00-20:00' and profile.preferred_time_label == '晚上'
        active = list(db.scalars(select(Memory).where(Memory.user_id==DEMO_USER_ID,
            Memory.category=='schedule', Memory.status=='active')))
        assert len(active)==1 and active[0].structured['window']=='19:00-20:00'


def test_chat_meal_record_requires_confirmation_and_replay_does_not_duplicate(env):
    from app.evals.dialogue_runner import DAY
    def record(d):
        day = next(o for o in d['snapshot']['objects'] if o['type']=='day')
        return {'snapshot_id':d['snapshot']['snapshot_id'], 'tasks':[task(day['id'], 'nutrition.record_meal',
            slot='lunch', title='鸡胸肉米饭', kcal=600, protein_g=40, note='用户估算').model_dump()]}
    before = state(env)
    preview = request(env, '午餐实际吃了鸡胸肉米饭，估算600千卡40克蛋白，先核对再记录', Scripted([record]))
    assert preview['overall_status']=='confirmation_required', preview
    assert '600' in preview['reply'] and '40' in preview['reply']
    assert state(env)==before
    confirm = command(preview['pending_confirmation'])
    saved = request(env, '确认登记', None, confirmation=confirm, request_id='meal-confirm')
    again = request(env, '确认登记', None, confirmation=confirm, request_id='meal-confirm')
    assert saved==again and saved['overall_status']=='success', saved
    after=state(env)
    assert after['days']==before['days'] and after['profile']==before['profile']
    rows=after['facts'][str(DAY)]['meal_logs']
    assert len(rows)==1 and rows[0]['kcal']==600 and rows[0]['protein_g']==40
    with env.factory() as db:
        journal=db.scalar(select(CoachTaskExecution).where(CoachTaskExecution.operation=='nutrition.record_meal'))
        assert journal.resource_changes[0]['kind']=='meal_logs'


def test_chat_meal_correction_changes_only_the_selected_actual_field(env):
    from app.models import MealLog, new_id
    from app.evals.dialogue_runner import DAY
    with env.factory() as db:
        row=MealLog(id=new_id(),user_id=DEMO_USER_ID,date=DAY,slot='dinner',title='牛肉饭',kcal=700,protein_g=45,note='估算')
        db.add(row);db.commit();ident=row.id
    def correction(d):
        row=next(o for o in d['snapshot']['objects'] if o['type']=='meal_log' and o['id']==ident)
        return {'snapshot_id':d['snapshot']['snapshot_id'], 'tasks':[task(row['id'], 'nutrition.correct_meal', kcal=650).model_dump()]}
    before=state(env)
    preview=request(env,'晚餐那条记录热量改成650，其他不变，先核对',Scripted([correction]))
    saved=request(env,'确认',None,confirmation=command(preview['pending_confirmation']))
    assert saved['overall_status']=='success', saved
    after=state(env);expected=json.loads(json.dumps(before))
    expected['facts'][str(DAY)]['meal_logs'][0]['kcal']=650
    assert after==expected


def test_meal_record_unknown_nutrition_cannot_be_executed(env):
    from app.agent.business_snapshot import build_snapshot
    from app.agent.task_executor import execute_tasks
    from app.evals.dialogue_runner import DAY
    from app.agent.task_executor import TaskFailure
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);snapshot=build_snapshot(db,user,env.thread,[str(DAY)])
        day=next(o for o in snapshot.payload['objects'] if o['type']=='day')
        with pytest.raises(TaskFailure,match='进食'):
            execute_tasks(db,user,snapshot,[task(day['id'],'nutrition.record_meal',slot='lunch',title='鸡翅')],confirmed=True)
    assert not state(env)['facts'][str(DAY)]['meal_logs']


def test_completed_result_is_recoverable_if_process_fails_after_finish_commit(env, monkeypatch):
    from app.agent import coach_agent
    original = coach_agent._run
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise ConnectionError('response interrupted after commit')
    monkeypatch.setattr(coach_agent, '_run', interrupted)
    with pytest.raises(ConnectionError):
        request(env, '查询今天的安排', Scripted([lambda d: {'snapshot_id':d['snapshot']['snapshot_id'],
            'mode':'noop','answer':'已核对当前安排','tasks':[]}]), request_id='finish-crash')
    from app.models import AgentRun
    with env.factory() as db:
        claim=db.scalar(select(AgentRun).where(AgentRun.kind=='coach_request'))
        assert claim.status=='succeeded' and claim.output['reply']=='已核对当前安排'
        assert claim.output['run_id']
    response=env.http.get('/api/v1/agent/requests/finish-crash',headers=env.headers)
    assert response.status_code==200 and response.json()['data']['status']=='succeeded'


def test_old_running_claim_is_unknown_and_never_reexecuted_by_query(env):
    import datetime as dt
    from app.models import AgentRun
    from uuid import uuid5, NAMESPACE_URL
    ident=str(uuid5(NAMESPACE_URL,f'coach:{DEMO_USER_ID}:old-claim'))
    with env.factory() as db:
        db.add(AgentRun(id=ident,user_id=DEMO_USER_ID,kind='coach_request',mode='agent',status='running',
            input={'request_id':'old-claim','thread':env.thread}, created_at=dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=10)))
        db.commit()
    before=state(env)
    response=env.http.get('/api/v1/agent/requests/old-claim',headers=env.headers)
    assert response.status_code==200
    assert response.json()['data']['status']=='needs_reconciliation' and response.json()['data']['result'] is None
    assert state(env)==before
    with env.factory() as db:
        assert db.get(AgentRun,ident).status=='running'


@pytest.mark.parametrize('fail_review', [False, True])
@pytest.mark.parametrize('field', ['answer', 'question'])
def test_unsupported_allergen_safety_promise_is_withheld_even_after_planner_claims_success(env, fail_review, field):
    class UnsafeReply(Scripted):
        def complete(self, messages, **kwargs):
            data=json.loads(messages[-1]['content'])
            if 'reply_evidence_input' in data:
                if fail_review: raise TimeoutError('review unavailable')
                return {'message':{'content':json.dumps({'unsupported_safety_claim':True})}}
            return super().complete(messages,**kwargs)
    before=state(env)
    result=request(env,'记住海鲜过敏就能保证任何外食都不会过敏吗？',UnsafeReply([lambda d:{
        'snapshot_id':d['snapshot']['snapshot_id'],'mode':'noop',field:'放心，任何外食都保证不会过敏。','tasks':[]}]))
    assert '任何外食都保证不会过敏' not in result['reply']
    assert '不能保证' in result['reply'] and state(env)==before
    assert any(e['stage']=='reply_evidence_enforced' for e in result['trace'])


def test_initial_contract_keeps_three_actions_and_fifty_minutes_when_only_dinner_changes(fresh_draft_api):
    from app.agent import coach_agent
    from app.domain.nutrition_catalog import ingredient_key
    from app.evals.dialogue_runner import DAY
    from app.models import Plan, PlanDraft
    env, uid, headers=fresh_draft_api
    class Independent(Scripted):
        def __init__(self,responses,goals):super().__init__(responses);self.goals=iter(goals)
        def complete(self,messages,**kwargs):
            data=json.loads(messages[-1]['content'])
            if 'initial_goal_input' in data:
                return {'message':{'content':json.dumps(next(self.goals),ensure_ascii=False)}}
            return super().complete(messages,**kwargs)
    def proposal(d):
        prior=next((o for o in d['snapshot']['objects'] if o['type']=='initial_plan_draft'),None)
        target=prior or next(o for o in d['snapshot']['objects'] if o['type']=='profile')
        args={'instruction':'仍然排30分钟；晚餐牛肉150克' if prior else '先按档案生成'}
        if prior:args['expected_revision']=prior['etag']
        cap='plan.revise_initial_draft' if prior else 'plan.create_initial_draft'
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(target['id'],cap,**args).model_dump()]}
    def edit(d):
        p=d['pending']
        return {'action':'new_request','pending_id':p['pending_id'],'version':p['version'],
            'execute_revision':False,'new_request_text':'按用户最新要求修改草案','keep_task_ids':[],
            'edit_task_ids':[p['tasks'][0]['task_id']]}
    model=Independent([proposal,edit,proposal,edit,proposal],[
        {'formal_actions':3,'duration':{'kind':'target','minutes':30}},
        {'duration':{'kind':'target','minutes':50}},
        {'changed_meal_slots':['dinner'],'meal_rules':[{'slot':'dinner','ingredient_options':['牛肉'],'grams':150}]}])
    outputs=[]
    for text in ['每次30分钟，每个训练日3个正式动作，热身另计','改成练满50分钟，其他不变','只改晚餐吃牛肉150克，训练保持']:
        with env.factory() as db:
            value=coach_agent.run(db,db.get(User,uid),text,day=DAY,thread='initial-contract',client=model)
            assert value['overall_status']=='confirmation_required',value
            outputs.append(value)
            assert db.get(Profile,uid).session_minutes==30
            assert db.scalar(select(Plan).where(Plan.user_id==uid)) is None
    for n,value in enumerate(outputs):
        for day in value['plan_draft']['days']:
            assert len([a for a in day['actions'] if a['slot']!='warmup'])==3
            assert sum(a['duration_min'] for a in day['actions'])==(30 if n==0 else 50)
    assert [[a['action_ref'] for a in d['actions']] for d in outputs[0]['plan_draft']['days']] == [[a['action_ref'] for a in d['actions']] for d in outputs[1]['plan_draft']['days']]
    assert outputs[1]['plan_draft']['meals']==outputs[0]['plan_draft']['meals']
    assert outputs[2]['plan_draft']['days']==outputs[1]['plan_draft']['days']
    assert [m for m in outputs[2]['plan_draft']['meals'] if m['slot']!='dinner']==[m for m in outputs[1]['plan_draft']['meals'] if m['slot']!='dinner']
    for meal in outputs[-1]['plan_draft']['meals']:
        if meal['slot']=='dinner':
            assert sum(i['grams'] for i in meal['items'] if ingredient_key(i['name'])=='牛肉')==150
    assert outputs[-1]['plan_draft']['meta']['goal_contract']['duration']['minutes']==50
    confirmed=env.http.post('/api/v1/plans/drafts/'+outputs[-1]['plan_draft']['draft_id']+'/confirm',headers=headers,json={})
    assert confirmed.status_code==200,confirmed.text
    from app.models import PlanAction, PlanVersion
    with env.factory() as db:
        plan=db.scalar(select(Plan).where(Plan.user_id==uid))
        version=db.scalar(select(PlanVersion).where(PlanVersion.plan_id==plan.id))
        actions=list(db.scalars(select(PlanAction).where(PlanAction.plan_version_id==version.id)))
        assert plan.current_version==1
        for day_index in {a.day_index for a in actions}:
            day=[a for a in actions if a.day_index==day_index]
            assert len([a for a in day if a.slot!='warmup'])==3
            assert sum(a.duration_min for a in day)==50


def test_initial_typed_choices_apply_real_catalog_actions_and_reject_allergen_conflict(fresh_draft_api):
    from app.agent.initial_requirements import InitialRequirements
    from app.domain import coach
    from app.domain.patches import PatchValidationError
    env,uid,headers=fresh_draft_api
    goal=InitialRequirements(formal_actions=3,selected_action_refs=['goblet-squat','push-up','dead-bug'],
        duration={'kind':'target','minutes':50})
    with env.factory() as db:
        user=db.get(User,uid)
        row=coach.create_draft(db,user,'只按结构化条件生成',interpret_instruction=False,goal_contract=goal)
        for day in row.payload['days']:
            assert [a['action_ref'] for a in day['actions'] if a['slot']!='warmup']==['goblet-squat','push-up','dead-bug']
            assert sum(a['duration_min'] for a in day['actions'])==50
        db.get(Profile,uid).allergies=['海鲜'];db.commit()
        unsafe=InitialRequirements(formal_actions=3,meal_rules=[{'slot':'dinner','ingredient_options':['虾'],'grams':100}])
        with pytest.raises(PatchValidationError,match='过敏'):
            coach.create_draft(db,user,'晚餐虾100克',interpret_instruction=False,goal_contract=unsafe)
        assert db.get(type(row),row.id).status=='ready'


def test_failed_initial_edit_cannot_be_confirmed_as_the_old_goal(fresh_draft_api):
    from app.agent import coach_agent
    from app.agent.initial_plan_runtime import draft_revision
    from app.agent.business_snapshot import build_snapshot
    from app.agent.semantic_runtime import make_pending
    from app.models import PlanDraft, CoachPendingPlan, Plan
    from app.evals.dialogue_runner import DAY
    env,uid,headers=fresh_draft_api
    first=env.http.post('/api/v1/plans/drafts',headers=headers,json={'instruction':'先预览'}).json()['data']
    with env.factory() as db:
        user=db.get(User,uid);row=db.get(PlanDraft,first['draft_id'])
        snap=build_snapshot(db,user,'unmet-initial',[str(DAY)])
        pending=make_pending(db,user,'unmet-initial',snap,[task(row.id,'plan.confirm_initial_draft',expected_revision=draft_revision(row))],'确认后才启用')
        pid=pending.id;db.commit()
    class Infeasible(Scripted):
        def complete(self,messages,**kwargs):
            data=json.loads(messages[-1]['content'])
            if 'initial_goal_input' in data:
                return {'message':{'content':json.dumps({'formal_actions':3,'duration':{'kind':'target','minutes':5}})}}
            return super().complete(messages,**kwargs)
    def edit(d):
        p=d['pending']
        return {'action':'new_request','pending_id':p['pending_id'],'version':p['version'],'execute_revision':False,
            'new_request_text':'改为5分钟仍保留3动作','keep_task_ids':[],'edit_task_ids':[p['tasks'][0]['task_id']]}
    def revise(d):
        row=next(o for o in d['snapshot']['objects'] if o['type']=='initial_plan_draft')
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(row['id'],'plan.revise_initial_draft',
            instruction='5分钟3动作',expected_revision=row['etag']).model_dump()]}
    with env.factory() as db:
        failed=coach_agent.run(db,db.get(User,uid),'改为5分钟，3个正式动作和热身都保留',day=DAY,thread='unmet-initial',client=Infeasible([edit,revise]))
        assert failed['overall_status'] in {'failed','clarification_required'},failed
        saved=db.get(CoachPendingPlan,pid)
        assert saved.result['unresolved_initial_requirements']['duration']['minutes']==5
        blocked=coach_agent.run(db,db.get(User,uid),'确认',day=DAY,thread='unmet-initial',confirmation=command({'pending_id':pid,'version':saved.version}))
        assert '尚未满足' in blocked['reply'] and db.scalar(select(Plan).where(Plan.user_id==uid)) is None
        assert db.get(PlanDraft,first['draft_id']).status=='ready'


def test_meal_record_verification_rolls_back_unrelated_profile_write(env):
    from app.models import MealLog
    from app.agent.business_snapshot import build_snapshot
    from app.agent.task_executor import execute_tasks
    from app.evals.dialogue_runner import DAY
    before=state(env)
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);snapshot=build_snapshot(db,user,env.thread,[str(DAY)])
        day=next(o for o in snapshot.payload['objects'] if o['type']=='day')
        original=db.flush
        def injected(*a, **kw):
            if any(isinstance(row,MealLog) for row in db.new):
                db.get(Profile,DEMO_USER_ID).weight_kg=99
            original(*a, **kw)
        db.flush=injected
        result=execute_tasks(db,user,snapshot,[task(day['id'],'nutrition.record_meal',
            slot='lunch',title='午餐',kcal=600,protein_g=40)],confirmed=True)
        assert result[0]['status']=='failed' and result[0]['stage']=='verification'
        db.commit()
    assert state(env)==before


@pytest.mark.parametrize('withdraw', [False, True])
def test_unresolved_initial_requirement_is_not_discarded_by_a_new_meal_request(withdraw):
    from app.agent.initial_requirements import extract_goal
    class Delta:
        def available(self): return True
        def complete(self,*a,**kw):
            return {'message':{'content':json.dumps({'changed_meal_slots':['dinner'],
                'meal_rules':[{'slot':'dinner','ingredient_options':['牛肉']}],
                'discard_unsupported_requirements':withdraw})}}
    goal=extract_goal(Delta(),'晚餐吃牛肉',{'formal_actions':3,'unsupported_requirements':['每个动作精确5组']},{},[])
    assert goal.unsupported_requirements == ([] if withdraw else ['每个动作精确5组'])
