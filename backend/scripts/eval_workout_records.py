"""Real LLM + authenticated API + isolated SQLite, with continuous actual facts."""
import sys,json,tempfile,argparse,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from app.config import Settings
from app.llm.tool_client import tool_client_from_settings
from app.evals.dialogue_runner import DialogueEnvironment,DAY,digest,diff
from app.models import User,WorkoutSession,WorkoutLog,WorkoutRecordRevision
from app.seed import DEMO_USER_ID
from app.domain import workout


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--suite',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args()
    suite=json.loads(a.suite.read_text());model=tool_client_from_settings(Settings())
    if not model.available():raise SystemExit('real model required')
    report={'id':suite['id'],'split':suite['split'],'model':model.model,'suite_hash':digest(suite),
        'evaluator_hash':digest(Path(__file__).read_text()),
        'source_hash':digest({str(p):p.read_text() for p in sorted(Path('app').rglob('*.py'))}),
        'database':'isolated SQLite','real_order_writes':0,'conversations':[],'complete':False}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    def save():a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    for c in suite['conversations']:
        record={'id':c['id'],'turns':[]};report['conversations'].append(record)
        with tempfile.TemporaryDirectory(prefix='workout-live-eval-') as tmp:
            # Suite labels are descriptive; the public chat thread is <=32 chars.
            # Hash the full label to keep long IDs isolated without truncation collisions.
            env=DialogueEnvironment(Path(tmp)/'state.db',digest(c['id'])[:32],model)
            try:
                with env.factory() as db:
                    user=db.get(User,DEMO_USER_ID)
                    if suite.get('fixture')=='progress':
                        from app.domain.day_plan import effective_day,save_override
                        v=effective_day(db,user,DAY)
                        actions=[{'slot':'warmup','name':'动态热身','action_ref':'dynamic_warmup','sets':1,'duration_min':5}]+[{**x,'sets':3,'reps':10,'duration_min':12} for x in v['actions']]
                        save_override(db,user,DAY,actions=actions,meals=v['meals'],summary='合成训练进度夹具',constraints={},reason='synthetic fixture',base_version=v['plan_version']);db.commit()
                    session=workout.start_session(db,user,DAY)
                    if suite.get('fixture')!='progress':
                        for reps,weight in [(8,50),(9,55),(10,60)]:workout.complete_set(db,user,session.id,reps=reps,weight_kg=weight)
                    sid=session.id
                for i,t in enumerate(c['turns'],1):
                    before=env.snapshot();calls=len(env.client.calls)
                    response=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':t['text'],'date':str(DAY),'thread':env.thread,'request_id':c['id']+':'+str(i)})
                    result=response.json().get('data') or {'overall_status':'http_error','reply':response.text}
                    after=env.snapshot();expected=t['expect'];errors=[]
                    with env.factory() as db:
                        s=db.get(WorkoutSession,sid);logs=list(db.scalars(select(WorkoutLog).where(WorkoutLog.session_id==sid)))
                        order={item['id']:index for index,item in enumerate(s.items)}
                        logs.sort(key=lambda log:order.get(log.session_item_id,len(order)))
                        audits=list(db.scalars(select(WorkoutRecordRevision).where(WorkoutRecordRevision.session_id==sid)))
                        actual={'status':s.status,'sets':[l.sets for l in logs],'weights':[(l.details.get('set_logs') or [{}])[-1].get('weight_kg') for l in logs],
                            'reps':[l.reps for l in logs],
                            'logs':len(logs),'audits':len(audits),'items':s.items,
                            'item_sets':[int(x.get('completed_sets') or 0) for x in s.items],
                            'targets':[x['target_sets'] for x in s.items],
                            'actual_minutes':s.actual_minutes,
                            'progress_percent':workout.session_payload(s)['progress']['percent']}
                    if result.get('overall_status') not in expected['statuses']:errors.append('response_status:'+str(result.get('overall_status')))
                    for k in ['status','sets','weights','reps','logs','audits','item_sets','targets','progress_percent','actual_minutes']:
                        if k in expected and actual[k]!=expected[k]:errors.append('facts:'+k)
                    intended_days=copy.deepcopy(before['days'])
                    if expected.get('day_strength_sets') is not None:
                        for action in intended_days[str(DAY)]['actions'].values():
                            if action.get('slot')=='strength':action['sets']=expected['day_strength_sets']
                    protected=diff(intended_days,after['days'])
                    # Profile, preferences, meals and other dates remain untouched.
                    for table in ['profiles','meal_logs','plans','plan_versions','memories']:
                        protected+=diff(before['tables'].get(table),after['tables'].get(table))
                    if protected:errors.append('unrelated_changes')
                    row={'turn':i,'user_text':t['text'],'understood_goals':result.get('task_plan'),
                        'context_and_model_calls':env.client.calls[calls:],'task_tools_and_arguments':result.get('task_results'),
                        'before':before,'after':after,'actual_changes':diff(before,after),'unauthorized_changes':protected,
                        'actual_facts':actual,'reply':result.get('reply'),'result':result,'history_after':env.history(),
                        'oracle':expected,'errors':errors,'passed':not errors}
                    record['turns'].append(row);save()
                    print(json.dumps({'turn':i,'passed':not errors,'errors':errors,'reply':result.get('reply')},ensure_ascii=False),flush=True)
            finally:env.close()
    turns=[t for c in report['conversations'] for t in c['turns']]
    report.update(complete=True,total=len(turns),passed=sum(t['passed'] for t in turns),pass_rate=sum(t['passed'] for t in turns)/len(turns),
        unauthorized_changes=sum(len(t['unauthorized_changes']) for t in turns),
        source_hash_after=digest({str(p):p.read_text() for p in sorted(Path('app').rglob('*.py'))}))
    report['source_unchanged_during_run']=report['source_hash']==report['source_hash_after']
    save();model._client.close()

if __name__=='__main__':main()
