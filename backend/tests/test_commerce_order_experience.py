"""Incremental pickup UX contracts with actual SQLite, fake MCP, no live orders."""
import pytest
from sqlalchemy import select
from app.models import User,CoachPendingPlan,CheckoutDraft
from app.seed import DEMO_USER_ID
from app.domain import commerce
from tests.test_commerce_chat import chat,browse,make_plan


def preview(chat,preference=None):
    env,model,fake,post=chat;browse(chat)
    with env.factory() as db:
        from app.models import CommerceObject
        store=db.scalar(select(CommerceObject).where(CommerceObject.kind=='commerce_store'))
        food=db.scalar(select(CommerceObject).where(CommerceObject.kind=='commerce_product',CommerceObject.external_key=='wings'))
        store_id,product_id=store.id,food.id
    if preference:
        model.responses.append(lambda d:make_plan(d,'commerce.prepare_order','commerce_store',items=[{'product_id':product_id,'quantity':1}],take_way_preference=preference))
        value=post('堂食鸡翅' if preference=='dine_in' else '鸡翅外带')
    else:value=post('预览',commerce_action={'action':'prepare_order','target_id':store_id,'items':[{'product_id':product_id,'quantity':1}]})
    return value


def test_no_arbitrary_default_location_and_saved_location_is_user_scoped_without_token(chat):
    env,model,fake,post=chat;url='/api/v1/agent/commerce/preferences'
    assert env.http.get(url).status_code==401
    assert env.http.get(url,headers=env.headers).json()['data']=={'city':'','keyword':''}
    response=env.http.put(url,headers=env.headers,json={'city':' 北京 ','keyword':' 三里屯 '})
    assert response.status_code==200 and response.json()['data']=={'city':'北京','keyword':'三里屯'}
    with env.factory() as db:
        db.add(User(id='different-location-user',nickname='另一个合成用户'));db.commit()
        assert commerce.pickup_preferences(db,'different-location-user')=={'city':'','keyword':''}
    assert env.http.put(url,headers=env.headers,json={'city':'','keyword':'三里屯'}).status_code==422
    model.responses.append(lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'mode':'noop','answer':'只检查上下文'})
    post('检查位置')
    root=next(o for o in model.calls[-1]['snapshot']['objects'] if o['type']=='commerce_account')
    assert root['saved_location']=={'city':'北京','keyword':'三里屯'}
    assert env.http.put(url,headers=env.headers,json={'city':'','keyword':''}).json()['data']=={'city':'','keyword':''}
    assert fake.submits==0


def test_default_takeaway_quote_highlights_requested_wings_without_submitting(chat):
    env,model,fake,post=chat;value=preview(chat)
    assert value['overall_status']=='confirmation_required' and value['checkout']['status']=='awaiting_confirmation'
    assert value['checkout']['take_way_code']=='locker-out'
    assert value['checkout']['quote']['take_way_source']=='default_takeaway'
    assert value['checkout']['cart'][0]['name']=='麦辣鸡翅' and '麦辣鸡翅' in value['reply']
    assert {a['label'] for a in value['actions']} >= {'堂食','外带','确认创建待支付订单'}
    assert fake.submits==0


def test_explicit_dine_in_overrides_takeaway_default_before_first_quote(chat):
    value=preview(chat,'dine_in')
    assert value['checkout']['take_way_code']=='eat-in'
    assert value['checkout']['quote']['take_way_source']=='explicit_preference'
    assert chat[2].submits==0


def test_changing_take_way_requotes_and_invalidates_old_confirmation(chat):
    env,model,fake,post=chat;first=preview(chat);old=first['pending_confirmation'];checkout=first['checkout']
    changed=post('改堂食',checkout_action={'action':'choose_take_way','checkout_id':checkout['id'],'version':checkout['version'],'take_way_code':'eat-in'})
    assert changed['checkout']['take_way_code']=='eat-in' and changed['checkout']['version']==checkout['version']+1
    assert changed['pending_confirmation']['pending_id']!=old['pending_id']
    late=post('旧确认',confirmation={'action':'confirm','pending_id':old['pending_id'],'version':old['version']})
    assert late['overall_status']=='cancelled' and fake.submits==0


