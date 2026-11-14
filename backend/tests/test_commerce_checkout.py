"""Commerce business tests. The provider is explicitly mocked; DB writes are real."""
import copy
import datetime as dt
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select
from app.db import Base, make_engine, make_session_factory
from app.models import User, CheckoutDraft, CommerceObject, ExternalOperation, utcnow
from app.domain import commerce as c

TOOLS={'query-nearby-stores','query-meals','query-meal-detail','calculate-price','create-order','query-order'}


class FakeMcp:
    def __init__(self):
        self.price=1000;self.submits=0;self.unavailable=False;self.timeout=False;self.query_failure=False
        self.calls=[];self.orders={};self.mutex=threading.Lock()
    def discover(self):return {name:{} for name in TOOLS}
    def close(self):pass
    def call_read(self,name,args):
        self.calls.append((name,copy.deepcopy(args)))
        if name=='query-nearby-stores':return [{'storeCode':'store1','storeName':'真实门店','address':'测试地址','businessStatus':True,'distance':100}]
        if name=='query-meals':return {'meals':{} if self.unavailable else {'wings':{'name':'麦辣鸡翅','currentPrice':'10','productType':1},'combo':{'name':'牛肉汉堡套餐','currentPrice':'10','productType':2}}}
        if name=='query-meal-detail':
            return {'code':args['code'],'name':'麦辣鸡翅' if args['code']=='wings' else '牛肉汉堡套餐','rounds':[] if args['code']=='wings' else [{'id':1,'minQuantity':1,'maxQuantity':1,'choices':[{'code':'beef','name':'牛肉汉堡','isDefault':1,'quantity':1}]}]}
        if name=='calculate-price':return {'price':self.price*sum(i['quantity'] for i in args['items']),'takeWayList':[{'code':'eat-in','title':'堂食'},{'code':'locker-out','title':'外带'}]}
        if name=='query-order':
            if self.query_failure:raise TimeoutError('query failed')
            return self.orders[args['orderId']]
        raise AssertionError(name)
    def submit_pickup(self,args):
        with self.mutex:self.submits+=1
        id='order-'+str(self.submits)
        self.orders[id]={'orderId':id,'takeWayCode':args['takeWayCode'],'orderType':1,'beType':1,'storeCode':'store1','totalAmount':str(self.price*sum(i['quantity'] for i in args['items'])/100),'orderStatus':'待支付',
                         'orderProductList':[dict(x) for x in args['items']]}
        if self.timeout:raise TimeoutError('after remote order created')
        return {'orderId':id}


@pytest.fixture
def setup(tmp_path):
    engine=make_engine('sqlite:///'+str(tmp_path/'checkout.db'));Base.metadata.create_all(engine);factory=make_session_factory(engine)
    fake=FakeMcp();p=c.CommerceProvider('account',lambda:fake,True)
    with factory() as db:
        db.add_all([User(id='u',nickname='u'),User(id='other',nickname='other')]);db.commit()
        store=c.object_row(db,'u','chat',p.account_key,'commerce_store','store1',payload={
            'provider_code':'store1','name':'真实门店','address':'测试地址','open':True,'search':{'city':'北京','keyword':'三里屯','beType':1,'searchType':2}})
        c.save_menu(db,'u','chat',p,store,fake.call_read('query-meals',{}));db.commit();store_id=store.id
        products={x.external_key:x.id for x in db.scalars(select(CommerceObject).where(CommerceObject.kind=='commerce_product'))}
    yield factory,fake,p,store_id,products
    engine.dispose()


def prepare(setup,way='locker-out',product='wings',defaults=False):
    factory,fake,p,store,products=setup
    with factory() as db:
        row=c.quote_preview(db,'u','chat',p,store,[{'product_id':products[product],'quantity':1,'use_default_options':defaults}],way,key='prepare')
        db.commit();return row.id,row.version


def submit(setup,id,version,confirmed=True):
    factory,_,p,_,_=setup
    with factory() as db:return c.submit(db,'u','chat',p,id,version,confirmed=confirmed)


def test_prepare_defaults_takeaway_but_still_requires_transaction_confirmation(setup):
    id,v=prepare(setup,way='')
    with setup[0]() as db:
        row=db.get(CheckoutDraft,id);assert row.status=='awaiting_confirmation' and row.take_way_code=='locker-out'
    with pytest.raises(c.CheckoutError):submit(setup,id,v,confirmed=False)
    assert setup[1].submits==0


