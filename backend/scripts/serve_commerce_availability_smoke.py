"""Synthetic UI acceptance: closure consent and stock recovery, no live MCP."""
import sys,json,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.evals.dialogue_runner import DialogueEnvironment
from app.api.coach_routes import coach_commerce_dependency
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.domain.commerce import CommerceProvider,save_pickup_preferences
from app.seed import DEMO_USER_ID
from scripts.eval_commerce_availability import AvailabilityMcp
from tests.test_semantic_binding import Scripted,task
from tests.test_commerce_chat import make_plan
from starlette.staticfiles import StaticFiles
import uvicorn


def main():
    fake=AvailabilityMcp('nearest_closed');model=Scripted([]);p=CommerceProvider('synthetic-availability',lambda:fake,True)
    def search(d):return {**make_plan(d,'commerce.search_stores','commerce_account',city='北京',keyword='地标',selection_policy='nearest'),'continue_after_tools':True}
    def menu(d):
        root=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_account')
        return {'snapshot_id':d['snapshot']['snapshot_id'],'continue_after_tools':True,'tasks':[task(root['store_search']['nearest_open_id'],'commerce.menu').model_dump()]}
    def quote(d):
        root=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_account');id=root['store_search']['nearest_open_id']
        food=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_product' and o['store_id']==id and o['name']=='麦辣鸡翅2块')
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(id,'commerce.prepare_order',items=[{'product_id':food['id'],'quantity':1}]).model_dump()]}
    def new_request(d):return {'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],'new_request_text':d['user_text'],'execute_revision':False}
    model.responses.extend([search,menu,quote,new_request,search,menu,quote])
    with tempfile.TemporaryDirectory(prefix='availability-ui-') as tmp:
        env=DialogueEnvironment(Path(tmp)/'db.sqlite','availability-ui',model);env.app.dependency_overrides[coach_commerce_dependency]=lambda:p
        with env.factory() as db:save_pickup_preferences(db,DEMO_USER_ID,'北京','地标');db.commit()
        env.app.include_router(auth_router);env.app.include_router(router)
        Path('/tmp/commerce-availability-ui-auth.json').write_text(json.dumps(env.headers))
        @env.app.get('/api/v1/system/status')
        def status():return {'data':{'paused':False}}
        @env.app.get('/__synthetic_evidence')
        def evidence():return {'provider':'fake','model':'scripted','submits':fake.submits,'calls':fake.calls,'snapshot':env.snapshot()}
        @env.app.post('/__synthetic_stock_event')
        def stock():fake.fixture='stock_rejected';return {'inventory_event':'nearest opens, wings sold out; synthetic only'}
        env.app.mount('/',StaticFiles(directory='/tmp/commerce-availability-h5',html=True),name='ui')
        try:uvicorn.run(env.app,host='127.0.0.1',port=5399)
        finally:env.close()

if __name__=='__main__':main()
