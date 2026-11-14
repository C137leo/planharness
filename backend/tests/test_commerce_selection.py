"""Continuous regressions for selected SKU -> details -> quote. Explicit mocks, real DB."""
import copy
import pytest
from sqlalchemy import select
from app.models import CommerceObject,CheckoutDraft
from app.agent.business_snapshot import build_snapshot
from app.seed import DEMO_USER_ID
from tests.test_commerce_chat import chat,browse,make_plan
from tests.test_semantic_binding import task

HOT='麦咖啡™热美式中杯';COLD='麦咖啡™冰美式大杯'

from tests.test_commerce_checkout import FakeMcp


class GenericCoffeeMcp(FakeMcp):
    """Fixed provider evidence: a generic menu label resolves to a cold SKU."""
    def call_read(self,name,args):
        if name=='query-meals':
            return {'meals':{'coffee':{'name':'麦咖啡™美式','currentPrice':'11.9','productType':1}}}
        if name=='query-meal-detail':
            self.calls.append((name,copy.deepcopy(args)))
            return {'code':args['code'],'name':'麦咖啡™冰美式中杯','rounds':[],
                'modification':{'items':[{'minValues':1,'maxValues':1,'values':[
                    {'code':'extra','name':'不加','selectedQuantity':1,'selectedKey':'0-0'},
                    {'code':'extra','name':'加','selectedQuantity':0,'selectedKey':'0-1'}]}]}}
        return super().call_read(name,args)

@pytest.fixture
def coffee(chat):
    env,model,fake,post=chat;original=fake.call_read;fake.price=1190
    def read(name,args):
        if name=='query-meals':return {'meals':{'hot':{'name':HOT,'currentPrice':'11.9','productType':1},'cold':{'name':COLD,'currentPrice':'12.9','productType':1}}}
        if name=='query-meal-detail':
            fake.calls.append((name,copy.deepcopy(args)))
            return {'code':args['code'],'name':HOT if args['code']=='hot' else COLD,'rounds':[],
                'modification':{'items':[{'minValues':1,'maxValues':1,'values':[
                {'code':'extra','name':'不加','selectedQuantity':1,'selectedKey':'0-0'},
                {'code':'extra','name':'加','selectedQuantity':0,'selectedKey':'0-1'}]}]}}
        return original(name,args)
    fake.call_read=read;browse(chat)
    return chat


def detail_plan(d,name=HOT,pending=False):
    product=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_product' and o['name']==name)
    t=task(product['id'],'commerce.product_detail',status='candidate' if pending else 'resolved',basis='inferred' if pending else 'explicit')
    t.user_goal='读取所选商品详情，再准备订单报价'
    return {'snapshot_id':d['snapshot']['snapshot_id'],'continue_after_tools':True,'tasks':[t.model_dump()],
            'unfulfilled_goals':['还未确定要哪款美式及冰/热规格，暂未生成报价预览']}


def quote_plan(d,name=HOT):
    product=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_product' and o['name']==name)
    root=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_account')
    assert root['focused_product']['product_id']==product['id']
    assert product['detail_summary']['modification_groups'][0]['choices'][0]['selected_quantity']==1
    plan=make_plan(d,'commerce.prepare_order','commerce_store',items=[{'product_id':product['id'],'quantity':1,'use_default_options':True}])
    plan['tasks'][0]['resolution']['selected_id']=product['store_id'];plan['tasks'][0]['resolution']['candidate_ids']=[product['store_id']]
    return plan


