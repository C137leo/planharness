"""Isolated branded UI fixture. Scripted model, Mock MCP, temp DB; no real orders."""
import sys,json,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.evals.dialogue_runner import DialogueEnvironment
from app.api.coach_routes import coach_commerce_dependency
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.domain.commerce import CommerceProvider
from tests.test_commerce_checkout import FakeMcp,TOOLS
from tests.test_semantic_binding import Scripted
from starlette.staticfiles import StaticFiles
import uvicorn

class UiMcp(FakeMcp):
    def discover(self):return {n:{} for n in TOOLS|{'list-nutrition-foods'}}
    def call_read(self,name,args):
        if name=='list-nutrition-foods':return json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/mcd-nutrition-readonly.json').read_text())['data']
        if name=='query-nearby-stores':return [
            {'storeCode':'store1','storeName':'测试 · 广场餐厅','address':'测试地址 · 地铁站 A 出口旁','businessStatus':True,'distance':235,'businessStartTime':'00:00','businessEndTime':'23:59'},
            {'storeCode':'store2','storeName':'测试 · 福州路餐厅','address':'测试地址 · 福州路沿街','businessStatus':True,'distance':412,'businessStartTime':'07:00','businessEndTime':'23:00'},
            {'storeCode':'store3','storeName':'测试 · 商场餐厅','address':'测试地址 · 商场一层','businessStatus':False,'distance':628,'businessStartTime':'10:00','businessEndTime':'22:00'}]
        if name=='query-meals':return {'meals':{
            'wings':{'name':'麦辣鸡翅-2块','currentPrice':'15','productType':1},
            'combo':{'name':'牛肉汉堡套餐','currentPrice':'34','productType':2}}}
        return super().call_read(name,args)
    def submit_pickup(self,args):
        r=super().submit_pickup(args)
        self.orders[r['orderId']]['storeName']='测试 · 广场餐厅'
        return {**r,'payH5Url':'https://m.mcd.cn/mcp/scanToPay?orderId='+r['orderId']}


def main():
    fake=UiMcp();model=Scripted([])
    with tempfile.TemporaryDirectory(prefix='commerce-ui-fixture-') as tmp:
        env=DialogueEnvironment(Path(tmp)/'db.sqlite','ui-redesign',model)
        state={'enabled':True}
        env.app.dependency_overrides[coach_commerce_dependency]=lambda:CommerceProvider('ui-test-account',lambda:fake,True) if state['enabled'] else None
        env.app.include_router(auth_router);env.app.include_router(router)
        Path('/tmp/commerce-ui-redesign-auth.json').write_text(json.dumps(env.headers))
        @env.app.get('/api/v1/system/status')
        def status():return {'data':{'paused':False}}
        @env.app.get('/__mock_evidence')
        def evidence():return {'provider':'mock','model':'scripted','submits':fake.submits,'model_calls':len(model.calls)}
        @env.app.post('/__mock_connection/{enabled}')
        def connection(enabled:int):state['enabled']=bool(enabled);return {'enabled':state['enabled']}
        env.app.mount('/',StaticFiles(directory='/tmp/planharness-commerce-ui-h5',html=True),name='ui')
        try:uvicorn.run(env.app,host='127.0.0.1',port=5191)
        finally:env.close()
if __name__=='__main__':main()
