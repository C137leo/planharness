"""Temporary, synthetic-only browser environment; never a real user database."""
import sys,json,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.test_day_revision_chat import state,load,create,dinner,restore_to_three
from tests.test_semantic_binding import task
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.evals.dialogue_runner import DAY
from starlette.staticfiles import StaticFiles
import uvicorn
with tempfile.TemporaryDirectory(prefix='day-revision-ui-') as tmp:
    fixture=state.__wrapped__(Path(tmp));env=next(fixture);env.app.include_router(auth_router);env.app.include_router(router)
    model=env.client.client
    model.responses.extend([load,create,
        lambda d:{'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],'execute_revision':False,'keep_task_ids':[],'edit_task_ids':['apply-day-revision'],'new_request_text':'保留训练草案，晚餐三文鱼210克和米饭150克，先预览'},load,
        lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(next(o['id'] for o in d['snapshot']['objects'] if o['type']=='day_revision'),'day_plan.update_revision',expected_revision=next(o['etag'] for o in d['snapshot']['objects'] if o['type']=='day_revision'),proposal=dinner(210,'三文鱼')).model_dump()]},
        lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(next(o['id'] for o in d['snapshot']['objects'] if o['type']=='day'),'context.inspect').model_dump()]}])
    Path('/tmp/day-revision-ui-auth.json').write_text(json.dumps(env.headers))
    @env.app.get('/api/v1/system/status')
    def status():return {'data':{'paused':False}}
    @env.app.post('/__synthetic_restore')
    def restore():restore_to_three(env);return {'synthetic':True}
    @env.app.get('/__synthetic_evidence')
    def evidence():return env.snapshot()
    env.app.mount('/',StaticFiles(directory='/tmp/workout-progress-h5',html=True),name='ui')
    try:uvicorn.run(env.app,host='127.0.0.1',port=5196)
    finally:
        try:next(fixture)
        except StopIteration:pass
