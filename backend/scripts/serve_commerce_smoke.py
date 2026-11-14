"""Manual local browser fixture: scripted model, Mock MCP, temporary DB. Not production.
Build H5 into /tmp/planharness-commerce-chat-h5, then run this script.
"""
import sys,json,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.evals.dialogue_runner import DialogueEnvironment
from app.api.coach_routes import coach_commerce_dependency
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.domain.commerce import CommerceProvider
from tests.test_commerce_checkout import FakeMcp
from tests.test_commerce_chat import make_plan
from tests.test_semantic_binding import Scripted
from starlette.staticfiles import StaticFiles
import uvicorn
fake=FakeMcp()
def prepare(d):
    product=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_product' and o['name']=='麦辣鸡翅')
    return make_plan(d,'commerce.prepare_order','commerce_store',items=[{'product_id':product['id'],'quantity':1}])
model=Scripted([
    lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'mode':'load_context','context_request':{'commerce':True}},
    lambda d:{**make_plan(d,'commerce.search_stores','commerce_account',city='北京',keyword='三里屯'),'continue_after_tools':True},
    lambda d:make_plan(d,'commerce.menu','commerce_store'),prepare])
tmp=tempfile.TemporaryDirectory(prefix='commerce-ui-mock-')
env=DialogueEnvironment(Path(tmp.name)/'state.sqlite','ui-mock',model)
env.app.dependency_overrides[coach_commerce_dependency]=lambda:CommerceProvider('ui-mock',lambda:fake,True)
env.app.include_router(auth_router);env.app.include_router(router)
Path('/tmp/commerce-ui-auth.json').write_text(json.dumps(env.headers))
@env.app.get('/api/v1/system/status')
def status():return {'data':{'paused':False}}
@env.app.get('/__mock_evidence')
def evidence():return {'provider':'mock','model':'scripted','submits':fake.submits,'calls':fake.calls}
env.app.mount('/',StaticFiles(directory='/tmp/planharness-commerce-chat-h5',html=True),name='ui')
try:uvicorn.run(env.app,host='127.0.0.1',port=5189)
finally:env.close();tmp.cleanup()
