"""Temporary synthetic UI only: misleading historical flags and safe replacement."""
import sys,json,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.test_workout_replan import progress_chat,legacy_finished,request_plan,replacement_plan
from tests.test_day_revision_chat import load
from tests.test_semantic_binding import task
from app.evals.dialogue_runner import DAY
from app.api.auth_routes import router as auth_router
from app.api.routes import router
from starlette.staticfiles import StaticFiles
import uvicorn
with tempfile.TemporaryDirectory(prefix='workout-replan-ui-') as tmp:
    fixture=progress_chat.__wrapped__(Path(tmp));env,model,sid,post=next(fixture);legacy_finished(env,sid)
    env.app.include_router(auth_router);env.app.include_router(router)
    model.responses.extend([
        lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(next(o['id'] for o in d['snapshot']['objects'] if o['type']=='day'),'context.inspect').model_dump()]},
        load,request_plan,load,request_plan,replacement_plan])
    Path('/tmp/workout-replan-ui-auth.json').write_text(json.dumps(env.headers))
    @env.app.get('/api/v1/system/status')
    def status():return {'data':{'paused':False}}
    @env.app.get('/__synthetic_evidence')
    def evidence():return env.snapshot()
    env.app.mount('/',StaticFiles(directory='/tmp/workout-progress-h5',html=True),name='ui')
    try:uvicorn.run(env.app,host='127.0.0.1',port=5197)
    finally:
        try:next(fixture)
        except StopIteration:pass
