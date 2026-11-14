"""Continuous real-model replan acceptance with synthetic facts only; no main app DB."""
import sys,json,tempfile,argparse,copy,re
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from app.config import Settings
from app.llm.tool_client import tool_client_from_settings
from app.evals.dialogue_runner import DAY,digest,diff
from scripts.eval_day_revision_chat import hashes
from tests.test_workout_replan import progress_chat,legacy_finished
from app.models import User,WorkoutSession,WorkoutReplanAuthorization,DayPlanRevision,WorkoutLog,CoachPendingPlan,utcnow
from app.seed import DEMO_USER_ID
from app.domain import workout_records as records
from app.agent.workout_capabilities import RecordProgress

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--suite',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    suite=json.loads(args.suite.read_text());model=tool_client_from_settings(Settings())
    if not model.available():raise SystemExit('real model required')
    report={'suite':suite['id'],'split':suite['split'],'suite_hash':digest(suite),'evaluator_hash':digest(Path(__file__).read_text()),'hashes_before':hashes(),'model':model.model,'conversations':[],'complete':False,'real_user_data':False,'real_order_writes':0}
    def save():args.output.parent.mkdir(exist_ok=True,parents=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    for case in suite['conversations']:
        record={'id':case['id'],'turns':[]};report['conversations'].append(record)
        with tempfile.TemporaryDirectory(prefix='replan-live-') as temp:
            fixture=progress_chat.__wrapped__(Path(temp));env,_,sid,post=next(fixture);env.client.client=model
            if case.get('fixture')=='empty_day':
                from app.domain.day_plan import effective_day,save_override
                with env.factory() as db:
                    u=db.get(User,DEMO_USER_ID);current=effective_day(db,u,DAY)
                    db.delete(db.get(WorkoutSession,sid))
                    save_override(db,u,DAY,actions=[],meals=current['meals'],constraints={},summary='合成休息日',reason='synthetic empty-day setup',base_version=current['plan_version']);db.commit()
                sid=None
            elif case.get('fixture')=='partial_active':
                with env.factory() as db:
                    u=db.get(User,DEMO_USER_ID);s=db.get(WorkoutSession,sid)
                    records.progress(db,u,sid,s.items[1]['id'],RecordProgress(expected_revision=records.revision(s),completion_basis='actual',actual_sets=1,reps=8));db.commit()
            else:legacy_finished(env,sid)
            expired_ids=[]
            if case.get('expired_approval'):
                import datetime as dt
                from app.domain import workout_replan,day_revision
                with env.factory() as db:
                    u=db.get(User,DEMO_USER_ID);ctx=day_revision.context(db,u,DAY)
                    approval=workout_replan.request(db,u,DAY,scope=['workout','breakfast','lunch','dinner'],
                        expected_base_revision=day_revision.digest(ctx),goal_text='合成旧请求：重新安排今天的训练和三餐，保留真实记录')
                    workout_replan.approve(db,u,approval['authorization_id'],approval['etag'])
                    db.get(WorkoutReplanAuthorization,approval['authorization_id']).expires_at=utcnow()-dt.timedelta(hours=1);db.commit()
                    expired_ids.append(approval['authorization_id'])
            if case.get('failed_history'):
                from tests.test_replan_authorization_state import seed_failed_history
                seed_failed_history(env)
            with env.factory() as db:original_items=copy.deepcopy(db.get(WorkoutSession,sid).items) if sid else []
            first_draft=None
            try:
                for number,turn in enumerate(case['turns'],1):
                    event=None
                    if turn.get('expire_confirmation_before'):
                        import datetime as dt
                        with env.factory() as db:
                            pending=db.scalar(select(CoachPendingPlan).where(CoachPendingPlan.thread==env.thread,CoachPendingPlan.status=='pending').order_by(CoachPendingPlan.created_at.desc()))
                            if pending and len(pending.tasks)==1 and pending.tasks[0]['capability_id']=='day_plan.confirm_replan':
                                row=db.get(WorkoutReplanAuthorization,pending.tasks[0]['resolution']['selected_id'])
                                event={'kind':'simulated_elapsed_confirmation_window','pending_id':pending.id,'authorization_id':row.id,
                                    'previous_pending_deadline':pending.expires_at,'previous_authorization_deadline':row.expires_at}
                                pending.expires_at=row.expires_at=utcnow()-dt.timedelta(seconds=1);db.commit();expired_ids.append(row.id)
                            else:event={'kind':'elapsed_window_not_applied','reason':'prior confirmation was not created'}
                    before=env.snapshot();calls=len(env.client.calls);result=post(turn['text']);after=env.snapshot();expected=turn['expect'];errors=[]
                    day=env.http.get('/api/v1/agent/day',headers=env.headers,params={'date':str(DAY)}).json()['data']
                    with env.factory() as db:
                        old=db.get(WorkoutSession,sid) if sid else None;auth=db.scalar(select(WorkoutReplanAuthorization).order_by(WorkoutReplanAuthorization.created_at.desc()));draft=db.scalar(select(DayPlanRevision).order_by(DayPlanRevision.created_at.desc()))
                        facts={'old_status':old.status if old else None,'old_items':copy.deepcopy(old.items) if old else [],'authorization_status':auth.status if auth else None,'authorization_id':auth.id if auth else None,'draft_status':draft.status if draft else None,'draft_id':draft.id if draft else None,'target':copy.deepcopy(draft.target_plan) if draft else None,'progress':day['day_progress'],'completed':day['completed'],'previous_training':day.get('previous_training',[])}
                    if result.get('overall_status') not in expected['statuses']:errors.append('response:'+str(result.get('overall_status')))
                    if facts['draft_id'] and first_draft is None:first_draft=facts['draft_id']
                    if expected.get('same_draft') and facts['draft_id']!=first_draft:errors.append('same_draft')
                    if expected.get('reply_contains') and any(s not in result.get('reply','') for s in expected['reply_contains']):errors.append('reply_missing_requested_fact')
                    if expected.get('fresh_consent'):
                        if facts['authorization_id'] in expired_ids or facts['authorization_status']!='pending' or not result.get('pending_confirmation'):errors.append('fresh_pending_consent_not_created')
                        elif result['pending_confirmation']['tasks'][0]['capability_id']!='day_plan.confirm_replan':errors.append('wrong_confirmation_stage')
                    for name in ['authorization_status','draft_status','old_status','completed']:
                        if name in expected and facts[name]!=expected[name]:errors.append(name)
                    if 'done_sets' in expected and facts['progress']['done_sets']!=expected['done_sets']:errors.append('done_sets')
                    if 'previous_sets' in expected and sum(i['completed_sets'] for i in facts['previous_training'])!=expected['previous_sets']:errors.append('previous_sets')
                    if expected.get('max_minutes') and facts['target']:
                        from app.domain.day_plan import _estimated_minutes
                        if sum(_estimated_minutes(a) for a in facts['target']['actions'])>expected['max_minutes']:errors.append('time_budget')
                    if expected.get('foods'):
                        meals={m['slot']:{i['name']:i['grams'] for i in m['items']} for m in (facts['target'] or {}).get('meals',[])}
                        for slot,items in expected['foods'].items():
                            if meals.get(slot)!=items:errors.append('food:'+slot)
                    if facts['old_items']!=original_items:errors.append('rewrote_original_facts')
                    if expected.get('not_all_complete') and any(x in result.get('reply','') for x in ['已经全部完成','没有剩余部分可以再安排','没有可以重新安排的未完成内容']):errors.append('false_completion_claim')
                    if expected.get('explain_separation'):
                        reply=result.get('reply','');data=result.get('workout_records') or {}
                        if data.get('record_scope')=='prior_cycle':
                            explained='重排前记录' in reply and '当前有效目标已记录 0/' in reply
                        else:
                            explained=any(s in reply for s in ['0','零']) and any(s in reply for s in ['旧','重排前','之前','此前']) and any(s in reply for s in ['不计','单独','分别','分开'])
                        old_count=sum(i['completed_sets'] for i in facts['previous_training'])
                        explained=explained and bool(re.search(r'(?<!\d)'+str(old_count)+r'\s*组',reply))
                        if not explained:errors.append('reply_does_not_disambiguate_old_and_new')
                    protected=[]
                    planned=copy.deepcopy(before['days'])
                    if expected.get('apply'):
                        planned[str(DAY)]=copy.deepcopy(after['days'][str(DAY)])
                        old_meals=copy.deepcopy(before['days'][str(DAY)]['meals']);new_meals=after['days'][str(DAY)]['meals']
                        for slot in expected.get('meal_scopes',[]):
                            if slot in new_meals:old_meals[slot]=new_meals[slot]
                        protected+=diff(old_meals,new_meals)
                    protected+=diff(planned,after['days'])
                    for table in ['profiles','meal_logs','plans','plan_versions','memories']:protected+=diff(before['tables'][table],after['tables'][table])
                    old_sessions={row['id']:copy.deepcopy(row) for row in before['tables']['workout_sessions']}
                    new_sessions={row['id']:row for row in after['tables']['workout_sessions']}
                    if expected.get('apply'):
                        for key,row in old_sessions.items():
                            if row['date']==str(DAY) and row['status'] in ['active','paused','completed'] and new_sessions.get(key,{}).get('status')=='superseded':
                                row['status']='superseded'
                                for field in ['finished_at','updated_at']:row[field]=new_sessions.get(key,{}).get(field)
                    protected+=diff(old_sessions,new_sessions)
                    old_logs={row['id']:row for row in before['tables']['workout_logs']};new_logs={row['id']:row for row in after['tables']['workout_logs']}
                    for key,value in old_logs.items():protected+=diff(value,new_logs.get(key))
                    allowed={i['id']:i for i in original_items if int(i.get('completed_sets') or 0)>0}
                    for key,log in new_logs.items():
                        if key in old_logs:continue
                        origin=allowed.get(log['session_item_id'])
                        if not expected.get('apply') or log['session_id']!=sid or not origin or log['sets']!=origin['completed_sets'] or log['details']['set_logs']!=origin['set_logs']:protected.append({'unexpected_log':key})
                    if protected:errors.append('unauthorized_changes')
                    record['turns'].append({'turn':number,'environment_event':event,'user_text':turn['text'],'goals':result.get('task_plan'),'context_and_model_calls':env.client.calls[calls:],'tools_and_arguments':result.get('task_results'),'before':before,'after':after,'actual_changes':diff(before,after),'unauthorized_changes':protected,'actual_facts':facts,'reply':result.get('reply'),'result':result,'history_after':env.history(),'oracle':expected,'passed':not errors,'errors':errors});save()
                    print(json.dumps({'turn':number,'passed':not errors,'errors':errors,'reply':result.get('reply')},ensure_ascii=False),flush=True)
            finally:
                try:next(fixture)
                except StopIteration:pass
    turns=[t for c in report['conversations'] for t in c['turns']];report.update(complete=True,total=len(turns),passed=sum(t['passed'] for t in turns),pass_rate=sum(t['passed'] for t in turns)/len(turns),unauthorized_changes=sum(len(t['unauthorized_changes']) for t in turns),hashes_after=hashes());save();model._client.close()
if __name__=='__main__':main()
