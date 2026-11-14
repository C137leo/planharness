"""Isolated synthetic order-history UI, no real credentials or external writes."""
import sys,json,tempfile,datetime as dt,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.evals.dialogue_runner import DialogueEnvironment
from app.api.coach_routes import coach_commerce_dependency
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.domain import commerce as c
from app.models import CheckoutDraft,ExternalOperation,new_id,utcnow
from app.audit import append_event
from app.seed import DEMO_USER_ID
from tests.test_commerce_checkout import FakeMcp
from tests.test_semantic_binding import Scripted
from starlette.staticfiles import StaticFiles
import uvicorn


def seed_history(env,fake,p):
    with env.factory() as db:
        store=c.object_row(db,DEMO_USER_ID,'history-ui',p.account_key,'commerce_store','store1',payload={
            'provider_code':'store1','name':'城市花园餐厅 · 测试门店','address':'花园路 18 号 · 合成地址','open':True})
        quote={'total_cents':1500,'store':store.payload,'currency':'CNY','take_ways':[{'code':'locker-out','title':'外带'}]}
        basic=[{'product_id':'wings','name':'麦辣鸡翅2块','quantity':1,'specifications':[],'official':{'productCode':'wings','quantity':1}}]
        def add(status,age=0,long=False,unknown=False,failed=False):
            id=new_id();remote='TEST-ORDER-'+id[:8].upper();created=utcnow()-dt.timedelta(minutes=age)
            cart=copy.deepcopy(basic)
            if long:cart=[{'product_id':'sku-'+str(n),'name':['麦辣鸡翅2块','牛肉汉堡','热美式中杯'][n] if n<3 else '合成餐品 '+str(n+1),
                'quantity':1,'specifications':[{'name':'标准份量','quantity':1},{'name':'默认搭配','quantity':1}],
                'official':{'productCode':'sku-'+str(n),'quantity':1}} for n in range(20)]
            total=15800 if long else 1500
            receipt={'order_id':None if unknown or failed else remote,'status_code':status,'status':{'1':'待支付','2':'已支付，配餐中','6':'已完成','7':'已取消'}.get(status,'待核实'),
                'status_verified':not unknown,'verified':not unknown,'created_at':created.isoformat(),'observed_at':utcnow().isoformat(),
                'expire_at':(created+dt.timedelta(minutes=15)).isoformat(),'pay_url':'' if unknown or failed else 'https://www.mcd.cn/pay?orderId='+remote}
            op=ExternalOperation(id=new_id(),user_id=DEMO_USER_ID,provider='mcd:'+p.account_key[:16],capability_id='commerce.submit_order',request_key='checkout:'+id,
                payload_hash='c'*64,status='failed' if failed else 'unknown' if unknown else 'succeeded',remote_id=None if unknown or failed else remote,result=receipt,created_at=created)
            db.add(op);db.add(CheckoutDraft(id=id,user_id=DEMO_USER_ID,thread='saved-order-chat',account_key=p.account_key,store_id=store.id,request_key='history-fixture:'+id,
                status='failed' if failed else 'unknown' if unknown else 'submitted',cart=cart,quote={**copy.deepcopy(quote),'total_cents':total,'order':receipt},take_way_code='locker-out',expires_at=created,created_at=created,operation_id=op.id))
            fake.orders[remote]={'orderId':remote,'orderStatus':status,'storeCode':'store1','takeWayCode':'locker-out','totalAmount':str(total/100),
                'orderProductList':[{**x['official'],'productName':x['name'],'specifications':x['specifications']} for x in cart]}
        add('1',3);add('1',6)
        for n in range(31):add('2' if n==0 else '6',40+n,long=n==0)
        for n in range(8):add('1',100+n)
        add('7',95);add('',999,unknown=True);add('',98,failed=True)
        draft=CheckoutDraft(id=new_id(),user_id=DEMO_USER_ID,thread='current-chat',account_key=p.account_key,store_id=store.id,request_key='unsubmitted-fixture',status='awaiting_confirmation',cart=copy.deepcopy(basic),quote=copy.deepcopy(quote),take_way_code='locker-out',expires_at=utcnow()+dt.timedelta(minutes=5))
        db.add(draft)
        append_event(db,DEMO_USER_ID,'mcd_order_created',{'order_id':'LEGACY-TEST-001','total':29,'store_name':'旧版餐厅 · 测试记录','status':'已支付','account_key':'other-connection'},actor_type='user',actor_id=DEMO_USER_ID)
        db.commit();return draft.id


def main():
    fake=FakeMcp();model=Scripted([]);p=c.CommerceProvider('history-ui-synthetic',lambda:fake,True)
    with tempfile.TemporaryDirectory(prefix='order-history-ui-') as tmp:
        env=DialogueEnvironment(Path(tmp)/'history.sqlite','history-ui',model);env.app.dependency_overrides[coach_commerce_dependency]=lambda:p
        draft_id=seed_history(env,fake,p);env.app.include_router(auth_router);env.app.include_router(router)
        Path('/tmp/commerce-history-ui-auth.json').write_text(json.dumps(env.headers))
        @env.app.get('/api/v1/system/status')
        def status():return {'data':{'paused':False}}
        @env.app.get('/__synthetic_evidence')
        def evidence():
            with env.factory() as db:
                draft=db.get(CheckoutDraft,draft_id);unchanged={'id':draft.id,'status':draft.status,'cart':draft.cart,'quote':draft.quote,'version':draft.version}
            return {'provider':'fake MCP','model_calls':len(model.calls),'submits':fake.submits,'calls':fake.calls,'draft':unchanged,'history':env.history(),'days':env.snapshot()['days']}
        env.app.mount('/',StaticFiles(directory='/tmp/commerce-history-h5',html=True),name='ui')
        try:uvicorn.run(env.app,host='127.0.0.1',port=5499)
        finally:env.close()

if __name__=='__main__':main()
