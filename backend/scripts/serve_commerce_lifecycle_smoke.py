"""Synthetic native UI acceptance. No real MCP, user data or financial writes."""
import sys,json,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.evals.dialogue_runner import DialogueEnvironment
from app.api.coach_routes import coach_commerce_dependency
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.domain.commerce import CommerceProvider
from scripts.eval_commerce_lifecycle import LifecycleMcp,initialize
from tests.test_semantic_binding import Scripted
from tests.test_commerce_chat import make_plan
from starlette.staticfiles import StaticFiles
import uvicorn


def main():
    fake=LifecycleMcp();model=Scripted([]);p=CommerceProvider('synthetic-lifecycle',lambda:fake,True)
    def choose(data):
        food=next(o for o in data['snapshot']['objects'] if o['type']=='commerce_product' and o['name']=='麦辣鸡翅')
        return make_plan(data,'commerce.prepare_order','commerce_store',items=[{'product_id':food['id'],'quantity':1}])
    model.responses.extend([choose,choose])
    with tempfile.TemporaryDirectory(prefix='lifecycle-ui-') as tmp:
        env=DialogueEnvironment(Path(tmp)/'db.sqlite','lifecycle-ui',model)
        env.app.dependency_overrides[coach_commerce_dependency]=lambda:p
        initialize(env,fake,p,'unpaid')
        env.app.include_router(auth_router);env.app.include_router(router)
        Path('/tmp/commerce-lifecycle-ui-auth.json').write_text(json.dumps(env.headers))
        @env.app.get('/api/v1/system/status')
        def status():return {'data':{'paused':False}}
        @env.app.get('/__synthetic_evidence')
        def evidence():return {'provider':'fake','model':'scripted','submits':fake.submits,'cancels':len(fake.cancellations),'snapshot':env.snapshot()}
        env.app.mount('/',StaticFiles(directory='/tmp/commerce-lifecycle-h5',html=True),name='ui')
        try:uvicorn.run(env.app,host='127.0.0.1',port=5299)
        finally:env.close()

if __name__=='__main__':main()