def test_menu_then_cart_change_revises_same_draft_and_preserves_old_price_until_preview(chat):
    env,model,fake,post=chat;first=preview(chat);before=env.snapshot();checkout=first['checkout']
    menu=post('重新选餐',commerce_action={'action':'checkout_menu','target_id':checkout['id'],'version':checkout['version']})
    assert menu['commerce_catalog']['kind']=='products' and menu['checkout']==checkout and menu['commerce_view']=='edit_menu'
    chosen=next(i for i in menu['commerce_catalog']['items'] if i['name']=='牛肉汉堡套餐')
    changed=post('预览新选择',commerce_action={'action':'revise_order','target_id':checkout['id'],'version':checkout['version'],
        'items':[{'product_id':chosen['id'],'quantity':1,'use_default_options':True}]})
    assert changed['checkout']['id']==checkout['id'] and changed['checkout']['version']==checkout['version']+1
    assert changed['checkout']['cart'][0]['name']=='牛肉汉堡套餐'
    with env.factory() as db:
        assert len(list(db.scalars(select(CheckoutDraft))))==1
        assert db.get(CoachPendingPlan,first['pending_confirmation']['pending_id']).status=='cancelled'
    assert env.snapshot()['days']==before['days'] and fake.submits==0


def test_no_takeaway_option_does_not_guess_code_or_default_to_dine_in(chat):
    env,model,fake,post=chat;original=fake.call_read
    def read(name,args):
        value=original(name,args)
        if name=='calculate-price':value['takeWayList']=[{'code':'dine-only-code','title':'堂食'}]
        return value
    fake.call_read=read
    value=preview(chat)
    assert value['checkout']['status']=='needs_selection' and value['checkout']['take_way_code']==''
    assert not any(a['type']=='confirm_tasks' for a in value['actions']) and fake.submits==0


def test_provider_takeaway_code_is_observed_not_assumed_and_detail_keeps_cart(chat):
    env,model,fake,post=chat;original=fake.call_read
    def read(name,args):
        value=original(name,args)
        if name=='calculate-price':value['takeWayList']=[{'code':'provider-dine-87','title':'堂食'},{'code':'provider-carry-91','title':'外带'}]
        return value
    fake.call_read=read;first=preview(chat);checkout=first['checkout']
    assert checkout['take_way_code']=='provider-carry-91'
    result=post('商品详情',commerce_action={'action':'product_detail','target_id':checkout['cart'][0]['product_id'],'checkout_id':checkout['id'],'version':checkout['version']})
    assert result['checkout']==checkout and result['commerce_catalog']['kind']=='detail'
    assert fake.submits==0


@pytest.mark.parametrize('transient,expected_calls',[(True,2),(False,1)])
def test_only_read_gateway_errors_retry_and_authentication_does_not(transient,expected_calls):
    import httpx,json
    from app.food.mcd_transport import McdCommerceClient,McpAuthenticationError
    calls=[]
    def handle(request):
        value=json.loads(request.content);calls.append(value)
        if len(calls)==1:return httpx.Response(502 if transient else 401)
        return httpx.Response(200,json={'jsonrpc':'2.0','id':value['id'],'result':{'structuredContent':{'success':True,'data':{'meals':{}}}}})
    client=McdCommerceClient(url='https://synthetic.example',token='synthetic',http=httpx.Client(transport=httpx.MockTransport(handle)))
    client.tools={'query-meals':{'inputSchema':{'type':'object'}},'create-order':{'inputSchema':{'type':'object'}}}
    if transient:assert client.call_read('query-meals',{})=={'meals':{}}
    else:
        with pytest.raises(McpAuthenticationError):client.call_read('query-meals',{})
    assert len(calls)==expected_calls
    calls.clear()
    with pytest.raises(Exception):client.submit_pickup({'orderType':1,'beType':1,'takeWayCode':'synthetic-code','items':[{'code':'synthetic-product','quantity':1}]})
    assert len(calls)==1  # no retry for a potentially dispatched write


