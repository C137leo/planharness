"""Actual database + continuous authenticated chat. Scripted model is not an NLU score."""
import copy
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select,text,inspect
from app.models import User,WorkoutSession,WorkoutLog,WorkoutRecordRevision
from app.seed import DEMO_USER_ID
from app.evals.dialogue_runner import DialogueEnvironment,DAY
from app.domain import workout,workout_records as records
from app.agent.workout_capabilities import FinishSession,CorrectRecord
from tests.test_semantic_binding import Scripted,task

@pytest.fixture
def training(tmp_path):
    model=Scripted([]);env=DialogueEnvironment(tmp_path/'state.db','workout-facts',model)
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);s=workout.start_session(db,user,DAY)
        for reps,weight in [(8,50),(9,55),(10,60)]:workout.complete_set(db,user,s.id,reps=reps,weight_kg=weight)
        sid=s.id
    def post(text,**fields):
        r=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':text,'thread':env.thread,'date':str(DAY),**fields})
        assert r.status_code==200,r.text
        return r.json()['data']
    yield env,model,sid,post
    env.close()


def finish_task(d):
    s=next(o for o in d['snapshot']['objects'] if o['type']=='session')
    return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(s['id'],'workout.finish_session',expected_revision=s['record_revision']).model_dump()]}


def confirm(post,result):
    p=result['pending_confirmation'];return post('按钮确认',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})


def test_continuous_finish_confirm_read_correct_confirm_and_repeated_confirmation(training):
    env,model,sid,post=training;before=env.snapshot()['days']
    model.responses.append(finish_task);r=post('今天到这里，没记的部分先留空，帮我结束训练')
    assert r['overall_status']=='confirmation_required' and '3/4组' in r['reply'],r
    first=copy.deepcopy(r);r=confirm(post,r)
    assert r['overall_status']=='success' and r['workout_records']['session']['status']=='finished',r
    logs=r['workout_records']['logs'];assert len(logs)==1 and logs[0]['sets']==3
    assert [s['weight_kg'] for s in logs[0]['details']['set_logs']]==[50,55,60]
    assert r['workout_records']['session']['actual_minutes'] is None and logs[0]['duration_min']==0
    assert sum(i['completed_sets'] for i in r['workout_records']['session']['items'])==3
    model.responses.append(lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(sid,'workout.read_records').model_dump()]})
    r=post('查刚才的真实记录');assert r['workout_records']['logs'][0]['sets']==3
    def correction(d):
        parent=next(o for o in d['snapshot']['objects'] if o['id']==sid)
        item=next(o for o in d['snapshot']['objects'] if o['type']=='session_item' and o.get('completed_sets')==3)
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(item['id'],'workout.correct_record',expected_revision=parent['record_revision'],set_no=3,weight_kg=62.5,reason='用户纠正最后一组重量').model_dump()]}
    model.responses.append(correction);r=post('最后一组其实是62.5公斤，其他别动');r=confirm(post,r)
    assert r['overall_status']=='success',r
    assert r['workout_records']['logs'][0]['details']['set_logs'][-1]['weight_kg']==62.5
    assert r['workout_records']['logs'][0]['sets']==3
    confirm(post,first)
    with env.factory() as db:
        assert len(list(db.scalars(select(WorkoutLog))))==1
        assert len(list(db.scalars(select(WorkoutRecordRevision))))==1
    assert env.snapshot()['days']==before


def test_stale_finish_confirmation_does_not_close_new_progress(training):
    env,model,sid,post=training;model.responses.append(finish_task);r=post('结束一下训练')
    with env.factory() as db:workout.complete_set(db,db.get(User,DEMO_USER_ID),sid,reps=8,weight_kg=60)
    r=confirm(post,r);assert r['overall_status']=='stale',r
    with env.factory() as db:assert db.get(WorkoutSession,sid).status!='finished';assert list(db.scalars(select(WorkoutLog)))==[]


def test_finish_failure_rolls_back_session_and_logs(training,monkeypatch):
    env,model,sid,post=training;model.responses.append(finish_task);r=post('结束并保存')
    monkeypatch.setattr(records,'verify',lambda *a:(_ for _ in ()).throw(ValueError('injected verification failure')))
    r=confirm(post,r);assert r['overall_status']=='failed',r
    with env.factory() as db:assert db.get(WorkoutSession,sid).status=='active';assert list(db.scalars(select(WorkoutLog)))==[]


def test_concurrent_finish_creates_one_log_per_item(training):
    env,model,sid,post=training
    with env.factory() as db:version=records.revision(db.get(WorkoutSession,sid))
    def close():
        with env.factory() as db:
            r=records.finish(db,db.get(User,DEMO_USER_ID),sid,FinishSession(expected_revision=version));db.commit();return r
    with ThreadPoolExecutor(max_workers=2) as pool:rows=list(pool.map(lambda _:close(),range(2)))
    assert rows[0]['logs'][0]['id']==rows[1]['logs'][0]['id']
    with env.factory() as db:assert len(list(db.scalars(select(WorkoutLog))))==1


def test_legacy_finish_no_longer_fills_plan_counts_and_missing_reps_stay_unknown(training):
    env,model,sid,post=training
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        before=copy.deepcopy(s.items)
        result=workout.finish_session(db,user,sid,completed=True)
        assert [i['completed_sets'] for i in s.items]==[i['completed_sets'] for i in before]
        assert len(result['logs'])==1 and result['logs'][0]['sets']==3
        assert workout.finish_session(db,user,sid)['logs'][0]['log_id']==result['logs'][0]['log_id']
        with pytest.raises(ValueError):workout.complete_set(db,user,sid)
    # A fresh session: a click with no repetitions records completion, not planned repetitions.
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);s=workout.start_session(db,user,DAY)
        workout.complete_set(db,user,s.id)
        assert s.items[0]['set_logs'][0]['reps'] is None


