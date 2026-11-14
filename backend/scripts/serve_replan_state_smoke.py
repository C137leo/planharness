"""Synthetic UI: a planner attempts an expired approval, then natural confirmation."""
import sys,json,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.test_workout_progress import progress_chat
from tests.test_workout_replan import legacy_finished,replacement_plan
from tests.test_replan_authorization_state import old_approval,direct_proposal,natural_confirm
from tests.test_day_revision_chat import load
from app.api.auth_routes import router as auth_router
from app.api.routes import router
from starlette.staticfiles import StaticFiles
import uvicorn
with tempfile.TemporaryDirectory(prefix='replan-state-ui-') as temp:
    fixture=progress_chat.__wrapped__(Path(temp));env,model,sid,post=next(fixture);legacy_finished(env,sid)
    old=old_approval(env)
    env.app.include_router(auth_router);env.app.include_router(router)
    model.responses.extend([load,lambda d:direct_proposal(d,old['authorization_id']),natural_confirm,replacement_plan])
    Path('/tmp/replan-state-ui-auth.json').write_text(json.dumps(env.headers))
    Path('/tmp/replan-state-ui-case.json').write_text(json.dumps(old))
    @env.app.get('/api/v1/system/status')
    def status():return {'data':{'paused':False}}
    @env.app.get('/__synthetic_evidence')
    def evidence():return env.snapshot()
    env.app.mount('/',StaticFiles(directory='/tmp/workout-progress-h5',html=True),name='ui')
    try:uvicorn.run(env.app,host='127.0.0.1',port=5199)
    finally:
        try:next(fixture)
        except StopIteration:pass