def test_menu_gateway_failure_is_not_misreported_as_missing_city(chat):
    from app.food.mcd_transport import McpHttpError
    env,model,fake,post=chat;browse(chat)
    original=fake.call_read
    def read(name,args):
        if name=='query-meals':raise McpHttpError(502)
        return original(name,args)
    fake.call_read=read
    model.responses.append(lambda d:make_plan(d,'commerce.menu','commerce_store'))
    result=post('继续看刚才门店菜单')
    assert result['overall_status']=='failed'
    assert '门店菜单' in result['reply'] and '稍后重试' in result['reply']
    assert result['task_results'][0]['error_code']=='mcp_http_502' and fake.submits==0


def test_further_fulfillment_clarification_and_hold_keep_real_wings_preview(chat):
    from tests.test_semantic_binding import task
    env,model,fake,post=chat;browse(chat);original=fake.call_read
    def read(name,args):
        value=original(name,args)
        if name=='calculate-price':value['takeWayList']=[{'code':'a','title':'取餐方式A'},{'code':'b','title':'取餐方式B'}]
        return value
    fake.call_read=read
    def prepare(d):
        food=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_product' and o['name']=='麦辣鸡翅')
        return {**make_plan(d,'commerce.prepare_order','commerce_store',items=[{'product_id':food['id'],'quantity':1}]),'continue_after_tools':True}
    def ask(d):
        quote=next(o for o in d['snapshot']['objects'] if o['type']=='checkout')
        pending=task(quote['id'],'commerce.revise_order',version=quote['version']);pending.parameters_resolved=False
        return {'snapshot_id':d['snapshot']['snapshot_id'],'tasks':[pending.model_dump()],'question':'请选择官方返回的取餐方式'}
    model.responses.extend([prepare,ask]);value=post('我要鸡翅，生成预览')
    assert value['checkout']['cart'][0]['name']=='麦辣鸡翅' and value['checkout']['status']=='needs_selection'
    assert len([a for a in value['actions'] if a['type']=='checkout_way'])==2
    model.responses.append(lambda d:{'action':'clarify','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],'execute_revision':False,'question':'预览已保留'})
    held=post('先保留')
    assert held['checkout']==value['checkout'] and len([a for a in held['actions'] if a['type']=='checkout_way'])==2
    assert fake.submits==0


def test_natural_cart_revision_handoff_flushes_preview_before_hold(chat):
    env,model,fake,post=chat;first=preview(chat)
    def control(d):
        return {'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],
            'execute_revision':False,'edit_task_ids':[d['pending']['tasks'][0]['task_id']],'new_request_text':'鸡翅改两份，先更新预览'}
    def revise(d):
        quote=next(o for o in d['snapshot']['objects'] if o['type']=='checkout')
        return make_plan(d,'commerce.revise_order','checkout',version=quote['version'],items=[{**x['selection'],'quantity':2} for x in quote['cart']])
    model.responses.extend([control,revise]);changed=post('鸡翅再加一份，先别下单')
    assert changed['checkout']['cart'][0]['quantity']==2
    with env.factory() as db:
        assert db.get(CoachPendingPlan,changed['pending_confirmation']['pending_id']).result['checkout']['cart'][0]['quantity']==2
    model.responses.append(lambda d:{'action':'clarify','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],'execute_revision':False,'question':'报价先保留'})
    held=post('先保留这份报价，不要创建订单')
    assert held['checkout']==changed['checkout'] and fake.submits==0
