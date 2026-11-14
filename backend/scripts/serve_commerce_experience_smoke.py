"""Synthetic 120-item menu and selected wings UI. No real MCP or orders."""
import sys,json,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.evals.dialogue_runner import DialogueEnvironment
from app.api.coach_routes import coach_commerce_dependency
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.domain.commerce import CommerceProvider,pickup_preferences
from app.seed import DEMO_USER_ID
from tests.test_commerce_checkout import FakeMcp,TOOLS
from tests.test_semantic_binding import Scripted
from tests.test_commerce_chat import make_plan
from starlette.staticfiles import StaticFiles
import uvicorn


class ExperienceMcp(FakeMcp):
    def call_read(self,name,args):
        if name=='query-meals':
            self.calls.append((name,args))
            return {'meals':{'wings':{'name':'麦辣鸡翅-2块','currentPrice':'10','productType':1},**{
                'side'+str(i):{'name':'测试餐品 '+str(i),'currentPrice':'12','productType':1} for i in range(1,120)}}}
        if name=='query-meal-detail':
            return {'code':args['code'],'name':'麦辣鸡翅-2块' if args['code']=='wings' else '测试餐品 '+args['code'][4:],'rounds':[]}
        return super().call_read(name,args)


def main():
    fake=ExperienceMcp();model=Scripted([])
    def choose(d):
        food=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_product' and o['name']=='麦辣鸡翅-2块')
        return make_plan(d,'commerce.prepare_order','commerce_store',items=[{'product_id':food['id'],'quantity':1}])
    model.responses.append(choose)
    with tempfile.TemporaryDirectory(prefix='commerce-experience-ui-') as tmp:
        env=DialogueEnvironment(Path(tmp)/'db.sqlite','commerce-experience-ui',model)
        env.app.dependency_overrides[coach_commerce_dependency]=lambda:CommerceProvider('ui-synthetic',lambda:fake,True)
        env.app.include_router(auth_router);env.app.include_router(router)
        Path('/tmp/commerce-experience-ui-auth.json').write_text(json.dumps(env.headers))
        @env.app.get('/api/v1/system/status')
        def status():return {'data':{'paused':False}}
        @env.app.get('/__synthetic_evidence')
        def evidence():
            with env.factory() as db:location=pickup_preferences(db,DEMO_USER_ID)
            return {'provider':'mock','model':'scripted','submits':fake.submits,'model_calls':len(model.calls),'location':location,'snapshot':env.snapshot()}
        env.app.mount('/',StaticFiles(directory='/tmp/commerce-experience-h5',html=True),name='ui')
        try:uvicorn.run(env.app,host='127.0.0.1',port=5198)
        finally:env.close()


if __name__=='__main__':main()
