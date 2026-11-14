"""Incremental lifecycle regressions: real SQLite, isolated synthetic MCP only."""
import datetime as dt
import pytest
from sqlalchemy import select
from app.domain import commerce as c, order_lifecycle as life
from app.models import CheckoutDraft,ExternalOperation,utcnow
from tests.test_commerce_checkout import setup,prepare,submit


@pytest.mark.parametrize('raw,expected',[('1','unpaid'),('待支付','unpaid'),('7','cancelled'),('已取消','cancelled'),('2','paid'),('6','paid'),('已过期','expired')])
def test_lifecycle_independent_of_full_cart_verification(raw,expected):
    now=utcnow();receipt=c.order_observation({'orderId':'owned-order','orderStatus':raw},'owned-order',{'total_cents':1000,'order_created_at':now.isoformat()},False)
    value=life.present(receipt,now)
    assert value['lifecycle']==expected and value['verified'] is False
    foreign=c.order_observation({'orderId':'foreign','orderStatus':raw},'owned-order',{'total_cents':1000},False)
    assert life.present(foreign,now)['lifecycle']=='unknown'


@pytest.mark.parametrize('offset,expected',[(899,'unpaid'),(900,'expired'),(901,'expired')])
def test_fifteen_minute_boundary_uses_dispatch_not_quote_creation(offset,expected):
    created=utcnow();r={'status_verified':True,'status_code':'1','created_at':created.isoformat()}
    assert life.present(r,created+dt.timedelta(seconds=offset))['lifecycle']==expected


def test_official_china_timezone_and_paid_order_never_mislabelled_expired():
    now=dt.datetime(2026,11,13,2,tzinfo=dt.timezone.utc)
    r={'status_verified':True,'status_code':'1','expire_at':'2026-11-13 10:01:00'}
    assert life.present(r,now)['lifecycle']=='unpaid'
    assert life.present(r,now+dt.timedelta(minutes=1))['lifecycle']=='expired'
    r['status_code']='2'
    assert life.present(r,now+dt.timedelta(days=4))['lifecycle']=='paid'
    assert life.present({'status_verified':False,'status_code':'1','expire_at':'bad'},now)['lifecycle']=='unknown'


def new_quote(setup,key='new'):
    factory,_,p,store,products=setup
    with factory() as db:
        row=c.quote_preview(db,'u','chat',p,store,[{'product_id':products['wings'],'quantity':1}],None,key=key)
        db.commit();return row.id,row.version


def test_old_cancelled_order_with_incomplete_cart_no_longer_blocks_new_order(setup):
    old,v=prepare(setup);setup[1].query_failure=True
    assert submit(setup,old,v)['status']=='unknown'
    setup[1].query_failure=False;setup[1].orders['order-1']={'orderId':'order-1','orderStatus':'已取消'}
    new,version=new_quote(setup)
    assert submit(setup,new,version)['status']=='success'
    assert setup[1].submits==2
    with setup[0]() as db:
        saved=db.get(CheckoutDraft,old)
        assert saved.status=='unknown'
        assert saved.quote['order']['lifecycle']=='cancelled'
        assert db.scalar(select(ExternalOperation).where(ExternalOperation.remote_id=='order-1')).status=='unknown'


def commit_old(setup,raw='1',*,incomplete=False,age=0):
    old,v=prepare(setup);submit(setup,old,v)
    fake=setup[1];fake.orders['order-1']['orderStatus']=raw
    if incomplete:fake.orders['order-1']={'orderId':'order-1','orderStatus':raw}
    with setup[0]() as db:
        row=db.get(CheckoutDraft,old);row.status='unknown'
        row.quote={**row.quote,'order_created_at':(utcnow()-dt.timedelta(seconds=age)).isoformat(),'order':None}
        op=db.get(ExternalOperation,row.operation_id);op.result={};op.created_at=utcnow()-dt.timedelta(seconds=age)
        db.commit()
    return old


@pytest.mark.parametrize('raw,age',[('7',0),('1',901),('2',0),('6',86400)])
def test_inactive_and_paid_history_never_asks_existing_order_question(setup,raw,age):
    commit_old(setup,raw,incomplete=True,age=age)
    id,v=new_quote(setup)
    result=submit(setup,id,v)
    assert result['status']=='success' and not result.get('order_conflict'),result
    assert setup[1].submits==2


def test_active_unpaid_offers_choice_and_explicit_new_is_single_dispatch(setup):
    old=commit_old(setup);id,v=new_quote(setup);r=submit(setup,id,v)
    assert r['status']=='existing_order' and setup[1].submits==1
    conflict=r['order_conflict'];assert conflict['orders'][0]['checkout_id']==old
    assert conflict['orders'][0]['order']['lifecycle']=='unpaid'
    with setup[0]() as db:
        r=c.submit(db,'u','chat',setup[2],id,v,confirmed=True,existing_order_decision=conflict['fingerprint'])
    assert r['status']=='success' and setup[1].submits==2
    submit(setup,id,v);assert setup[1].submits==2


