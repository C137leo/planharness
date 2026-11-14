"""Live continuous goal/meal editing acceptance; no real user DB or orders."""
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
from tests.test_revision_goal_contract import state,training_preview,pending_preview


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--suite',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    suite=json.loads(args.suite.read_text());model=tool_client_from_settings(Settings())
    if not model.available():raise SystemExit('real model required')
    report={'suite':suite['id'],'split':suite['split'],'suite_hash':digest(suite),'evaluator_hash':digest(Path(__file__).read_text()),'model':model.model,
        'hashes_before':hashes(),'conversations':[],'complete':False,'real_user_data':False,'real_order_writes':0}
    def save():args.output.parent.mkdir(exist_ok=True,parents=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    for case in suite['conversations']:
        conversation={'id':case['id'],'turns':[]};report['conversations'].append(conversation)
        with tempfile.TemporaryDirectory(prefix='revision-goal-live-') as temp:
            fixture=state.__wrapped__(Path(temp));env=next(fixture);draft=training_preview(env);p=pending_preview(env,draft);env.client.client=model
            with env.factory() as db:
                db.add(AgentMessage(user_id=DEMO_USER_ID,thread=env.thread,role='user',text='训练保留高脚杯深蹲、哑铃卧推、哑铃俯身划船3个正式动作，加上热身，先看预览。'))
                db.add(AgentMessage(user_id=DEMO_USER_ID,thread=env.thread,role='agent',text='预览为热身5分钟，三个正式动作各8分钟，合计29分钟，餐食未改。',result=p));db.commit()
            try:
                for n,turn in enumerate(case['turns'],1):
                    before=env.snapshot();history_before=env.history();start=len(env.client.calls)
                    response=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':turn['text'],'thread':env.thread,'date':str(DAY)})
                    result=response.json().get('data') or {'overall_status':'http_error','reply':response.text};after=env.snapshot();expected=turn['expect'];errors=[]
                    with env.factory() as db:
                        rows=list(db.scalars(select(DayPlanRevision).order_by(DayPlanRevision.created_at)));row=rows[-1]
                        from app.domain import day_revision
                        fact=day_revision.payload(row);fact['drafts']=len(rows)
                    if result.get('overall_status') not in expected['statuses']:errors.append('response:'+str(result.get('overall_status')))
                    if fact['revision_id']!=draft['revision_id'] or fact['drafts']!=1:errors.append('same_draft')
                    if fact['status']!=expected.get('draft_status','awaiting_confirmation'):errors.append('draft_status')
                    if 'minutes' in expected and fact['workout_minutes_after']!=expected['minutes']:errors.append('actual_minutes')
                    if 'max_minutes' in expected and fact['workout_minutes_after']>expected['max_minutes']:errors.append('max_minutes')
                    if 'min_minutes' in expected and fact['workout_minutes_after']<expected['min_minutes']:errors.append('min_minutes')
                    if 'target' in expected and fact['constraints'].get('target_workout_minutes')!=expected['target']:errors.append('stored_target')
                    if expected.get('scope') and sorted(fact['scope'])!=sorted(expected['scope']):errors.append('scope')
                    refs=[a['action_ref'] for a in fact['target_plan']['actions'] if a['slot']!='warmup']
                    if refs!=['goblet-squat','dumbbell-bench-press','dumbbell-bent-over-row']:errors.append('selected_actions_changed')
                    meals={m['slot']:{i['name']:i['grams'] for i in m['items']} for m in fact['target_plan']['meals']}
                    for slot,foods in expected.get('foods',{}).items():
                        if any(meals.get(slot,{}).get(food)!=grams for food,grams in foods.items()):errors.append('food_quantity:'+slot)
                    for slot,foods in expected.get('contains',{}).items():
                        if any(food not in meals.get(slot,{}) for food in foods):errors.append('missing_food:'+slot)
                    for slot,foods in expected.get('excludes',{}).items():
                        if any(food in meals.get(slot,{}) for food in foods):errors.append('excluded_food:'+slot)
                    protected=[];days=copy.deepcopy(before['days'])
                    if expected.get('apply'):
                        days[str(DAY)]=copy.deepcopy(after['days'][str(DAY)])
                        for slot in before['days'][str(DAY)]['meals']:
                            if slot not in expected.get('allowed_meals',[]):protected+=diff(before['days'][str(DAY)]['meals'][slot],after['days'][str(DAY)]['meals'].get(slot))
                    protected+=diff(days,after['days'])
                    for table in ['profiles','meal_logs','workout_logs','workout_sessions','plans','plan_versions','memories']:
                        protected+=diff(before['tables'].get(table),after['tables'].get(table))
                    if protected:errors.append('unauthorized_changes')
                    entry={'turn':n,'user_text':turn['text'],'actual_goals':result.get('task_plan'),'context_and_model_calls':env.client.calls[start:],
                        'tools_and_parameters':result.get('task_results'),'before':before,'after':after,'actual_changes':diff(before,after),'unauthorized_changes':protected,
                        'reply':result.get('reply'),'result':result,'history_before':history_before,'history_after':env.history(),'facts':fact,'oracle':expected,'passed':not errors,'errors':errors}
                    conversation['turns'].append(entry);save();print(json.dumps({'conversation':case['id'],'turn':n,'passed':not errors,'errors':errors,'reply':result.get('reply')},ensure_ascii=False),flush=True)
            finally:
                try:next(fixture)
                except StopIteration:pass
    turns=[t for c in report['conversations'] for t in c['turns']]
    report.update(complete=True,total=len(turns),passed=sum(t['passed'] for t in turns),pass_rate=sum(t['passed'] for t in turns)/len(turns),
        unauthorized_changes=sum(len(t['unauthorized_changes']) for t in turns),hashes_after=hashes());save();model._client.close()


if __name__=='__main__':main()
