"""Stage 2: deterministic contracts and real isolated SQLite transactions."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest
from sqlalchemy import select
from app.db import Base, make_engine, make_session_factory
from app.models import User, ExternalOperation
from app.agent.capabilities import REGISTRY
from app.agent.execution_contract import TaskState, VerifiedOutcome
from app.domain import external_operations as ops


@pytest.fixture
def factory(tmp_path):
    engine=make_engine('sqlite:///'+str(tmp_path/'operations.db'));Base.metadata.create_all(engine)
    factory=make_session_factory(engine)
    with factory() as db:
        db.add(User(id='owner',nickname='owner'));db.add(User(id='other',nickname='other'));db.commit()
    yield factory
    engine.dispose()


def run(factory, **overrides):
    args=dict(user_id='owner',provider='test',capability_id='commerce.submit',request_key='business-key',
              payload={'cart':'same'},dispatch=lambda:'remote-1',verify=lambda id:(True,{'status':'pending_payment'}))
    args.update(overrides)
    return ops.execute(factory,**args)


def test_local_manifest_adds_meal_facts_and_declares_boundaries():
    assert len(REGISTRY)==16
    for cap in REGISTRY.values():
        data=cap.public()
        assert data['scope_schema']['ownership']=='authenticated_user'
        assert data['idempotency_policy'] and data['verification_policy']
        assert data['execution_boundary']=='local_database'


def test_success_requires_postcondition_evidence():
    with pytest.raises(ValueError):VerifiedOutcome(state=TaskState.SUCCEEDED,operation_id='x')
    with pytest.raises(ValueError):VerifiedOutcome(state=TaskState.UNKNOWN,operation_id='x',verified=True)


def test_claim_is_durable_before_dispatch_and_duplicate_is_not_sent(factory):
    called=[]
    def dispatch():
        with factory() as db:
            assert db.scalar(select(ExternalOperation)).status=='executing'
        called.append(True);return 'remote-1'
    a=run(factory,dispatch=dispatch);b=run(factory,dispatch=dispatch)
    assert a.state==b.state==TaskState.SUCCEEDED and len(called)==1
    assert a.operation_id==b.operation_id


@pytest.mark.parametrize('exception',[TimeoutError(),ConnectionError(),ValueError('bad provider response')])
def test_unknown_never_blindly_retried(factory,exception):
    called=[]
    def dispatch():called.append(True);raise exception
    a=run(factory,dispatch=dispatch);b=run(factory,dispatch=dispatch)
    assert a.state==b.state==TaskState.UNKNOWN and len(called)==1
    assert a.recovery


def test_definite_rejection_is_separate_from_unknown(factory):
    def reject():raise ops.DefiniteRejection()
    assert run(factory,dispatch=reject).state==TaskState.FAILED


def test_unverified_success_and_truthy_nonboolean_are_unknown(factory):
    assert run(factory,verify=lambda id:(False,{'success':True})).state==TaskState.UNKNOWN
    assert run(factory,request_key='second',verify=lambda id:('yes',{})).state==TaskState.UNKNOWN


def test_conflicting_business_key_and_foreign_inspection(factory):
    a=run(factory)
    with pytest.raises(ops.OperationConflict):run(factory,payload={'cart':'changed'})
    with pytest.raises(LookupError):ops.inspect_operation(factory,user_id='other',operation_id=a.operation_id)


def test_query_can_reconcile_without_resubmission(factory):
    a=run(factory,verify=lambda id:(False,{}))
    b=ops.reconcile(factory,user_id='owner',operation_id=a.operation_id,verify=lambda id:(True,{'status':'pending_payment'}))
    assert b.state==TaskState.SUCCEEDED and b.remote_id=='remote-1'


def test_concurrent_request_has_single_dispatch(factory):
    entered,release=Event(),Event();calls=[]
    def dispatch():
        calls.append(True);entered.set();assert release.wait(5);return 'remote-1'
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(run,factory,dispatch=dispatch)
        assert entered.wait(5)
        second=pool.submit(run,factory,dispatch=dispatch).result(timeout=5)
        assert second.state==TaskState.EXECUTING
        release.set();assert first.result(timeout=5).state==TaskState.SUCCEEDED
    assert len(calls)==1


def test_model_menu_preserves_legacy_contract_except_reviewed_memory_and_undo_changes():
    import json
    from pathlib import Path
    from app.agent.capabilities import manifest
    old = json.loads((Path(__file__).resolve().parent/'fixtures/semantic-manifest-before.json').read_text())
    current = {c['capability_id']: c for c in manifest()}
    assert set(current) - {c['capability_id'] for c in old} == {'nutrition.record_meal', 'nutrition.correct_meal'}
    assert {c['capability_id'] for c in old} <= set(current)
    assert current['nutrition.record_meal']['confirmation'] == current['nutrition.correct_meal']['confirmation'] == 'always'
    assert current['nutrition.record_meal']['target_types'] == ['day']
    assert current['nutrition.correct_meal']['target_types'] == ['meal_log']
    for previous in old:
        value = current[previous['capability_id']]
        if previous['capability_id'] not in {'memory.create', 'memory.archive', 'history.undo'}:
            assert value == previous
            continue
        assert {k:v for k,v in value.items() if k not in {'description','input_schema'}} == {
            k:v for k,v in previous.items() if k not in {'description','input_schema'}}
        if previous['capability_id'] in {'history.undo', 'memory.archive'}:
            assert value['input_schema'] == previous['input_schema']
            assert ('undo_supported' if previous['capability_id'] == 'history.undo' else 'profile.update') in value['description']
        else:
            before = previous['input_schema']; after = value['input_schema']
            assert after['required'] == before['required']
            assert all(after['properties'][key] == schema for key,schema in before['properties'].items())
            assert set(after['properties']) - set(before['properties']) == {'kind', 'retention_days'}
            assert after['properties']['kind']['enum'] == ['long_term', 'short_term']
            assert after['properties']['retention_days']['minimum'] == 1