def test_changed_old_order_set_invalidates_choice(setup):
    commit_old(setup);id,v=new_quote(setup);r=submit(setup,id,v);stamp=r['order_conflict']['fingerprint']
    third,tv=new_quote(setup,'third')
    with setup[0]() as db:
        row=db.get(CheckoutDraft,third);row.status='unknown';db.commit()
        r=c.submit(db,'u','chat',setup[2],id,v,confirmed=True,existing_order_decision=stamp)
    assert r['status']=='existing_order' and len(r['order_conflict']['orders'])==2
    assert setup[1].submits==1


def test_all_old_orders_evaluated_and_unknown_never_expires_by_age(setup):
    commit_old(setup,'7',incomplete=True)
    old,v=new_quote(setup,'unknown')
    with setup[0]() as db:
        row=db.get(CheckoutDraft,old);row.status='unknown'
        row.quote={**row.quote,'order_created_at':(utcnow()-dt.timedelta(days=2)).isoformat()};db.commit()
    id,v=new_quote(setup,'current');r=submit(setup,id,v)
    assert r['status']=='existing_order' and len(r['order_conflict']['orders'])==1
    assert r['order_conflict']['orders'][0]['order']['lifecycle']=='unknown'
    assert r['order_conflict']['can_create_separately'] and setup[1].submits==1


def test_inflight_other_intent_cannot_be_overridden(setup):
    old,v=new_quote(setup,'inflight')
    with setup[0]() as db:
        row=db.get(CheckoutDraft,old);row.status='submitting'
        row.quote={**row.quote,'order_created_at':utcnow().isoformat()};db.commit()
    id,v=new_quote(setup);r=submit(setup,id,v)
    assert r['status']=='existing_order' and not r['order_conflict']['can_create_separately']
    with setup[0]() as db:r=c.submit(db,'u','chat',setup[2],id,v,confirmed=True,existing_order_decision=r['order_conflict']['fingerprint'])
    assert r['status']=='existing_order' and setup[1].submits==0


def test_remote_cancellation_is_separately_confirmed_and_never_submits_new(setup):
    old=commit_old(setup);id,v=new_quote(setup);r=submit(setup,id,v)
    fake=setup[1];cancelled=[]
    original=fake.discover;fake.discover=lambda:{**original(),'cancel-order':{}}
    def cancel(args):
        cancelled.append(args);fake.orders[args['orderId']]['orderStatus']='7';return {'cancelResult':True}
    fake.cancel_pickup=cancel
    with setup[0]() as db:
        with pytest.raises(c.CheckoutError):c.cancel_existing_order(db,'u','chat',setup[2],id,v,old,r['order_conflict']['fingerprint'],confirmed=False)
        result=c.cancel_existing_order(db,'u','chat',setup[2],id,v,old,r['order_conflict']['fingerprint'],confirmed=True)
    assert result['status']=='confirmation_required' and len(cancelled)==1 and fake.submits==1
    assert submit(setup,id,v)['status']=='success' and fake.submits==2


def test_payment_race_prevents_cancellation_and_foreign_existing_order_rejected(setup):
    old=commit_old(setup);id,v=new_quote(setup);r=submit(setup,id,v)
    setup[1].orders['order-1']['orderStatus']='2'
    with setup[0]() as db:
        with pytest.raises(c.CheckoutError,match='已支付'):c.cancel_existing_order(db,'u','chat',setup[2],id,v,old,r['order_conflict']['fingerprint'],confirmed=True)
        with pytest.raises(c.CheckoutError):c.cancel_existing_order(db,'u','chat',setup[2],id,v,'foreign',r['order_conflict']['fingerprint'],confirmed=True)
    assert setup[1].submits==1


def test_cross_thread_pending_order_is_detected_but_other_user_or_account_is_not(setup):
    old=commit_old(setup)
    with setup[0]() as db:
        row=db.get(CheckoutDraft,old);row.thread='older-chat';db.commit()
    id,v=new_quote(setup);r=submit(setup,id,v)
    assert r['status']=='existing_order'
    with setup[0]() as db:
        row=db.get(CheckoutDraft,old);row.account_key='other-account';db.commit()
    assert submit(setup,id,v)['status']=='success'


def test_cancellation_timeout_is_not_retried_and_cannot_automatically_create_new(setup):
    old=commit_old(setup);id,v=new_quote(setup);r=submit(setup,id,v);fake=setup[1];calls=[]
    discover=fake.discover;fake.discover=lambda:{**discover(),'cancel-order':{}}
    def cancel(args):calls.append(args);raise TimeoutError('cancel response lost')
    fake.cancel_pickup=cancel
    for _ in range(2):
        with setup[0]() as db:
            result=c.cancel_existing_order(db,'u','chat',setup[2],id,v,old,r['order_conflict']['fingerprint'],confirmed=True)
            assert result['status']=='existing_order'
    assert len(calls)==1 and fake.submits==1


