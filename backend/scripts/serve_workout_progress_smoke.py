"""Synthetic-only UI fixture for progress, adjusted defaults and calorie formatting."""
import sys,json,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select,delete
from app.evals.dialogue_runner import DialogueEnvironment,DAY
from app.api.coach_routes import coach_commerce_dependency
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.domain import workout
from app.domain.day_plan import effective_day,save_override
from app.models import User,Plan,PlanVersion,MealPlan,WorkoutSession,WorkoutLog
from app.seed import DEMO_USER_ID
from tests.test_semantic_binding import Scripted
from tests.test_workout_progress import report,reduce_plan,finish_defaults
from starlette.staticfiles import StaticFiles
import uvicorn

def edit(d):
    return {'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],
        'keep_task_ids':[],'execute_revision':False,'new_request_text':'其余仍按刚才完成摘要，深蹲只做1组用了6分钟，请重新预览'}

def main():
    model=Scripted([report,reduce_plan,finish_defaults,edit,lambda d:finish_defaults(d,True)])
    with tempfile.TemporaryDirectory(prefix='progress-ui-') as tmp:
        env=DialogueEnvironment(Path(tmp)/'state.db','progress-ui',model)
        env.app.dependency_overrides[coach_commerce_dependency]=lambda:None
        env.app.include_router(auth_router);env.app.include_router(router)
        with env.factory() as db:
            user=db.get(User,DEMO_USER_ID);v=effective_day(db,user,DAY)
            actions=[{'slot':'warmup','name':'动态热身','action_ref':'dynamic_warmup','sets':1,'duration_min':5}]+[{**a,'sets':3,'reps':10,'duration_min':12} for a in v['actions']]
            save_override(db,user,DAY,actions=actions,meals=v['meals'],summary='合成UI训练安排',constraints={},reason='synthetic fixture',base_version=v['plan_version'])
            plan=db.scalar(select(Plan).where(Plan.user_id==user.id));plan.nutrition={**(plan.nutrition or {}),'target_kcal':2092.4}
            version=db.scalar(select(PlanVersion).where(PlanVersion.plan_id==plan.id,PlanVersion.version==plan.current_version))
            db.execute(delete(MealPlan).where(MealPlan.user_id==user.id,MealPlan.plan_version_id==version.id,MealPlan.day_index==DAY.isoweekday()))
            for meal in v['meals']:
                db.add(MealPlan(user_id=user.id,plan_version_id=version.id,day_index=DAY.isoweekday(),slot=meal['slot'],title=meal['title'],items=meal['items'],kcal=meal['kcal'],protein_g=meal.get('protein_g') or 0))
            db.commit();sid=workout.start_session(db,user,DAY).id
        Path('/tmp/workout-progress-ui-auth.json').write_text(json.dumps(env.headers))
        @env.app.get('/api/v1/system/status')
        def status():return {'data':{'paused':False}}
        @env.app.get('/__mock_evidence')
        def evidence():
            with env.factory() as db:
                s=db.get(WorkoutSession,sid);logs=list(db.scalars(select(WorkoutLog).where(WorkoutLog.session_id==sid)))
                return {'model':'scripted','submits':0,'status':s.status,'progress':workout.session_payload(s)['progress'],
                    'targets':[i['target_sets'] for i in s.items],'done':[int(i.get('completed_sets') or 0) for i in s.items],
                    'logs':len(logs),'minutes':s.actual_minutes,'weights':[r.get('weight_kg') for l in logs for r in l.details['set_logs']]}
        env.app.mount('/',StaticFiles(directory='/tmp/workout-progress-h5',html=True),name='ui')
        try:uvicorn.run(env.app,host='127.0.0.1',port=5194)
        finally:env.close()

if __name__=='__main__':main()
