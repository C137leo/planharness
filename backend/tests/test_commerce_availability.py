"""Incremental store and stock regressions, real SQLite and explicit fake MCP."""
import copy
import pytest
from app.domain import commerce as c,commerce_availability as a
from tests.test_commerce_checkout import setup,prepare
from app.models import CommerceObject


def stores(setup,rows):
    fake=setup[1];original=fake.call_read
    def read(name,args):return copy.deepcopy(rows) if name=='query-nearby-stores' else original(name,args)
    fake.call_read=read


def test_fresh_closed_store_stops_quote_before_menu_or_price(setup):
    factory,fake,p,id,products=setup
    stores(setup,[{'storeCode':'store1','storeName':'原店','businessStatus':False,'distance':100}])
    start=len(fake.calls)
    with factory() as db:
        with pytest.raises(a.AvailabilityError,match='没有确认营业') as error:
            c.quote_preview(db,'u','chat',p,id,[{'product_id':products['wings'],'quantity':1}],None,key='new')
        assert error.value.code=='no_open_store'
    assert not any(name in {'query-meals','calculate-price'} for name,_ in fake.calls[start:]) and fake.submits==0


def test_nearest_closed_suggests_actual_nearest_open_and_requires_consent(setup):
    factory,fake,p,id,products=setup
    stores(setup,[{'storeCode':'store1','storeName':'近店','businessStatus':False,'distance':100},
                 {'storeCode':'far','storeName':'远营业店','businessStatus':True,'distance':600},
                 {'storeCode':'open','storeName':'近营业店','businessStatus':True,'distance':200}])
    with factory() as db:
        original=db.get(CommerceObject,id)
        with p.connect() as client:rows,context=a.search(db,'u','chat',p,client,original.payload['search'],'nearest')
        issue=a.switch_error(rows,context)
        assert issue.choice['store']['name']=='近营业店'
        target=db.get(CommerceObject,issue.choice['store_id'])
        with p.connect() as client:
            with pytest.raises(a.AvailabilityError,match='是否改为这家'):a.check(db,'u','chat',p,target,client)
        a.choose(db,'u','chat',p,target.id,context['revision'])
        with p.connect() as client:assert a.check(db,'u','chat',p,target,client)['open'] is True
    assert fake.submits==0


def test_stock_disappearance_names_actual_product_without_substitution(setup):
    factory,fake,p,id,products=setup;fake.unavailable=True
    with factory() as db:
        with pytest.raises(a.AvailabilityError,match='麦辣鸡翅') as error:
            c.quote_preview(db,'u','chat',p,id,[{'product_id':products['wings'],'quantity':1}],None,key='stock')
        assert error.value.code=='product_unavailable'
    assert fake.submits==0


from tests.test_commerce_chat import chat


def closed_then_open(chat):
    env,model,fake,post=chat;original=fake.call_read
    def read(name,args):
        if name=='query-nearby-stores':return [
            {'storeCode':'closed','storeName':'最近休息店','address':'合成近址','businessStatus':False,'distance':100,'businessStartTime':'08:00','businessEndTime':'22:00'},
            {'storeCode':'store1','storeName':'真实门店','address':'测试地址','businessStatus':True,'distance':300,'businessStartTime':'00:00','businessEndTime':'23:59'}]
        return original(name,args)
    fake.call_read=read


