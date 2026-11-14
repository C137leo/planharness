"""Authenticated chat + actual SQLite + explicit MCP/model mocks; never NLU scores."""
import json
import pytest
from sqlalchemy import select
from app.evals.dialogue_runner import DialogueEnvironment, DAY
from app.domain.commerce import CommerceProvider
from app.api.coach_routes import coach_commerce_dependency
from app.models import CheckoutDraft, CoachPendingPlan, CommerceObject
from tests.test_commerce_checkout import FakeMcp
from tests.test_semantic_binding import Scripted, task


@pytest.fixture
def chat(tmp_path):
    model=Scripted([]);fake=FakeMcp();env=DialogueEnvironment(tmp_path/'chat.db','commerce-chat',model)
    p=CommerceProvider('test-account',lambda:fake,True)
    env.app.dependency_overrides[coach_commerce_dependency]=lambda:p
    def post(text,**fields):
        response=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':text,'date':str(DAY),'thread':env.thread,**fields})
        assert response.status_code==200,response.text
        return response.json()['data']
    yield env,model,fake,post
    env.close()


def make_plan(data,cap,kind,**args):
    target=next(o for o in data['snapshot']['objects'] if o['type']==kind)
    return {'snapshot_id':data['snapshot']['snapshot_id'],'tasks':[task(target['id'],cap,**args).model_dump()]}


def browse(chat):
    env,model,fake,post=chat
    model.responses.extend([
        lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'mode':'load_context','context_request':{'commerce':True}},
        lambda d:make_plan(d,'commerce.search_stores','commerce_account',city='北京',keyword='三里屯'),
        lambda d:make_plan(d,'commerce.menu','commerce_store')])
    r=post('看看三里屯麦当劳');assert r['overall_status']=='success',r
    r=post('看这家菜单');assert r['overall_status']=='success',r


def prepare_chat(chat):
    env,model,fake,post=chat;browse(chat)
    def prep(d):
        product=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_product' and o['name']=='麦辣鸡翅')
        return make_plan(d,'commerce.prepare_order','commerce_store',items=[{'product_id':product['id'],'quantity':1}])
    model.responses.append(prep)
    r=post('鸡翅准备下单')
    assert fake.submits==0 and r['checkout']['status']=='awaiting_confirmation' and r['checkout']['take_way_code']=='locker-out',r
    r=post('外带',checkout_action={'action':'choose_take_way','checkout_id':r['checkout']['id'],'version':r['checkout']['version'],'take_way_code':'locker-out'})
    assert r['overall_status']=='confirmation_required',r
    assert '官方总额：¥10.00' in r['reply'] and '外带' in r['reply']
    assert fake.submits==0
    return r


