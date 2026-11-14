from app.models import AgentRun
from app.seed import DEMO_USER_ID
from app.domain.runtime_console import redact,trace_events
from tests.test_semantic_binding import env,request,Scripted,binding_plan,confirm,state


def get(e,path):
    r=e.http.get('/api/v1/agent/runtime/'+path,headers=e.headers)
    assert r.status_code==200,r.text
    return r.json()['data']


def test_owner_legacy_and_missing_evidence(env):
    with env.factory() as db:
        r=AgentRun(user_id=DEMO_USER_ID,kind='old',mode='agent',status='failed',input={'thread':env.thread},output={},trace=[{'node':'old','status':'failed','ts':'2026-11-14T01:00:00Z'},{'stage':'new','status':'ok'}]);db.add(r);db.commit();id=r.id
        f=AgentRun(user_id='foreign',kind='old',mode='agent',status='succeeded',input={},output={},trace=[]);db.add(f);db.commit();fid=f.id
    d=get(env,'turns/'+id);assert d['events'][0]['timestamp']=='2026-11-14T01:00:00Z' and d['events'][1]['timestamp'] is None
    assert d['context']['actual_model_inputs'] is None and d['result_evidence']['goal_verified'] is None
    assert env.http.get('/api/v1/agent/runtime/turns/'+fid,headers=env.headers).status_code==404
    assert env.http.get('/api/v1/agent/runtime/turns/'+id).status_code==401
    assert get(env,'turns/'+id)==d


def test_multiturn_confirmation_and_persisted_changes(env):
    before=state(env);first=request(env,'先预览移除两项',Scripted([binding_plan(['山羊挺身','全身拉伸'])]))
    pending=get(env,'turns/'+first['run_id']);assert len(pending['tasks'])==2 and pending['pending_confirmation']
    assert all(x['persistence_evidence']=='not_recorded' for x in pending['tasks']) and state(env)==before
    assert pending['context']['actual_model_inputs'] is not None
    second=request(env,'确认',Scripted([confirm]));d=get(env,'turns/'+second['run_id'])
    assert len(d['tasks'])==2 and all(x['status']=='success' and x['persisted_changes'] for x in d['tasks'])
    assert d['result_evidence']['persisted_journal_count']==2 and get(env,'turns/'+second['run_id'])==d
    messages=get(env,'conversations/'+env.thread)['items'];assert [x['turn_id'] for x in messages]==[first['run_id']]*2+[second['run_id']]*2
    page=get(env,'conversations/'+env.thread+'?limit=2');older=get(env,'conversations/'+env.thread+'?limit=2&before='+page['before'])
    assert len(page['items'])==len(older['items'])==2 and not {x['id'] for x in page['items']} & {x['id'] for x in older['items']}
    assert get(env,'conversations')[0]['thread']==env.thread


def test_redaction_and_stable_event_identity():
    source={'api_key':'sk-secretvalue','nested':{'Authorization':'Bearer abc'},'text':'Bearer abc 13812345678'}
    safe=redact(source);assert all(v not in str(safe) for v in ['sk-secretvalue','Bearer abc','13812345678']) and source['api_key']=='sk-secretvalue'
    trace=[{'stage':'planning','parsed':{'capability_id':'x'}}];events=trace_events(trace,'r')
    assert events==trace_events(trace,'r') and len(events)==1 and events[0]['timestamp'] is None


def test_failure_never_claims_database_success(env):
    result=request(env,'安全模型失败',Scripted([]));d=get(env,'turns/'+result['run_id'])
    assert d['status']==result['overall_status'] and d['diagnostics']['failed_events'] and d['result_evidence']['persisted_journal_count']==0
    assert d['result_evidence']['goal_verified'] is None


def test_empty_conversation_and_legacy_trace_compatibility(env):
    r=env.http.post('/api/v1/agent/runtime/conversations',headers=env.headers);assert r.status_code==200
    thread=r.json()['data']['thread'];assert any(x['thread']==thread and x['status']=='idle' for x in get(env,'conversations'))
    assert get(env,'conversations/'+thread)['items']==[]
    from app.api.routes import router
    env.app.include_router(router)
    result=request(env,'未执行请求',Scripted([]));r=env.http.get('/api/v1/runs/'+result['run_id'],headers=env.headers)
    assert r.status_code==200,r.text
    assert any(x['ts'] is None for x in r.json()['data']['trace'])