def test_chat_nearest_closed_asks_once_then_confirm_resumes_original_wings_goal(chat):
    from tests.test_commerce_chat import make_plan
    from tests.test_semantic_binding import task
    env,model,fake,post=chat;closed_then_open(chat)
    model.responses.extend([
        lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'mode':'load_context','context_request':{'commerce':True}},
        lambda d:{**make_plan(d,'commerce.search_stores','commerce_account',city='北京',keyword='地标',selection_policy='nearest'),'continue_after_tools':True}])
    first=post('帮我从地标最近的店点鸡翅，先看看报价')
    assert first['overall_status']=='confirmation_required' and '最近休息店' in first['reply'] and '真实门店' in first['reply'],first
    assert first['pending_confirmation']['tasks'][0]['capability_id']=='commerce.select_store'
    assert not any(name=='query-meals' for name,_ in fake.calls) and fake.submits==0
    def menu(d):
        root=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_account')
        return {'snapshot_id':d['snapshot']['snapshot_id'],'continue_after_tools':True,
            'tasks':[task(root['selected_store']['store_id'],'commerce.menu').model_dump()]}
    def prep(d):
        product=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_product' and o['name']=='麦辣鸡翅')
        root=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_account')
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(root['selected_store']['store_id'],'commerce.prepare_order',items=[{'product_id':product['id'],'quantity':1}]).model_dump()]}
    model.responses.extend([menu,prep]);p=first['pending_confirmation']
    second=post('确认换这家继续',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert second['checkout']['cart'][0]['name']=='麦辣鸡翅' and second['checkout']['quote']['store']['name']=='真实门店' and fake.submits==0,second
    assert '鸡翅' in model.calls[-1]['user_text']


def test_all_closed_returns_business_explanation_not_generic_code(chat):
    from tests.test_commerce_chat import make_plan
    env,model,fake,post=chat;fake.call_read=lambda name,args:[{'storeCode':'closed','storeName':'休息店','businessStatus':False,'distance':10}]
    model.responses.extend([lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'mode':'load_context','context_request':{'commerce':True}},
        lambda d:make_plan(d,'commerce.search_stores','commerce_account',city='北京',keyword='地标',selection_policy='nearest')])
    r=post('从最近的店给我点鸡翅')
    assert r['overall_status']=='unavailable' and '没有确认营业' in r['reply'] and not r.get('pending_confirmation'),r
    assert fake.submits==0


def test_transport_preserves_safe_provider_reason_without_parsing_user_sentences():
    from tests.test_mcd_transport import fixture_http
    from app.food.mcd_transport import McpBusinessError
    client,_=fixture_http(payload={'success':False,'code':'SOLD_OUT','message':'麦辣鸡翅已售罄，请选择其他餐品。 token=private-value'})
    with pytest.raises(McpBusinessError) as error:client.call_read('query-meals',{'storeCode':'real','orderType':1,'beType':1})
    assert error.value.provider_code=='SOLD_OUT' and '麦辣鸡翅已售罄' in error.value.provider_message
    assert 'private-value' not in error.value.provider_message


def test_confirmed_candidate_closing_or_search_changed_requires_fresh_choice(setup):
    factory,fake,p,id,products=setup
    rows=[{'storeCode':'store1','storeName':'原店','businessStatus':False,'distance':100},{'storeCode':'new','storeName':'候选店','businessStatus':True,'distance':200}]
    stores(setup,rows)
    with factory() as db:
        old=db.get(CommerceObject,id)
        with p.connect() as client:found,context=a.search(db,'u','chat',p,client,old.payload['search'],'nearest')
        target=found[1]['id'];rows[1]['businessStatus']=False
        with pytest.raises(a.AvailabilityError) as error:a.choose(db,'u','chat',p,target,context['revision'])
        assert error.value.code=='no_open_store'
    assert fake.submits==0


def test_manual_store_button_is_explicit_switch_consent_and_still_checks_open(chat):
    env,model,fake,post=chat;closed_then_open(chat)
    result=post('找门店',commerce_action={'action':'search_stores','city':'北京','keyword':'地标'})
    opened=next(x for x in result['commerce_catalog']['items'] if x['open'])
    menu=post('选这家',commerce_action={'action':'menu','target_id':opened['id']})
    assert menu['commerce_catalog']['kind']=='products' and fake.submits==0 and not menu.get('pending_confirmation'),menu
    closed=next(x for x in result['commerce_catalog']['items'] if not x['open'])
    blocked=post('尝试休息店',commerce_action={'action':'menu','target_id':closed['id']})
    assert blocked['overall_status']=='confirmation_required' and fake.submits==0