def test_confirm_exact_cart_verified_and_repeated_confirmation_does_not_resubmit(setup):
    id,v=prepare(setup)
    with pytest.raises(c.CheckoutError):submit(setup,id,v,False)
    result=submit(setup,id,v)
    assert result['status']=='success',result
    assert setup[1].submits==1
    assert submit(setup,id,v)['status']=='success'
    assert setup[1].submits==1


def test_changed_price_requires_new_confirmation(setup):
    id,v=prepare(setup);setup[1].price=1200
    r=submit(setup,id,v)
    assert r['status']=='confirmation_required' and r['checkout']['version']==v+1
    assert setup[1].submits==0
    with pytest.raises(c.CheckoutError):submit(setup,id,v)
    assert submit(setup,id,v+1)['status']=='success'


def test_unavailable_product_never_creates_order(setup):
    id,v=prepare(setup);setup[1].unavailable=True
    with pytest.raises(c.CheckoutError):submit(setup,id,v)
    assert setup[1].submits==0


def test_cancel_expiry_wrong_version_and_owner(setup):
    id,v=prepare(setup);factory,_,p,_,_=setup
    with factory() as db:
        with pytest.raises(c.CheckoutError):c.draft_owned(db,'other','chat',p.account_key,id)
        c.cancel_draft(db,'u','chat',p,id,v);db.commit()
    with pytest.raises(c.CheckoutError):submit(setup,id,v)
    assert setup[1].submits==0


def test_unknown_order_is_not_resubmitted_without_remote_id(setup):
    id,v=prepare(setup);setup[1].timeout=True
    assert submit(setup,id,v)['status']=='unknown'
    assert submit(setup,id,v)['status']=='unknown'
    assert setup[1].submits==1


def test_verification_timeout_persists_remote_id_for_query_recovery(setup):
    id,v=prepare(setup);setup[1].query_failure=True
    assert submit(setup,id,v)['status']=='unknown'
    with setup[0]() as db:
        op=db.scalar(select(ExternalOperation));assert op.remote_id=='order-1'
    setup[1].query_failure=False
    assert submit(setup,id,v)['status']=='success'
    assert setup[1].submits==1


def test_foreign_product_cannot_be_bound_to_cart(setup):
    factory,fake,p,store,products=setup
    with factory() as db:
        with pytest.raises(c.CheckoutError):c.quote_preview(db,'other','chat',p,store,[{'product_id':products['wings'],'quantity':1}],'locker-out',key='attack')
    assert fake.submits==0


def test_combo_default_selection_is_visible_and_verified(setup):
    with pytest.raises(c.CheckoutError):prepare(setup,product='combo')
    id,v=prepare(setup,product='combo',defaults=True)
    with setup[0]() as db:
        row=db.get(CheckoutDraft,id);assert row.cart[0]['specifications']==[{'name':'牛肉汉堡','quantity':1}]
    assert submit(setup,id,v)['status']=='success'


def test_parallel_confirmations_never_double_dispatch(setup):
    id,v=prepare(setup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:submit(setup,id,v),range(2)))
    assert setup[1].submits==1
    assert any(r['status']=='success' for r in results)


def test_expiry_wrong_version_account_and_disabled_writes(setup):
    id,v=prepare(setup);factory,fake,p,_,_=setup
    with pytest.raises(c.CheckoutError):submit(setup,id,v+1)
    with factory() as db:
        with pytest.raises(c.CheckoutError):c.draft_owned(db,'u','chat','different-account',id)
        p.allow_orders=False
        with pytest.raises(c.CheckoutError):c.submit(db,'u','chat',p,id,v,confirmed=True)
        p.allow_orders=True
        row=db.get(CheckoutDraft,id);row.expires_at=utcnow()-dt.timedelta(seconds=1);db.commit()
    with pytest.raises(c.CheckoutError):submit(setup,id,v)
    assert fake.submits==0


