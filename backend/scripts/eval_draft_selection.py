"""Continuous live-model incremental acceptance; only temporary synthetic users."""
import sys,json,tempfile,argparse,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from app.config import Settings
from app.llm.tool_client import tool_client_from_settings
from app.evals.dialogue_runner import DAY,digest,diff
from app.models import DayPlanRevision,AgentMessage
from app.seed import DEMO_USER_ID
from scripts.eval_day_revision_chat import hashes
from tests.test_draft_action_selection import state,seed_clarification


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--suite',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    suite=json.loads(args.suite.read_text());model=tool_client_from_settings(Settings())
    if not model.available():raise SystemExit('real model unavailable')
    report={'suite':suite['id'],'split':suite['split'],'suite_hash':digest(suite),'evaluator_hash':digest(Path(__file__).read_text()),
        'model':model.model,'hashes_before':hashes(),'conversations':[],'complete':False,'real_user_data':False,'real_order_writes':0}
    def save():args.output.parent.mkdir(exist_ok=True,parents=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    for case in suite['conversations']:
        conversation={'id':case['id'],'turns':[]};report['conversations'].append(conversation)
        with tempfile.TemporaryDirectory(prefix='draft-selection-live-') as temp:
            fixture=state.__wrapped__(Path(temp));env=next(fixture);env.client.client=model
            first_id=None
            if case.get('fixture') in {'clarification','preview'}:
                clarifying=case['fixture']=='clarification'
                draft,pending=seed_clarification(env,clarification=clarifying);first_id=draft['revision_id']
                with env.factory() as db:
                    db.add(AgentMessage(user_id=DEMO_USER_ID,thread=env.thread,role='user',text='动作太多，精简到3个正式动作，热身另列，餐食和时长上限不变，先不要应用。' if clarifying else '重新排训练，先给我预览，餐食不变。'))
                    db.add(AgentMessage(user_id=DEMO_USER_ID,thread=env.thread,role='agent',text=pending['reply'],result=pending));db.commit()
            try:
                for n,turn in enumerate(case['turns'],1):
                    before=env.snapshot();history_before=env.history();start=len(env.client.calls)
                    response=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':turn['text'],'date':str(DAY),'thread':env.thread})
                    result=response.json().get('data') or {'overall_status':'http_error','reply':response.text};after=env.snapshot();expected=turn['expect'];errors=[]
                    with env.factory() as db:
                        rows=list(db.scalars(select(DayPlanRevision).order_by(DayPlanRevision.created_at)))
                        row=rows[-1] if rows else None
                        fact={'id':row.id if row else None,'version':row.version if row else None,'status':row.status if row else None,
                            'target':copy.deepcopy(row.target_plan) if row else None,'constraints':copy.deepcopy(row.constraints) if row else None,'drafts':len(rows)}
                    if first_id is None and fact['id']:first_id=fact['id']
                    if result.get('overall_status') not in expected['statuses']:errors.append('response:'+str(result.get('overall_status')))
                    if 'draft_status' in expected and fact['status']!=expected['draft_status']:errors.append('draft_status')
                    if expected.get('same_draft') and fact['id']!=first_id:errors.append('same_draft')
                    actions=(fact['target'] or {}).get('actions',[]);formal=[a for a in actions if a['slot']!='warmup'];refs=[a['action_ref'] for a in formal]
                    if 'formal_count' in expected and len(formal)!=expected['formal_count']:errors.append('formal_count')
                    if 'all_count' in expected and len(actions)!=expected['all_count']:errors.append('all_count')
                    if 'refs' in expected and sorted(refs)!=sorted(expected['refs']):errors.append('selected_refs')
                    if set(refs)&set(expected.get('excluded_refs',[])):errors.append('excluded_action')
                    if expected.get('max_formal') and len(formal)>expected['max_formal']:errors.append('action_limit')
                    if expected.get('quantity_limit') and (fact['constraints'] or {}).get('max_workout_actions')!=expected['quantity_limit']:errors.append('stored_quantity_limit')
                    protected=[];days=copy.deepcopy(before['days'])
                    if expected.get('apply'):
                        days[str(DAY)]=copy.deepcopy(after['days'][str(DAY)])
                        protected+=diff(before['days'][str(DAY)]['meals'],after['days'][str(DAY)]['meals'])
                    protected+=diff(days,after['days'])
                    for table in ['profiles','meal_logs','workout_logs','workout_sessions','plans','plan_versions','memories']:
                        protected+=diff(before['tables'].get(table),after['tables'].get(table))
                    if protected:errors.append('unauthorized_changes')
                    entry={'turn':n,'user_text':turn['text'],'actual_goals':result.get('task_plan'),
                        'context_and_model_calls':env.client.calls[start:],'tools_and_parameters':result.get('task_results'),
                        'before':before,'after':after,'actual_changes':diff(before,after),'unauthorized_changes':protected,
                        'reply':result.get('reply'),'result':result,'history_before':history_before,'history_after':env.history(),
                        'facts':fact,'oracle':expected,'passed':not errors,'errors':errors}
                    conversation['turns'].append(entry);save()
                    print(json.dumps({'conversation':case['id'],'turn':n,'passed':not errors,'errors':errors,'reply':result.get('reply')},ensure_ascii=False),flush=True)
            finally:
                try:next(fixture)
                except StopIteration:pass
    turns=[t for c in report['conversations'] for t in c['turns']]
    report.update(complete=True,total=len(turns),passed=sum(t['passed'] for t in turns),
        pass_rate=sum(t['passed'] for t in turns)/len(turns),unauthorized_changes=sum(len(t['unauthorized_changes']) for t in turns),hashes_after=hashes())
    save();model._client.close()


if __name__=='__main__':main()