def test_foreign_journal_cannot_prove_commit(env):
    from app.models import CoachTaskExecution
    from app.evals.dialogue_runner import DAY
    with env.factory() as db:
        j=CoachTaskExecution(user_id='foreign',thread=env.thread,task_id='t1',request_key='foreign',effective_date=DAY,operation='profile.update',domain='profile',scope='profile',tool='profile',resource_changes=[{'before':'private'}]);db.add(j);db.flush()
        r=AgentRun(user_id=DEMO_USER_ID,kind='test',mode='agent',status='partial',input={'thread':env.thread},output={'task_plan':[{'task_id':'t1'}],'task_results':[{'task_id':'t1','status':'failed','execution_id':j.id}]},trace=[]);db.add(r);db.commit();id=r.id
    d=get(env,'turns/'+id);assert d['status']=='partial' and d['tasks'][0]['persisted_changes']==[] and d['result_evidence']['persisted_journal_count']==0


def test_read_query_is_verified_without_fabricated_database_changes(env):
    def output(d):
        from tests.test_semantic_binding import task
        day=next(o for o in d['snapshot']['objects'] if o['type']=='day')
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(day['id'],'context.inspect').model_dump()]}
    before=state(env);r=request(env,'查询今天训练',Scripted([output]));d=get(env,'turns/'+r['run_id'])
    assert state(env)==before and d['tasks'][0]['status']=='success' and not d['tasks'][0]['persisted_changes']
    assert d['result_evidence']['persisted_journal_count']==0
    assert any(e['event_type']=='execution.boundary.completed' for e in d['events'])


def test_partial_task_failure_keeps_actual_status_and_changes(env):
    def output(d):
        plan=binding_plan(['山羊挺身','深蹲'],'resolved','workout.update_action',{'sets_delta':-3})(d)
        plan['tasks'][1]['arguments']={'sets_delta':-9};return plan
    r=request(env,'改两项',Scripted([output]));d=get(env,'turns/'+r['run_id'])
    assert d['status']=='partial_success' and [t['status'] for t in d['tasks']]==['success','failed']
    assert d['tasks'][0]['persisted_changes'] and not d['tasks'][1]['persisted_changes']
    assert d['diagnostics']['task_failures'] and d['result_evidence']['goal_verified'] is None


def test_observation_sink_failure_does_not_change_execution(env,monkeypatch):
    import app.agent.runtime_observation as obs
    import app.domain.runtime_console as console
    # Only the observer's redaction import is broken while the real execution succeeds.
    original=console.redact
    def broken(value):raise RuntimeError('synthetic telemetry failure')
    monkeypatch.setattr(console,'redact',broken)
    r=request(env,'移除山羊挺身',Scripted([binding_plan(['山羊挺身'],'resolved')]))
    monkeypatch.setattr(console,'redact',original)
    assert r['overall_status']=='success' and get(env,'turns/'+r['run_id'])['tasks'][0]['persisted_changes']


def test_diet_exclusion_has_real_persisted_food_changes(env):
    from tests.test_semantic_binding import task
    def output(d):
        foods=[o for o in d['snapshot']['objects'] if o['type']=='food_item' and o.get('name')=='鸡胸肉']
        assert foods
        tasks=[]
        for n,o in enumerate(foods):
            t=task(o['id'],'meal.remove_food');t.task_id='food'+str(n);tasks.append(t.model_dump())
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':tasks}
    r=request(env,'今天不要鸡胸肉',Scripted([output]));d=get(env,'turns/'+r['run_id'])
    import json
    assert r['overall_status']=='success' and '鸡胸肉' not in json.dumps(r['day']['meals'],ensure_ascii=False)
    assert all(t['persisted_changes'] for t in d['tasks']) and d['result_evidence']['persisted_journal_count']==len(d['tasks'])


def test_authenticated_structured_confirmation_is_json_safe(env):
    r=request(env,'先预览',Scripted([binding_plan(['山羊挺身','全身拉伸'])]))
    p=r['pending_confirmation']
    response=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':'确认','thread':env.thread,'confirmation':{'action':'confirm','pending_id':p['pending_id'],'version':p['version']}})
    assert response.status_code==200,response.text
    result=response.json()['data'];d=get(env,'turns/'+result['run_id'])
    assert d['input']['confirmation']['action']=='confirm' and d['result_evidence']['persisted_journal_count']==2