def test_verified_payment_link_survives_refresh_but_external_link_is_never_exposed(setup):
    id,v=prepare(setup);factory,fake,p,_,_=setup
    original=fake.submit_pickup
    def create(args):return {**original(args),'payH5Url':'https://m.mcd.cn/pay?orderId=order-1'}
    fake.submit_pickup=create
    assert submit(setup,id,v)['order']['pay_url']=='https://m.mcd.cn/pay?orderId=order-1'
    with factory() as db:
        assert c.query_checkout(db,'u','chat',p,id)['order']['pay_url'].startswith('https://m.mcd.cn/')
    for url in ['https://mcd.cn.attacker.test/pay','javascript:alert(1)','https://user@mcd.cn/pay','https://mcd.cn:bad/pay','http://m.mcd.cn/pay']:
        assert c.official_pay_url(url)==''


def test_order_success_without_matching_remote_content_remains_unknown(setup):
    id,v=prepare(setup);fake=setup[1];original=fake.submit_pickup
    def create(args):
        result=original(args);fake.orders['order-1']['orderProductList'][0]['quantity']=99
        return {**result,'payH5Url':'https://m.mcd.cn/pay'}
    fake.submit_pickup=create
    r=submit(setup,id,v)
    assert r['status']=='unknown' and r['order']['verified'] is False
    assert r['order']['pay_url']=='https://m.mcd.cn/pay'
    submit(setup,id,v);assert fake.submits==1


def test_forged_option_is_rejected_before_price_or_submit(setup):
    factory,fake,p,store,products=setup
    with factory() as db:
        with pytest.raises(c.CheckoutError):c.quote_preview(db,'u','chat',p,store,[{
            'product_id':products['combo'],'quantity':1,'selections':[{'option_id':products['wings'],'quantity':1}]}],'locker-out',key='bad-option')
    assert fake.submits==0


def test_discovered_query_order_schema_verifies_exact_names_store_address_and_combo_items(setup):
    id,v=prepare(setup,product='combo',defaults=True);factory,fake,p,_,_=setup
    original=fake.submit_pickup
    def create(args):
        result=original(args)
        fake.orders[result['orderId']]={'orderId':result['orderId'],'storeName':'真实门店','storeAddress':'测试地址',
            'takeWay':'外带','totalAmount':'10.00','orderStatus':'1','orderProductList':[
                {'productName':'牛肉汉堡套餐','quantity':1,'comboItemList':[{'itemName':'牛肉汉堡','itemQuantity':1}]}]}
        return result
    fake.submit_pickup=create
    assert submit(setup,id,v)['status']=='success'
    fake.orders['order-1']['storeAddress']='另一地址'
    with factory() as db:assert c.query_checkout(db,'u','chat',p,id)['status']=='unknown'
    fake.orders['order-1']['storeAddress']='测试地址'
    fake.orders['order-1']['orderProductList'][0]['comboItemList'][0]['itemQuantity']=2
    with factory() as db:assert c.query_checkout(db,'u','chat',p,id)['status']=='unknown'
    assert fake.submits==1


def test_discovered_modifier_contract_includes_unselected_keys():
    item={'modification':{'items':[{'minValues':1,'maxValues':1,'values':[
        {'code':'ice','selectedQuantity':1,'selectedKey':'standard'},
        {'code':'ice','selectedQuantity':0,'selectedKey':'none','unselectedKey':'not-none'}]}]}}
    assert c.default_modification(item)=={'values':[{'code':'ice','key':'standard','quantity':1},{'code':'ice','key':'not-none','quantity':0}]}


def test_create_receipt_payment_link_survives_query_timeout_and_recovery(setup):
    id,v=prepare(setup);factory,fake,p,_,_=setup;original=fake.submit_pickup
    def create(args):return {**original(args),'payH5Url':'https://m.mcd.cn/pay?orderId=order-1'}
    fake.submit_pickup=create;fake.query_failure=True
    r=submit(setup,id,v)
    assert r['status']=='unknown' and r['order']['pay_url'].endswith('orderId=order-1')
    with factory() as db:
        op=db.scalar(select(ExternalOperation));assert op.result['pay_url']==r['order']['pay_url']
        r=c.query_checkout(db,'u','chat',p,id);assert r['order']['pay_url'].endswith('orderId=order-1')
    fake.query_failure=False
    with factory() as db:assert c.query_checkout(db,'u','chat',p,id)['order']['verified'] is True
    assert fake.submits==1


def test_payment_url_is_not_accepted_for_another_order():
    assert c.official_pay_url('https://m.mcd.cn/pay?orderId=other','mine')==''
    assert c.official_pay_url('https://m.mcd.cn/pay\n?orderId=mine','mine')==''