def test_chat_cancel_choice_then_new_confirmation_preserves_whole_conversation(chat):
    from tests.test_commerce_chat import prepare_chat
    from tests.test_semantic_binding import task
    env,model,fake,post=chat
    discover=fake.discover;fake.discover=lambda:{**discover(),'cancel-order':{}}
    calls=[]
    def cancel(args):calls.append(args);fake.orders[args['orderId']]['orderStatus']='7';return {'cancelResult':True}
    fake.cancel_pickup=cancel
    first=prepare_chat(chat);pending=first['pending_confirmation']
    old=post('确认第一单',confirmation={'action':'confirm','pending_id':pending['pending_id'],'version':pending['version']})
    fake.orders['order-1']['orderStatus']='1'
    fake.orders['order-1']['payH5Url']='https://www.mcd.cn/pay?orderId=order-1'
    def prep(data):
        store=next(o for o in data['snapshot']['objects'] if o['type']=='commerce_store')
        product=next(o for o in data['snapshot']['objects'] if o['type']=='commerce_product' and o['name']=='麦辣鸡翅')
        return {'snapshot_id':data['snapshot']['snapshot_id'],'tasks':[task(store['id'],'commerce.prepare_order',items=[{'product_id':product['id'],'quantity':2}]).model_dump()]}
    model.responses.append(prep)
    new=post('再点两份鸡翅，先看预览');pending=new['pending_confirmation']
    conflict=post('确认新预览',confirmation={'action':'confirm','pending_id':pending['pending_id'],'version':pending['version']})
    assert conflict['overall_status']=='existing_order' and fake.submits==1,conflict
    assert conflict['order_conflict']['orders'][0]['order']['pay_url'].startswith('https://www.mcd.cn/')
    action=next(a for a in conflict['actions'] if a['type']=='checkout_existing_cancel')
    cancel_preview=post('取消这笔旧单',checkout_action={'action':'cancel_existing','checkout_id':new['checkout']['id'],'version':new['checkout']['version'],
        'existing_checkout_id':action['existing_checkout_id'],'decision_fingerprint':action['decision_fingerprint']})
    assert len(calls)==0 and cancel_preview['pending_confirmation']['tasks'][0]['capability_id']=='commerce.cancel_order'
    p=cancel_preview['pending_confirmation'];cancelled=post('确认取消旧单',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert len(calls)==1 and fake.submits==1 and cancelled['checkout']['id']==new['checkout']['id'],cancelled
    p=cancelled['pending_confirmation'];done=post('确认创建新单',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert fake.submits==2 and done['order']['order_id']=='order-2'
    assert len(env.history())>=12


from tests.test_commerce_chat import chat


def test_cancel_transport_uses_discovered_schema_and_read_boundary_stays_closed():
    from app.food.mcd_transport import McdCommerceClient
    from tests.test_mcd_transport import fixture_http
    schema={'type':'object','properties':{'orderId':{'type':'string'},'cancelReasonCode':{'enum':['2']}},'required':['orderId','cancelReasonCode'],'additionalProperties':False}
    old,calls=fixture_http(tool='cancel-order',schema=schema,payload={'success':True,'data':{'cancelResult':True}})
    client=McdCommerceClient(url='https://mcp.example',token='synthetic',http=old.http)
    with pytest.raises(PermissionError):client.call_read('cancel-order',{'orderId':'owned','cancelReasonCode':'2'})
    with pytest.raises(ValueError):client.cancel_pickup({'orderId':'owned','cancelReasonCode':'unregistered'})
    assert not calls
    assert client.cancel_pickup({'orderId':'owned','cancelReasonCode':'2'})['cancelResult'] is True
    assert len([x for x in calls if x['method']=='tools/call'])==1


def test_earlier_official_deadline_wins_and_missing_or_malformed_status_never_fakes_expiry():
    created=utcnow();r={'status_verified':True,'status_code':'1','created_at':created.isoformat(),'expire_at':(created+dt.timedelta(minutes=4)).isoformat()}
    assert life.present(r,created+dt.timedelta(minutes=4))['lifecycle']=='expired'
    r['status_code']='unrecognized';assert life.present(r,created+dt.timedelta(days=9))['lifecycle']=='unknown'


def test_payment_window_starts_at_dispatch_not_slow_preflight(setup,monkeypatch):
    id,v=prepare(setup);fake=setup[1];base=utcnow();clock=[base];original=fake.call_read
    def read(name,args):
        if name=='query-nearby-stores':clock[0]=base+dt.timedelta(minutes=3)
        return original(name,args)
    fake.call_read=read;monkeypatch.setattr(c,'utcnow',lambda:clock[0])
    result=submit(setup,id,v)
    assert result['order']['created_at']==clock[0].isoformat()
    assert life.provider_time(result['order']['payment_deadline'])==clock[0]+dt.timedelta(minutes=15)


def test_disabled_writes_hide_both_submit_and_cancel_capabilities():
    from app.agent import semantic_planner
    from types import SimpleNamespace
    snapshot=SimpleNamespace(payload={'objects':[{'type':'commerce_account','context_loaded':True,'tools':['create-order','cancel-order','query-order','calculate-price','query-meals','query-meal-detail','query-nearby-stores'],'order_submission_enabled':False}]})
    caps=semantic_planner.planner_manifest(snapshot)
    assert not any(x['capability_id'] in {'commerce.submit_order','commerce.cancel_order'} for x in caps)