def test_confirmed_read_resumes_original_quote_goal_without_resubmitting_or_stale_gaps(coffee):
    env,model,fake,post=coffee;before=env.snapshot()['days']
    model.responses.append(lambda d:detail_plan(d,pending=True))
    r=post('要热美式中杯，读完规格后给我报价，先不要下单')
    assert r['overall_status']=='confirmation_required' and fake.submits==0
    old=r['pending_confirmation']
    model.responses.extend([
        lambda d:{'action':'confirm','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],'execute_revision':False},
        lambda d:quote_plan(d)])
    r=post('确认')
    assert r.get('checkout'),r
    assert r['checkout']['cart'][0]['name']==HOT and r['checkout']['status']=='awaiting_confirmation'
    assert '未确定' not in r['reply'] and not r.get('unfulfilled_goals') and fake.submits==0
    count=len([x for x in fake.calls if x[0]=='query-meal-detail'])
    replay=post('重复按钮',confirmation={'action':'confirm','pending_id':old['pending_id'],'version':old['version']})
    assert replay['confirmation_replayed'] and fake.submits==0
    assert len([x for x in fake.calls if x[0]=='query-meal-detail'])==count
    assert env.snapshot()['days']==before


def test_single_sku_detail_exposes_top_level_modifiers_and_product_instead_of_zero_options(coffee):
    env,model,fake,post=coffee
    with env.factory() as db:product=db.scalar(select(CommerceObject).where(CommerceObject.kind=='commerce_product',CommerceObject.external_key=='hot'))
    r=post('查看已选规格',commerce_action={'action':'product_detail','target_id':product.id})
    assert r['commerce_catalog']['kind']=='detail',r
    assert r['commerce_catalog']['product']['name']==HOT
    assert r['commerce_catalog']['detail']['modification_groups'][0]['choices'][0]['name']=='不加'
    assert '共0项' not in r['reply'] and fake.submits==0
    r=post('再看同店菜单',commerce_action={'action':'menu','target_id':product.parent_key})
    with env.factory() as db:
        row=db.get(CommerceObject,product.id)
        assert row.payload['detail_summary']['modification_groups']


def test_missing_optional_options_is_still_a_valid_fixed_product(coffee):
    env,model,fake,post=coffee;original=fake.call_read
    fake.call_read=lambda name,args:{'code':args['code'],'name':HOT,'rounds':[]} if name=='query-meal-detail' else original(name,args)
    with env.factory() as db:product=db.scalar(select(CommerceObject).where(CommerceObject.kind=='commerce_product',CommerceObject.external_key=='hot'))
    r=post('查看',commerce_action={'action':'product_detail','target_id':product.id})
    assert r['commerce_catalog']['kind']=='detail' and r['commerce_catalog']['product']['id']==product.id
    assert r['commerce_catalog']['detail']['option_group_count']==0
    assert '未返回额外可选项' in r['reply'] and '未选' not in r['reply']


def test_cancel_read_never_resumes_quote(coffee):
    env,model,fake,post=coffee
    model.responses.append(lambda d:detail_plan(d,pending=True));r=post('先确认读取这个商品后给报价')
    p=r['pending_confirmation'];r=post('取消',confirmation={'action':'cancel','pending_id':p['pending_id'],'version':p['version']})
    assert r['overall_status']=='cancelled' and fake.submits==0
    with env.factory() as db:assert list(db.scalars(select(CheckoutDraft)))==[]


def test_negated_choice_replaces_previous_pending_product_without_restoring_old_goal(coffee):
    env,model,fake,post=coffee
    model.responses.append(lambda d:detail_plan(d,pending=True));post('先看看热美式中杯再报价')
    model.responses.extend([
        lambda d:{'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],
            'execute_revision':False,'keep_task_ids':[],'new_request_text':'不要热美式，改为冰美式大杯，先查看详情并给报价，不提交'},
        lambda d:detail_plan(d,COLD),lambda d:quote_plan(d,COLD)])
    r=post('不要热的了，换成冰美式大杯，先报价别提交')
    assert r.get('checkout') and r['checkout']['cart'][0]['name']==COLD,r
    assert fake.submits==0


def test_read_confirmation_cannot_authorize_new_quote_submission(coffee):
    env,model,fake,post=coffee
    model.responses.append(lambda d:detail_plan(d,pending=True));r=post('读完这个商品后给我外带报价，不要提交')
    old=r['pending_confirmation']
    def quote(d):
        plan=quote_plan(d);plan['tasks'][0]['arguments']['take_way_code']='locker-out';return plan
    model.responses.append(quote)
    r=post('确认读取',confirmation={'action':'confirm','pending_id':old['pending_id'],'version':old['version']})
    assert fake.submits==0 and r['overall_status']=='confirmation_required',r
    assert r['pending_confirmation']['pending_id']!=old['pending_id']
    assert r['checkout']['cart'][0]['name']==HOT


def test_quote_continuation_model_failure_keeps_read_product_and_manual_preview_path(coffee):
    env,model,fake,post=coffee
    model.responses.append(lambda d:detail_plan(d,pending=True));r=post('先读商品详情再准备报价')
    old=r['pending_confirmation'];model.available=lambda:False
    r=post('确认',confirmation={'action':'confirm','pending_id':old['pending_id'],'version':old['version']})
    assert r['overall_status']=='partial_success' and r['commerce_catalog']['product']['name']==HOT,r
    assert '所选商品已保留' in r['reply'] and fake.submits==0
    product=r['commerce_catalog']['product'];store_id=r['commerce_catalog']['store_id']
    r=post('手动生成预览',commerce_action={'action':'prepare_order','target_id':store_id,'items':[{'product_id':product['id'],'quantity':1,'use_default_options':True}]})
    assert r['checkout']['cart'][0]['name']==HOT and fake.submits==0


def test_read_without_followup_goal_still_confirms_offline_without_planning_new_tasks(coffee):
    env,model,fake,post=coffee
    def read_only(d):
        plan=detail_plan(d,pending=True);plan['continue_after_tools']=False;plan['unfulfilled_goals']=[]
        plan['tasks'][0]['user_goal']='只查看这一个商品的详情';return plan
    model.responses.append(read_only);r=post('仅查看商品信息')
    p=r['pending_confirmation'];model.available=lambda:False
    r=post('确认',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert r['overall_status']=='success' and r['commerce_catalog']['product']['name']==HOT,r
    with env.factory() as db:assert list(db.scalars(select(CheckoutDraft)))==[]
    assert fake.submits==0


def test_detail_specific_name_overrides_generic_menu_in_snapshot_card_and_cart(coffee):
    env,model,fake,post=coffee;original=fake.call_read
    def read(name,args):
        data=original(name,args)
        if name=='query-meals':data['meals']['hot']['name']='麦咖啡™美式'
        return data
    fake.call_read=read
    with env.factory() as db:product=db.scalar(select(CommerceObject).where(CommerceObject.kind=='commerce_product',CommerceObject.external_key=='hot'))
    post('菜单',commerce_action={'action':'menu','target_id':product.parent_key})
    r=post('详情',commerce_action={'action':'product_detail','target_id':product.id})
    assert r['commerce_catalog']['product']['name']==HOT
    assert r['commerce_catalog']['detail']['menu_name']=='麦咖啡™美式'
    with env.factory() as db:
        from app.models import User
        from app.evals.dialogue_runner import DAY
        from app.domain.commerce import CommerceProvider
        db.info['commerce_provider']=CommerceProvider('test-account',lambda:fake,True)
        snapshot=build_snapshot(db,db.get(User,DEMO_USER_ID),env.thread,[str(DAY)])
        obj=next(o for o in snapshot.payload['objects'] if o['id']==product.id)
        assert obj['name']==HOT and obj['menu_name']=='麦咖啡™美式'
    r=post('默认预览',commerce_action={'action':'prepare_order','target_id':product.parent_key,
        'items':[{'product_id':product.id,'quantity':1,'use_default_options':True}]})
    assert r['checkout']['cart'][0]['name']==HOT and fake.submits==0


def test_continuation_with_no_compatible_product_keeps_detail_and_reports_clarification(coffee):
    env,model,fake,post=coffee
    model.responses.extend([lambda d:detail_plan(d),lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],
        'mode':'execute','tasks':[],'answer':'当前商品不符合指定规格，请改选。','question':'要改选吗？',
        'unfulfilled_goals':['未找到符合指定规格的商品'] }])
    r=post('先核对商品详情，符合我的要求再报价')
    assert r['overall_status']=='clarification_required' and r['commerce_catalog']['kind']=='detail',r
    assert not r.get('checkout') and fake.submits==0
