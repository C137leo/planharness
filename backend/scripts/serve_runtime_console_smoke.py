"""Synthetic runtime console fixture, real API and SQLite, no external model/MCP."""
import sys,tempfile,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.evals.dialogue_runner import DialogueEnvironment
from tests.test_semantic_binding import Scripted,binding_plan,request
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.api.coach_routes import coach_commerce_dependency
from starlette.staticfiles import StaticFiles
import uvicorn
with tempfile.TemporaryDirectory(prefix='runtime-console-ui-') as tmp:
 e=DialogueEnvironment(Path(tmp)/'state.db','console-ui',Scripted([binding_plan(['深蹲','划船'])]))
 e.app.include_router(router);e.app.include_router(auth_router)
 e.app.dependency_overrides[coach_commerce_dependency]=lambda:None
 result=request(e,'先预览移除深蹲和划船',e.client)
 Path('/tmp/runtime-console-auth.json').write_text(json.dumps(e.headers))
 @e.app.get('/api/v1/system/status')
 def status():return {'data':{'paused':False}}
 e.app.mount('/',StaticFiles(directory='/tmp/console-runtime-h5',html=True))
 try:uvicorn.run(e.app,host='127.0.0.1',port=5502)
 finally:e.close()