def test_cross_user_record_access_is_rejected(training):
    env,model,sid,post=training
    with env.factory() as db:
        other=User(id='other-record-user',nickname='other');db.add(other);db.commit()
        with pytest.raises(ValueError):records.read(db,other,sid)
        with pytest.raises(ValueError):records.finish(db,other,sid,FinishSession(expected_revision='0'*64))


def test_additive_legacy_log_schema_preserves_rows_and_unique_session_item(tmp_path):
    from app.db import make_engine
    from app.db_migrations import ensure_additive_schema
    engine=make_engine('sqlite:///'+str(tmp_path/'old.db'))
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE workout_logs (id VARCHAR(36) PRIMARY KEY, note TEXT)'))
        conn.execute(text("INSERT INTO workout_logs (id,note) VALUES ('legacy','unchanged')"))
    ensure_additive_schema(engine);ensure_additive_schema(engine)
    assert {'session_id','session_item_id','details','revision'}<={c['name'] for c in inspect(engine).get_columns('workout_logs')}
    with engine.begin() as conn:
        assert conn.execute(text("SELECT note,session_id FROM workout_logs WHERE id='legacy'")).one()==('unchanged',None)
        conn.execute(text("INSERT INTO workout_logs (id,session_id,session_item_id) VALUES ('one','s','i')"))
        with pytest.raises(Exception):conn.execute(text("INSERT INTO workout_logs (id,session_id,session_item_id) VALUES ('two','s','i')"))
    engine.dispose()


def test_explicit_skip_keeps_actual_partial_sets_and_zero_minutes(training):
    env,model,sid,post=training
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        data=records.finish(db,user,sid,FinishSession(expected_revision=records.revision(s),unfinished_policy='skip_remaining',actual_minutes=0))
        assert [i['status'] for i in data['session']['items']]==['partial','skipped','skipped']
        assert data['logs'][0]['sets']==3 and data['actual_minutes_known'] and data['session']['actual_minutes']==0
        db.commit()


@pytest.mark.parametrize('sets,expected',[ (2,'partial'),(0,'unrecorded'),(5,'done') ])
def test_correction_changes_one_item_and_keeps_missing_measurements_unknown(training,sets,expected):
    env,model,sid,post=training
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        data=records.finish(db,user,sid,FinishSession(expected_revision=records.revision(s)));db.commit()
        other=copy.deepcopy(data['session']['items'][1:]);iid=data['session']['items'][0]['id']
        data=records.correct(db,user,sid,iid,CorrectRecord(expected_revision=data['record_revision'],actual_sets=sets,reason='用户纠正实际总组数'))
        assert data['session']['items'][1:]==other
        assert data['session']['items'][0]['status']==expected and data['logs'][0]['sets']==sets
        if sets==5:assert data['logs'][0]['details']['set_logs'][-1]['reps'] is None
        db.commit()
        with pytest.raises(ValueError):records.correct(db,user,sid,iid,CorrectRecord(expected_revision='0'*64,actual_sets=3,reason='stale correction'))


def test_invalid_set_or_contradictory_state_does_not_change_finished_records(training):
    env,model,sid,post=training
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        data=records.finish(db,user,sid,FinishSession(expected_revision=records.revision(s)));db.commit()
        iid=data['session']['items'][0]['id'];version=data['record_revision']
        for args in [{'set_no':4,'weight_kg':80},{'actual_sets':3,'completion_state':'skipped'}]:
            with pytest.raises(ValueError):
                with db.begin_nested():records.correct(db,user,sid,iid,CorrectRecord(expected_revision=version,reason='invalid correction',**args))
        assert records.read(db,user,sid)['record_revision']==version
        assert list(db.scalars(select(WorkoutRecordRevision)))==[]


def test_all_unrecorded_session_creates_no_fabricated_logs(training):
    env,model,sid,post=training
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        items=copy.deepcopy(s.items)
        for i in items:i.update(completed_sets=0,set_logs=[],status='pending')
        s.items=items;db.commit()
        data=records.finish(db,user,sid,FinishSession(expected_revision=records.revision(s)))
        assert data['logs']==[] and all(i['status']=='unrecorded' for i in data['session']['items'])
        assert data['session']['completion_pct']==0


def test_fully_recorded_session_closes_after_last_live_set(training):
    env,model,sid,post=training
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID)
        for _ in range(9):workout.complete_set(db,user,sid,reps=5)
        s=db.get(WorkoutSession,sid);assert s.status=='completed'
        data=records.finish(db,user,sid,FinishSession(expected_revision=records.revision(s)))
        assert len(data['logs'])==3 and all(l['sets']==4 for l in data['logs'])
        assert data['session']['completion_pct']==100


@pytest.mark.parametrize('operation',[workout.skip_action,workout.replace_action,workout.adjust_session])
def test_live_mutation_endpoints_cannot_overwrite_finished_facts(training,operation):
    env,model,sid,post=training
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
        data=records.finish(db,user,sid,FinishSession(expected_revision=records.revision(s)));db.commit()
        with pytest.raises(ValueError):operation(db,user,sid)
        assert records.read(db,user,sid)['record_revision']==data['record_revision']