def test_upstream_business_reason_is_actionable_and_preserved_in_trace(chat):
    from tests.test_commerce_chat import browse,make_plan
    from app.food.mcd_transport import McpBusinessError
    env,model,fake,post=chat;browse(chat);original=fake.call_read
    def read(name,args):
        if name=='calculate-price':raise McpBusinessError(provider_code='STOCK_EMPTY',provider_message='麦辣鸡翅本时段已售罄',tool=name)
        return original(name,args)
    fake.call_read=read
    def prep(d):
        product=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_product' and o['name']=='麦辣鸡翅')
        return make_plan(d,'commerce.prepare_order','commerce_store',items=[{'product_id':product['id'],'quantity':1}])
    model.responses.append(prep);r=post('给我一份鸡翅报价')
    assert r['overall_status']=='unavailable' and '麦辣鸡翅本时段已售罄' in r['reply'] and 'mcp_business_rejected' not in r['reply'],r
    assert r['task_results'][0]['provider_code']=='STOCK_EMPTY'
    assert r['actions'][0]['type']=='commerce_recovery_menu' and fake.submits==0


def test_rejected_create_is_definite_failure_with_reason_and_no_retry(setup):
    from app.food.mcd_transport import McpBusinessError
    from app.models import CheckoutDraft,ExternalOperation
    from sqlalchemy import select
    id,v=prepare(setup);fake=setup[1];calls=[]
    def reject(args):calls.append(args);raise McpBusinessError(provider_code='NO_STOCK',provider_message='鸡翅已售罄')
    fake.submit_pickup=reject
    with setup[0]() as db:
        with pytest.raises(McpBusinessError):c.submit(db,'u','chat',setup[2],id,v,confirmed=True)
        row=db.get(CheckoutDraft,id);assert row.status=='failed'
        assert db.scalar(select(ExternalOperation)).status=='failed'
        with pytest.raises(c.CheckoutError):c.submit(db,'u','chat',setup[2],id,v,confirmed=True)
    assert len(calls)==1


def test_submit_time_closure_preserves_requested_cart_and_rebinds_new_store(chat):
    from tests.test_commerce_chat import prepare_chat
    from tests.test_semantic_binding import task
    env,model,fake,post=chat;first=prepare_chat(chat);old_id=first['checkout']['id'];original=fake.call_read
    def read(name,args):
        if name=='query-nearby-stores':return [
            {'storeCode':'store1','storeName':'真实门店','address':'测试地址','businessStatus':False,'distance':100},
            {'storeCode':'alternate','storeName':'新营业门店','address':'新址','businessStatus':True,'distance':200}]
        return original(name,args)
    fake.call_read=read;p=first['pending_confirmation']
    blocked=post('确认创建待支付订单',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert blocked['pending_confirmation']['tasks'][0]['capability_id']=='commerce.select_store' and fake.submits==0,blocked
    def menu(d):
        root=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_account')
        assert any(m.get('original_requested_cart')==[{'name':'麦辣鸡翅','quantity':1}] for m in d['recent_messages'])
        return {'snapshot_id':d['snapshot']['snapshot_id'],'continue_after_tools':True,'tasks':[task(root['selected_store']['store_id'],'commerce.menu').model_dump()]}
    def prep(d):
        root=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_account')
        food=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_product' and o['store_id']==root['selected_store']['store_id'] and o['name']=='麦辣鸡翅')
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[task(root['selected_store']['store_id'],'commerce.prepare_order',items=[{'product_id':food['id'],'quantity':1}]).model_dump()]}
    model.responses.extend([menu,prep]);pending=blocked['pending_confirmation']
    new=post('确认改店',confirmation={'action':'confirm','pending_id':pending['pending_id'],'version':pending['version']})
    assert new['checkout']['id']!=old_id and new['checkout']['quote']['store']['name']=='新营业门店' and fake.submits==0,new


def test_removed_products_no_longer_appear_as_buyable_snapshot_objects(setup):
    factory,fake,p,id,products=setup
    with factory() as db:
        store=db.get(CommerceObject,id);c.save_menu(db,'u','chat',p,store,{'meals':{}});db.commit()
        result=c.snapshot_objects(db,'u','chat',p)
        assert products['wings'] not in {o['id'] for o in result}
        assert db.get(CommerceObject,products['wings']).payload['available'] is False
