"""Synthetic UI reproduction: finished 7 actual sets / legacy 10 targets, dated 7 targets."""
import sys,json,tempfile,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select,delete
from app.evals.dialogue_runner import DialogueEnvironment,DAY
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.agent.workout_capabilities import FinishSession
from app.domain import workout,workout_records as records
from app.domain.day_plan import effective_day,save_override
from app.models import User,Plan,PlanVersion,MealPlan,WorkoutLog
from app.seed import DEMO_USER_ID
from tests.test_semantic_binding import Scripted
from starlette.staticfiles import StaticFiles
import uvicorn

with tempfile.TemporaryDirectory(prefix='progress-scroll-ui-') as tmp:
    env=DialogueEnvironment(Path(tmp)/'state.db','progress-scroll-ui',Scripted([]))
    env.app.include_router(auth_router);env.app.include_router(router)
    with env.factory() as db:
        user=db.get(User,DEMO_USER_ID);day=effective_day(db,user,DAY)
        actions=[{'action_ref':'dynamic_warmup','slot':'warmup','sets':1,'duration_min':5}]+[{**a,'sets':3,'duration_min':12} for a in day['actions']]
        save_override(db,user,DAY,actions=actions,meals=day['meals'],summary='合成旧会话复现',constraints={},reason='synthetic',base_version=day['plan_version']);db.commit()
        s=workout.start_session(db,user,DAY)
        records.finish(db,user,s.id,FinishSession(expected_revision=records.revision(s),completion_basis='confirm_current_plan',
            item_overrides=[{'item_id':i['id'],'actual_sets':1 if i['slot']=='warmup' else 2} for i in s.items]));db.commit()
        current=effective_day(db,user,DAY)
        save_override(db,user,DAY,actions=[{**a,'sets':1 if a['slot']=='warmup' else 2} for a in current['actions']],meals=current['meals'],summary='合成今日7组',constraints={},reason='synthetic',base_version=current['plan_version'])
        plan=db.scalar(select(Plan).where(Plan.user_id==user.id));version=db.scalar(select(PlanVersion).where(PlanVersion.plan_id==plan.id,PlanVersion.version==plan.current_version))
        db.execute(delete(MealPlan).where(MealPlan.plan_version_id==version.id))
        for weekday in range(1,8):
            for meal in current['meals']:
                db.add(MealPlan(user_id=user.id,plan_version_id=version.id,day_index=weekday,slot=meal['slot'],title=meal['title'],items=meal['items'],kcal=meal['kcal'],protein_g=meal['protein_g']))
        db.commit()
    Path('/tmp/progress-scroll-ui-auth.json').write_text(json.dumps(env.headers))
    @env.app.get('/api/v1/system/status')
    def status():return {'data':{'paused':False}}
    env.app.mount('/',StaticFiles(directory='/tmp/workout-progress-h5',html=True),name='ui')
    try:uvicorn.run(env.app,host='127.0.0.1',port=5195)
    finally:env.close()