def test_chat_store_menu_prepare_choose_confirm_query_and_duplicate(chat):
    env,model,fake,post=chat;r=prepare_chat(chat);pending=r['pending_confirmation']
    model.responses.append(lambda d:{'action':'confirm','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],'execute_revision':False})
    r=post('对，确认这单')
    assert r['overall_status']=='success' and fake.submits==1,r
    assert r['order']['order_id']=='order-1' and r['order']['status']=='待支付'
    assert '尚未完成' not in r['reply']
    id=r['checkout']['id']
    r=post('重复确认',confirmation={'action':'confirm','pending_id':pending['pending_id'],'version':pending['version']})
    assert fake.submits==1 and r['confirmation_replayed']
    fake.orders['order-1']['orderStatus']='已支付'
    r=post('查询',checkout_action={'action':'query','checkout_id':id,'version':r['checkout']['version']})
    assert r['order']['status']=='已支付' and fake.submits==1,r


def test_changed_price_creates_new_pending_and_old_confirm_never_submits(chat):
    env,model,fake,post=chat;r=prepare_chat(chat);p=r['pending_confirmation'];fake.price=1200
    r=post('确认',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert r['overall_status']=='confirmation_required' and fake.submits==0,r
    assert r['checkout']['version']==3 and r['checkout']['quote']['total_cents']==1200
    assert r['pending_confirmation']['pending_id']!=p['pending_id']
    new=r['pending_confirmation']
    r=post('确认新价格',confirmation={'action':'confirm','pending_id':new['pending_id'],'version':new['version']})
    assert r['overall_status']=='success' and fake.submits==1,r


def test_unknown_is_visible_and_no_second_dispatch(chat):
    env,model,fake,post=chat;r=prepare_chat(chat);p=r['pending_confirmation'];fake.timeout=True
    r=post('确认',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert r['overall_status']=='unknown' and r['checkout']['status']=='unknown' and fake.submits==1,r
    r=post('再确认',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert r['overall_status']=='unknown' and fake.submits==1


def test_unknown_body_ids_or_cross_thread_checkout_are_rejected(chat):
    env,model,fake,post=chat;r=prepare_chat(chat)
    response=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':'确认','thread':'other','checkout_action':{
        'action':'choose_take_way','checkout_id':r['checkout']['id'],'version':r['checkout']['version'],'take_way_code':'eat-in'}}).json()['data']
    assert response['overall_status']=='clarification_required' and fake.submits==0


def test_read_tools_advance_with_fresh_ids_in_one_chat_turn(chat):
    env,model,fake,post=chat
    def load(d):return {'snapshot_id':d['snapshot']['snapshot_id'],'mode':'load_context','context_request':{'commerce':True}}
    def search(d):return {**make_plan(d,'commerce.search_stores','commerce_account',city='北京',keyword='三里屯'),'continue_after_tools':True}
    def menu(d):return make_plan(d,'commerce.menu','commerce_store')
    model.responses.extend([load,search,menu])
    r=post('我在三里屯，查附近店再给我看最近一家菜单')
    assert r['overall_status']=='success' and r['commerce_catalog']['kind']=='products',r
    assert [t['capability_id'] for t in r['task_results']]==['commerce.search_stores','commerce.menu']
    assert fake.submits==0


def test_cancel_pending_checkout_and_late_confirm_cannot_order(chat):
    env,model,fake,post=chat;r=prepare_chat(chat);p=r['pending_confirmation']
    r=post('不买了',confirmation={'action':'cancel','pending_id':p['pending_id'],'version':p['version']})
    assert r['checkout']['status']=='cancelled',r
    post('迟到的确认',confirmation={'action':'confirm','pending_id':p['pending_id'],'version':p['version']})
    assert fake.submits==0


def test_model_repairs_inconsistent_candidates_without_python_selecting_store(chat):
    env,model,fake,post=chat;browse(chat)
    # Snapshot is created before planning: create second store before the request.
    with env.factory() as db:
        from app.domain.commerce import object_row
        from app.seed import DEMO_USER_ID
        object_row(db,DEMO_USER_ID,env.thread,'test-account','commerce_store','store2',payload={'name':'另一家'});db.commit()
    def invalid(d):
        plan=make_plan(d,'commerce.menu','commerce_store')
        stores=[o['id'] for o in d['snapshot']['objects'] if o['type']=='commerce_store']
        plan['tasks'][0]['resolution']['candidate_ids']=stores
        return plan
    def repair(d):
        assert d['validation_error']['task_id']=='t1'
        plan=d['previous_plan'];plan['tasks'][0]['resolution']['candidate_ids']=[plan['tasks'][0]['resolution']['selected_id']]
        return plan
    model.responses.extend([invalid,repair])
    r=post('看最近的店菜单');assert r['overall_status']=='success',r
    assert fake.submits==0


@pytest.mark.parametrize('broken_kind',['ownership_scope','dependency'])
def test_model_repair_uses_snapshot_ids_and_current_plan_dependencies(chat,broken_kind):
    env,model,fake,post=chat;browse(chat)
    def invalid(d):
        plan=make_plan(d,'commerce.menu','commerce_store')
        if broken_kind=='ownership_scope':plan['tasks'][0]['resolution']={'status':'resolved','selected_id':'invented-uuid','candidate_ids':[],'basis':'explicit'}
        else:plan['tasks'][0]['dependencies']=['already-executed-task']
        return plan
    def repair(d):
        plan=d['previous_plan'];store=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_store')
        plan['tasks'][0]['resolution']={'status':'resolved','selected_id':store['id'],'candidate_ids':[],'basis':'explicit'}
        plan['tasks'][0]['dependencies']=[]
        return plan
    model.responses.extend([invalid,repair]);r=post('看看这家店菜单')
    assert r['overall_status']=='success',r
    assert fake.submits==0


def test_requested_takeaway_uses_official_default_quote_without_submit(chat):
    env,model,fake,post=chat;browse(chat)
    calls_before=len(model.calls)
    def prep(d):
        product=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_product' and o['name']=='麦辣鸡翅')
        return {**make_plan(d,'commerce.prepare_order','commerce_store',items=[{'product_id':product['id'],'quantity':1}]),'continue_after_tools':True}
    model.responses.append(prep)
    r=post('一份鸡翅，拿走吃的，给我看报价')
    assert r['overall_status']=='confirmation_required' and r['checkout']['take_way_code']=='locker-out',r
    assert r['checkout']['status']=='awaiting_confirmation' and fake.submits==0
    assert r['checkout']['quote']['total_cents']==1000
    assert [t['capability_id'] for t in r['task_results']]==['commerce.prepare_order']
    assert len(model.calls)==calls_before+1, 'default takeaway needs no extra model/tool revision'
    assert [t['capability_id'] for t in r['pending_confirmation']['tasks']]==['commerce.submit_order']


def test_parameter_handoff_retires_old_authorization_but_revises_same_checkout(chat):
    env,model,fake,post=chat;r=prepare_chat(chat);old=r['pending_confirmation'];draft_id=r['checkout']['id']
    def control(d):return {'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],
        'execute_revision':False,'keep_task_ids':[],'new_request_text':'把刚才鸡翅数量改为两份，其余不变，只更新报价预览'}
    def revise(d):
        checkout=next(o for o in d['snapshot']['objects'] if o['type']=='checkout' and o['id']==draft_id)
        assert checkout['status']=='awaiting_confirmation'
        product=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_product' and o['name']=='麦辣鸡翅')
        return make_plan(d,'commerce.revise_order','checkout',version=checkout['version'],items=[{'product_id':product['id'],'quantity':2}])
    model.responses.extend([control,revise]);r=post('改成两份，先别提交')
    assert r['checkout']['id']==draft_id and r['checkout']['version']==3 and r['checkout']['cart'][0]['quantity']==2 and r['checkout']['quote']['total_cents']==2000,r
    post('迟到旧确认',confirmation={'action':'confirm','pending_id':old['pending_id'],'version':old['version']})
    assert fake.submits==0


def test_repair_can_rename_ephemeral_task_label_without_changing_operation(chat):
    env,model,fake,post=chat;browse(chat)
    def invalid(d):
        plan=make_plan(d,'commerce.menu','commerce_store');plan['tasks'][0]['dependencies']=['previously-executed'];return plan
    def repair(d):
        plan=d['previous_plan'];plan['tasks'][0]['task_id']='repaired-label';plan['tasks'][0]['dependencies']=[];return plan
    model.responses.extend([invalid,repair]);r=post('看菜单')
    assert r['overall_status']=='success' and fake.submits==0,r


def test_resolved_quote_preview_does_not_require_transaction_confirmation(chat):
    env,model,fake,post=chat;browse(chat)
    def quote(d):
        product=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_product' and o['name']=='麦辣鸡翅')
        plan=make_plan(d,'commerce.prepare_order','commerce_store',items=[{'product_id':product['id'],'quantity':1}])
        plan['tasks'][0]['needs_confirmation']=True;plan['tasks'][0]['resolution']['basis']='inferred'
        return plan
    model.responses.append(quote);r=post('你替我挑个在售的小食，给我预览就行')
    assert r['checkout']['status']=='awaiting_confirmation' and fake.submits==0,r
    assert r['checkout']['take_way_code']=='locker-out'
    assert [t['capability_id'] for t in r['pending_confirmation']['tasks']]==['commerce.submit_order']


@pytest.mark.parametrize('execute_old',[False,True])
def test_new_cart_revision_invalidates_carried_submit_even_if_controller_and_reviewer_keep_it(chat,execute_old):
    env,model,fake,post=chat;r=prepare_chat(chat);old=r['pending_confirmation'];id=r['checkout']['id']
    def control(d):return {'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],
        'execute_revision':execute_old,'keep_task_ids':[d['pending']['tasks'][0]['task_id']],
        'new_request_text':'鸡翅改成两份，其他不变，只修改预览，不提交'}
    model.responses.append(control)
    # Scripted's independent-review fixture deliberately approves all retained old tasks.
    def revise(d):
        checkout=next(o for o in d['snapshot']['objects'] if o['type']=='checkout' and o['id']==id)
        product=next(o for o in d['snapshot']['objects'] if o['type']=='commerce_product' and o['name']=='麦辣鸡翅')
        return make_plan(d,'commerce.revise_order','checkout',version=checkout['version'],items=[{'product_id':product['id'],'quantity':2}])
    model.responses.append(revise);r=post('不是一份，改成两份，只给我核对')
    assert fake.submits==0 and (r.get('checkout') or {}).get('id')==id and r['checkout']['cart'][0]['quantity']==2,r
    assert [t['capability_id'] for t in r['pending_confirmation']['tasks']]==['commerce.submit_order']
    assert r['pending_confirmation']['tasks'][0]['arguments']['version']==3
    new=r['pending_confirmation']
    post('旧确认',confirmation={'action':'confirm','pending_id':old['pending_id'],'version':old['version']});assert fake.submits==0
    r=post('确认新报价',confirmation={'action':'confirm','pending_id':new['pending_id'],'version':new['version']})
    assert fake.submits==1 and r['order']['total_cents']==2000,r


def test_confirm_order_plus_independent_fitness_request_preserves_order_receipt(chat):
    env,model,fake,post=chat;r=prepare_chat(chat)
    model.responses.extend([
        lambda d:{'action':'new_request','pending_id':d['pending']['pending_id'],'version':d['pending']['version'],
          'execute_revision':True,'keep_task_ids':[d['pending']['tasks'][0]['task_id']],
          'new_request_text':'把今天深蹲改为两组，其他不动'},
        lambda d:make_plan(d,'workout.update_action','workout_action',sets=2)])
    r=post('对，确认这单，顺便把深蹲改成两组')
    assert fake.submits==1 and r['overall_status']=='success' and r['order']['order_id']=='order-1',json.dumps(r,ensure_ascii=False)
    assert r['checkout']['status']=='submitted'
