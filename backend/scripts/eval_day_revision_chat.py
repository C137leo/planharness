"""Real LLM; synthetic SQLite and continuous conversation only. Oracles stay outside prompts."""
import sys,json,tempfile,argparse,copy,ast
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from app.config import Settings
from app.llm.tool_client import tool_client_from_settings
from app.evals.dialogue_runner import DAY,digest,diff
from app.models import DayPlanRevision
from tests.test_day_revision_chat import state,restore_to_three

def hashes():
    texts={str(p):p.read_text() for p in sorted(Path('app').rglob('*.py'))}
    return {'source':digest(texts),'executable_ast':digest({k:ast.dump(ast.parse(v)) for k,v in texts.items()})}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--suite',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    suite=json.loads(args.suite.read_text());model=tool_client_from_settings(Settings())
    if not model.available():raise SystemExit('real model required')
    report={'suite':suite['id'],'split':suite['split'],'suite_hash':digest(suite),'evaluator_hash':digest(Path(__file__).read_text()),'model':model.model,'hashes_before':hashes(),'conversations':[],'real_order_writes':0,'complete':False}
    def save():args.output.parent.mkdir(exist_ok=True,parents=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    for case in suite['conversations']:
        record={'id':case['id'],'turns':[]};report['conversations'].append(record)
        with tempfile.TemporaryDirectory(prefix='day-revision-live-') as tmp:
            fixture=state.__wrapped__(Path(tmp));env=next(fixture);env.client.client=model
            if case.get('fixture')=='rollback':restore_to_three(env)
            first_id=None;prior_workout=None
            try:
                for number,turn in enumerate(case['turns'],1):
                    before=env.snapshot();start=len(env.client.calls)
                    response=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'thread':env.thread,'date':str(DAY),'text':turn['text']})
                    result=response.json().get('data') or {'overall_status':'http_error','reply':response.text};after=env.snapshot();errors=[];expect=turn['expect']
                    with env.factory() as db:
                        rows=list(db.scalars(select(DayPlanRevision).order_by(DayPlanRevision.created_at)))
                        current=rows[-1] if rows else None
                        fact={'drafts':len(rows),'status':current.status if current else None,'id':current.id if current else None,'version':current.version if current else None,'scope':current.scope if current else None,'target':copy.deepcopy(current.target_plan) if current else None}
                    if result['overall_status'] not in expect['statuses']:errors.append('response:'+result['overall_status'])
                    if expect.get('draft_status') and fact['status']!=expect['draft_status']:errors.append('draft_status')
                    if expect.get('scope') and sorted(fact['scope'] or [])!=sorted(expect['scope']):errors.append('scope')
                    if fact['id'] and first_id is None:first_id=fact['id']
                    if expect.get('same_draft') and fact['id']!=first_id:errors.append('same_draft')
                    if expect.get('keep_workout') and fact['target'] and fact['target']['actions']!=prior_workout:errors.append('retained_workout')
                    if fact['target'] and prior_workout is None:prior_workout=copy.deepcopy(fact['target']['actions'])
                    if expect.get('food'):
                        meal=next((m for m in (fact['target'] or {}).get('meals',[]) if m['slot']=='dinner'),{})
                        found={i['name']:i['grams'] for i in meal.get('items',[])}
                        if any(found.get(k)!=v for k,v in expect['food'].items()):errors.append('food')
                    if expect.get('reply_contains') and any(s not in result['reply'] for s in expect['reply_contains']):errors.append('reply_current_plan')
                    protected=[]
                    if not expect.get('apply'):protected+=diff(before['days'],after['days'])
                    else:
                        expected_days=copy.deepcopy(before['days']);expected_days[str(DAY)]=copy.deepcopy(after['days'][str(DAY)])
                        protected+=diff(expected_days,after['days'])
                        protected+=diff(before['days'][str(DAY)]['meals']['lunch'],after['days'][str(DAY)]['meals']['lunch'])
                    for table in ['profiles','meal_logs','workout_logs','plans','plan_versions','memories']:
                        protected+=diff(before['tables'].get(table),after['tables'].get(table))
                    if protected:errors.append('unauthorized_changes')
                    row={'turn':number,'user_text':turn['text'],'goals':result.get('task_plan'),'context_and_model_calls':env.client.calls[start:],'tools_and_parameters':result.get('task_results'),'before':before,'after':after,'actual_changes':diff(before,after),'unauthorized_changes':protected,'facts':fact,'reply':result.get('reply'),'result':result,'history_after':env.history(),'oracle':expect,'passed':not errors,'errors':errors}
                    record['turns'].append(row);save();print(json.dumps({'turn':number,'passed':not errors,'errors':errors,'reply':result.get('reply')},ensure_ascii=False),flush=True)
            finally:
                try:next(fixture)
                except StopIteration:pass
    turns=[t for c in report['conversations'] for t in c['turns']];report.update(complete=True,total=len(turns),passed=sum(t['passed'] for t in turns),pass_rate=sum(t['passed'] for t in turns)/len(turns),unauthorized_changes=sum(len(t['unauthorized_changes']) for t in turns),hashes_after=hashes());save();model._client.close()
if __name__=='__main__':main()
