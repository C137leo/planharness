"""Isolated UI fixture: synthetic demo, Scripted model, Mock MCP; never a real order."""
import sys,json,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.evals.dialogue_runner import DialogueEnvironment,DAY
from app.api.coach_routes import coach_commerce_dependency
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.domain.commerce import CommerceProvider
from app.domain import workout
from app.models import User,WorkoutLog,WorkoutRecordRevision
from app.seed import DEMO_USER_ID
from tests.test_commerce_checkout import FakeMcp
from tests.test_semantic_binding import Scripted,task
from tests.test_workout_records import finish_task
from starlette.staticfiles import StaticFiles
from sqlalchemy import select
import uvicorn

def confirm(d):return {'action':'confirm','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],'execute_revision':False}
def correct(d):
    s=next(o for o in d['snapshot']['objects'] if o['type']=='session')
    item=next(o for o in d['snapshot']['objects'] if o['type']=='session_item' and o.get('completed_sets')==3)
    return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(item['id'],'workout.correct_record',expected_revision=s['record_revision'],set_no=3,weight_kg=65,reason='用户纠正末组重量').model_dump()]}

def main():
    model=Scripted([finish_task,confirm,correct,confirm]);fake=FakeMcp();state={'enabled':True}
    with tempfile.TemporaryDirectory(prefix='training-token-ui-') as tmp:
        env=DialogueEnvironment(Path(tmp)/'state.db','training-ui',model)
        env.app.dependency_overrides[coach_commerce_dependency]=lambda:CommerceProvider('mock-ui',lambda:fake,True) if state['enabled'] else None
        env.app.include_router(auth_router);env.app.include_router(router)
        with env.factory() as db:
            user=db.get(User,DEMO_USER_ID);s=workout.start_session(db,user,DAY)
            for reps,weight in [(8,50),(9,55),(10,60)]:workout.complete_set(db,user,s.id,reps=reps,weight_kg=weight)
        Path('/tmp/training-token-ui-auth.json').write_text(json.dumps(env.headers))
        @env.app.get('/api/v1/system/status')
        def status():return {'data':{'paused':False}}
        @env.app.post('/__mock_connection/{enabled}')
        def connection(enabled:int):state['enabled']=bool(enabled);return state
        @env.app.get('/__mock_evidence')
        def evidence():
            with env.factory() as db:
                logs=list(db.scalars(select(WorkoutLog)));audits=list(db.scalars(select(WorkoutRecordRevision)))
                return {'model':'scripted','provider':'mock','model_calls':len(model.calls),'submits':fake.submits,'logs':len(logs),'audits':len(audits),
                    'sets':[l.sets for l in logs],'last_weight':(logs[0].details['set_logs'][-1]['weight_kg'] if logs else None)}
        env.app.mount('/',StaticFiles(directory='/tmp/workout-stage4-h5',html=True),name='ui')
        try:uvicorn.run(env.app,host='127.0.0.1',port=5193)
        finally:env.close()

if __name__=='__main__':main()
