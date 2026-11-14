"""Order-history API with real SQLite; MCP is an explicit fake, never live writes."""
import copy,datetime as dt,hashlib
import pytest
from sqlalchemy import select
from app.models import CheckoutDraft,ExternalOperation,User,new_id,utcnow
from app.domain import commerce_history as h
from tests.test_commerce_chat import chat,prepare_chat

@pytest.fixture
def history(chat):
    env,model,fake,post=chat
    preview=prepare_chat(chat);p=preview['pending_confirmation']
    post('确认第一单',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    with env.factory() as db:
        template=copy.deepcopy(db.scalar(select(CheckoutDraft)).quote)
        cart=copy.deepcopy(db.scalar(select(CheckoutDraft)).cart)
        db.add(User(id='foreign-history-user',nickname='foreign'));db.commit()
    def add(status='1',age=0,uid=None,account='test-account',failed=False,unknown=False):
        from app.seed import DEMO_USER_ID
        uid=uid or DEMO_USER_ID;id=new_id();remote='history-'+id
        created=utcnow()-dt.timedelta(minutes=age)
        order={'order_id':remote,'status_code':status,'status':{'1':'待支付','2':'已支付','7':'已取消'}.get(status,'待核实'),
            'status_verified':not unknown,'verified':not unknown,'created_at':created.isoformat(),'observed_at':utcnow().isoformat(),'pay_url':'https://www.mcd.cn/pay?orderId='+remote}
        with env.factory() as db:
            base=db.scalar(select(CheckoutDraft));op=ExternalOperation(id=new_id(),user_id=uid,provider='mcd:'+account[:16],capability_id='commerce.submit_order',
                request_key='checkout:'+id,payload_hash='a'*64,status='failed' if failed else 'unknown' if unknown else 'succeeded',remote_id=None if unknown or failed else remote,result=order,created_at=created)
            row=CheckoutDraft(id=id,user_id=uid,thread='another-chat',account_key=account,store_id=base.store_id,request_key='fixture-'+id,
                status='failed' if failed else 'unknown' if unknown else 'submitted',cart=copy.deepcopy(cart),quote={**copy.deepcopy(template),'order':order},take_way_code='locker-out',expires_at=created,created_at=created,operation_id=op.id)
            db.add(op);db.add(row);db.commit()
        fake.orders[remote]={'orderId':remote,'orderStatus':status,'storeCode':'store1','takeWayCode':'locker-out','totalAmount':'10.0','orderProductList':[x['official'] for x in cart]}
        return 'checkout:'+id,remote
    return env,fake,add


def get(env,path,headers=None):
    response=env.http.get('/api/v1/agent/commerce/orders'+path,headers=headers or env.headers)
    assert response.status_code==200,response.text
    return response.json()['data']


def test_categories_expiry_paid_history_and_unknown_not_expired(history):
    env,fake,add=history
    expired,_=add(age=16);paid,_=add('2',age=2000);cancel,_=add('7');unknown,_=add(age=3000,unknown=True);failed,_=add(failed=True)
    active=get(env,'?group=active');assert active['counts']=={'active':1,'paid':1,'inactive':3,'unknown':1}
    assert {x['id'] for x in get(env,'?group=inactive')['items']}=={expired,cancel,failed}
    assert get(env,'?group=paid')['items'][0]['id']==paid
    assert get(env,'?group=unknown')['items'][0]['id']==unknown
    assert fake.submits==1


def test_unsubmitted_quote_and_cancelled_quote_are_not_real_order_history(chat):
    env,model,fake,post=chat;preview=prepare_chat(chat)
    assert get(env,'')['items']==[]
    post('取消草案',checkout_action={'action':'cancel','checkout_id':preview['checkout']['id'],'version':preview['checkout']['version']})
    assert get(env,'')['counts']=={'active':0,'paid':0,'inactive':0,'unknown':0} and fake.submits==0


def test_history_is_user_scoped_and_cross_thread_but_other_credentials_cannot_refresh(history):
    env,fake,add=history;foreign,_=add(uid='foreign-history-user');other,_=add(account='other-account');own,_=add()
    values=get(env,'')['items'];assert own in {x['id'] for x in values} and foreign not in {x['id'] for x in values}
    assert next(x for x in values if x['id']==other)['can_refresh'] is False
    start=len(fake.calls)
    assert env.http.get('/api/v1/agent/commerce/orders/'+foreign,headers=env.headers).status_code==404
    assert env.http.post('/api/v1/agent/commerce/orders/'+foreign+'/refresh',headers=env.headers).status_code==404
    assert env.http.post('/api/v1/agent/commerce/orders/'+other+'/refresh',headers=env.headers).status_code==422
    assert len(fake.calls)==start


def test_reads_work_without_mcp_token_but_refresh_requires_connection(history):
    from app.api.coach_routes import coach_commerce_dependency
    env,fake,add=history;env.app.dependency_overrides[coach_commerce_dependency]=lambda:None
    values=get(env,'')['items'];assert len(values)==1 and values[0]['can_refresh'] is False
    assert env.http.post('/api/v1/agent/commerce/orders/'+values[0]['id']+'/refresh',headers=env.headers).status_code==422
    assert env.http.get('/api/v1/agent/commerce/orders').status_code==401


def test_pagination_is_bounded_stable_and_cursor_scoped(history):
    env,fake,add=history
    for _ in range(24):add('2')
    first=get(env,'?group=paid&limit=10');second=get(env,'?group=paid&limit=10&cursor='+first['next_cursor']);third=get(env,'?group=paid&limit=10&cursor='+second['next_cursor'])
    ids=[x['id'] for page in [first,second,third] for x in page['items']]
    assert len(ids)==len(set(ids))==24 and third['next_cursor']==''
    for path in ['?limit=0','?limit=51','?cursor=bad','?group=active&cursor='+first['next_cursor']]:
        assert env.http.get('/api/v1/agent/commerce/orders'+path,headers=env.headers).status_code==422


def test_refresh_moves_paid_order_without_touching_chat_plan_or_creating_orders(history):
    env,fake,add=history;values=get(env,'')['items'];id=values[0]['id'];fake.orders['order-1']['orderStatus']='2'
    messages=env.history();before=env.snapshot()['days'];count=fake.submits
    result=env.http.post('/api/v1/agent/commerce/orders/'+id+'/refresh',headers=env.headers)
    assert result.status_code==200 and result.json()['data']['group']=='paid',result.text
    assert get(env,'?group=paid')['counts']['active']==0
    assert env.history()==messages and env.snapshot()['days']==before and fake.submits==count
    assert not any(name in {'create-order','cancel-order'} for name,args in fake.calls)


def test_failed_query_preserves_known_receipt_and_reports_sync_failure(history):
    env,fake,add=history;id=get(env,'')['items'][0]['id'];fake.query_failure=True
    response=env.http.post('/api/v1/agent/commerce/orders/'+id+'/refresh',headers=env.headers)
    assert response.status_code==422 and get(env,'')['counts']['active']==1 and fake.submits==1


def test_sync_caps_queries_and_never_dispatches(history):
    env,fake,add=history
    for _ in range(12):add()
    start=len(fake.calls);response=env.http.post('/api/v1/agent/commerce/orders/sync',headers=env.headers)
    assert response.status_code==200,response.text
    value=response.json()['data'];assert value['checked']==10 and value['remaining']==3 and value['failed']==0
    assert len(fake.calls[start:])==10 and fake.submits==1


def test_legacy_audit_orders_are_visible_and_refresh_does_not_rewrite_audit(history):
    from app.audit import append_event
    from app.seed import DEMO_USER_ID
    from app.models import AuditEvent
    env,fake,add=history;token='synthetic-history-token';legacy_key=hashlib.sha256(token.encode()).hexdigest();headers={**env.headers,'X-MCD-Token':token}
    with env.factory() as db:
        row=append_event(db,DEMO_USER_ID,'mcd_order_created',{'order_id':'legacy-1','total':15,'store_name':'旧版餐厅','status':'待支付','account_key':legacy_key},actor_type='user',actor_id=DEMO_USER_ID)
        db.commit();id='legacy:'+row.id;original=copy.deepcopy(row.payload);original_hash=row.event_hash if hasattr(row,'event_hash') else row.hash
    value=next(x for x in get(env,'',headers)['items'] if x['id']==id)
    assert value['source']=='legacy' and value['details_complete'] is False
    fake.orders['legacy-1']={'orderId':'legacy-1','orderStatus':'2','totalAmount':'15','orderProductList':[{'productName':'旧版鸡翅','quantity':1}]}
    refreshed=env.http.post('/api/v1/agent/commerce/orders/'+id+'/refresh',headers=headers)
    assert refreshed.status_code==200 and refreshed.json()['data']['group']=='paid',refreshed.text
    assert refreshed.json()['data']['items'][0]['name']=='旧版鸡翅'
    with env.factory() as db:
        saved=db.get(AuditEvent,id.split(':')[1]);assert saved.payload==original
        assert (saved.event_hash if hasattr(saved,'event_hash') else saved.hash)==original_hash


def test_history_never_exposes_account_fingerprints_or_unsafe_pay_url(history):
    env,fake,add=history;id,_=add()
    with env.factory() as db:
        row=db.get(CheckoutDraft,id.split(':')[1]);row.quote={**row.quote,'order':{**row.quote['order'],'pay_url':'https://evil.example/pay'}};db.commit()
    value=get(env,'/'+id)
    assert value['order']['pay_url']=='' and 'account_key' not in str(value) and 'payload_hash' not in str(value)


def test_unknown_without_remote_id_reports_no_query_target_and_never_expires(history):
    env,fake,add=history;id,_=add(age=9999,unknown=True);start=len(fake.calls)
    response=env.http.post('/api/v1/agent/commerce/orders/'+id+'/refresh',headers=env.headers)
    assert response.status_code==422 and '未返回可查询' in response.text
    assert get(env,'/'+id)['group']=='unknown' and len(fake.calls)==start


def test_matching_legacy_and_unified_remote_order_are_not_listed_twice(history):
    from app.audit import append_event
    from app.seed import DEMO_USER_ID
    from app.domain.commerce import CommerceProvider
    env,fake,add=history;legacy_key='l'*64
    with env.factory() as db:
        append_event(db,DEMO_USER_ID,'mcd_order_created',{'order_id':'order-1','total':10,'store_name':'重复旧记录','status':'待支付','account_key':legacy_key},actor_type='user',actor_id=DEMO_USER_ID);db.commit()
        values=h.records(db,DEMO_USER_ID,CommerceProvider('test-account',lambda:fake,True),legacy_key)
        assert len(values)==1 and values[0]['source']=='chat'


def test_old_cancel_audit_is_archived_and_missing_details_are_not_fabricated(history):
    from app.audit import append_event
    from app.seed import DEMO_USER_ID
    env,fake,add=history
    with env.factory() as db:
        row=append_event(db,DEMO_USER_ID,'mcd_order_created',{'order_id':'cancelled-legacy','total':10,'store_name':'旧店','status':'待支付'},actor_type='user',actor_id=DEMO_USER_ID)
        append_event(db,DEMO_USER_ID,'mcd_order_cancelled',{'order_id':'cancelled-legacy'},actor_type='user',actor_id=DEMO_USER_ID);db.commit();id='legacy:'+row.id
    value=get(env,'/'+id);assert value['group']=='inactive' and value['status_label']=='已取消' and value['items']==[]
