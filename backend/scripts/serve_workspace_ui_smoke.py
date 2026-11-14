"""Multi-page UI fixture: temporary synthetic SQLite, no LLM/MCP calls."""
import sys,json,tempfile,datetime as dt
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select,delete
from app.evals.dialogue_runner import DialogueEnvironment,DAY
from app.api.coach_routes import coach_commerce_dependency
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.domain import workout,workout_records
from app.domain.day_plan import effective_day,save_override
from app.models import User,Plan,PlanVersion,MealPlan,WorkoutSession,WorkoutLog,Checkin,Memory
from app.seed import DEMO_USER_ID
from app.agent.workout_capabilities import FinishSession
from tests.test_semantic_binding import Scripted
from starlette.staticfiles import StaticFiles
import uvicorn

def main():
 with tempfile.TemporaryDirectory(prefix='workspace-ui-smoke-') as tmp:
  env=DialogueEnvironment(Path(tmp)/'state.db','workspace-ui',Scripted([]))
  env.app.dependency_overrides[coach_commerce_dependency]=lambda:None
  env.app.include_router(auth_router);env.app.include_router(router)
  with env.factory() as db:
   user=db.get(User,DEMO_USER_ID);day=effective_day(db,user,DAY)
   actions=[{'slot':'warmup','name':'动态热身','action_ref':'dynamic_warmup','sets':1,'duration_min':5}]+[{**a,'sets':3,'reps':10,'duration_min':12} for a in day['actions']]
   save_override(db,user,DAY,actions=actions,meals=day['meals'],summary='合成布局验收',constraints={},reason='synthetic fixture',base_version=day['plan_version'])
   plan=db.scalar(select(Plan).where(Plan.user_id==user.id));version=db.scalar(select(PlanVersion).where(PlanVersion.plan_id==plan.id,PlanVersion.version==plan.current_version))
   db.execute(delete(MealPlan).where(MealPlan.plan_version_id==version.id))
   for weekday in range(1,8):
    for meal in day['meals']:db.add(MealPlan(user_id=user.id,plan_version_id=version.id,day_index=weekday,slot=meal['slot'],title=meal['title'],items=meal['items'],kcal=meal['kcal'],protein_g=meal['protein_g']))
   for n in range(24):db.add(WorkoutLog(user_id=user.id,date=DAY-dt.timedelta(days=n%12+1),action_ref='goblet-squat' if n%2 else 'push-up',duration_min=12,sets=2,reps=10,note='合成历史记录'))
   for n in range(20):
    date=DAY-dt.timedelta(days=n);row=db.scalar(select(Checkin).where(Checkin.user_id==user.id,Checkin.date==date))
    if row:row.weight_kg=75+n*.1
    else:db.add(Checkin(user_id=user.id,date=date,weight_kg=75+n*.1,note='合成打卡'))
   for n in range(18):db.add(Memory(user_id=user.id,kind='long_term',category='like',key='ui-food-'+str(n),value='合成偏好 '+str(n+1),source='user_edit',status='active'))
   db.commit()
   previous=workout.start_session(db,user,DAY-dt.timedelta(days=1))
   workout_records.finish(db,user,previous.id,FinishSession(expected_revision=workout_records.revision(previous),completion_basis='confirm_current_plan'))
   db.commit();sid=workout.start_session(db,user,DAY).id
  Path('/tmp/workspace-ui-auth.json').write_text(json.dumps({**env.headers,'session_id':sid,'date':str(DAY)}))
  @env.app.get('/api/v1/system/status')
  def status():return {'data':{'paused':False}}
  @env.app.get('/__ui_evidence')
  def evidence():
   with env.factory() as db:
    s=db.get(WorkoutSession,sid)
    return {'provider':'none','model_calls':len(env.client.calls),'session_id':sid,'session':workout.session_payload(s),'memories':len(list(db.scalars(select(Memory)))),'logs':len(list(db.scalars(select(WorkoutLog))))}
  env.app.mount('/',StaticFiles(directory='/tmp/workspace-polish-h5',html=True),name='ui')
  try:uvicorn.run(env.app,host='127.0.0.1',port=5501)
  finally:env.close()
if __name__=='__main__':main()
