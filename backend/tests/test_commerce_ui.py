"""UI contracts through authenticated chat; actual DB, explicitly Mock provider."""
import pytest
from app.food.official_nutrition import parse_table
from tests.test_commerce_chat import chat, browse, prepare_chat
from app.api.coach_routes import coach_commerce_dependency

TABLE='[2]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n麦辣鸡翅,2块,937,224,14,14,8,100,0\n纯水,null,0,0,0,0,0,0,0'


def test_nutrition_parser_keeps_official_portions_and_zero_values():
    rows=parse_table(TABLE)
    assert rows[0]['portion']=='2块' and rows[0]['kcal']==224 and rows[0]['protein_g']==14
    assert rows[1]['kcal']==0
    with pytest.raises(ValueError):parse_table('not a table')


def test_manual_store_menu_default_quote_without_model_calls(chat):
    env,model,fake,post=chat
    r=post('查门店',commerce_action={'action':'search_stores','city':'北京','keyword':'三里屯'})
    store=r['commerce_catalog']['items'][0];assert 'hours' in store
    r=post('选择门店',commerce_action={'action':'menu','target_id':store['id']})
    assert r['commerce_catalog']['store_id']==store['id']
    product=r['commerce_catalog']['items'][0]
    r=post('只看预览',commerce_action={'action':'prepare_order','target_id':store['id'],
        'items':[{'product_id':product['id'],'quantity':2,'use_default_options':True}]})
    assert r['checkout']['cart'][0]['quantity']==2 and fake.submits==0
    assert model.calls==[]


def test_nutrition_is_structured_for_card_and_not_fuzzy_attached_to_products(chat):
    env,model,fake,post=chat;original=fake.call_read
    fake.discover=lambda:{n:{} for n in {'list-nutrition-foods','query-meals','query-meal-detail','calculate-price','query-nearby-stores'}}
    fake.call_read=lambda name,args:TABLE if name=='list-nutrition-foods' else original(name,args)
    r=post('营养',commerce_action={'action':'nutrition'})
    assert r['commerce_nutrition']['items'][0]['kcal']==224
    r=post('门店',commerce_action={'action':'search_stores','city':'北京','keyword':'三里屯'})
    store=r['commerce_catalog']['items'][0]['id']
    r=post('菜单',commerce_action={'action':'menu','target_id':store})
    assert r['commerce_catalog']['items'][0]['nutrition']['portion']=='2块'
    assert r['commerce_catalog']['items'][1]['nutrition'] is None


def test_missing_token_gets_setup_card_and_cannot_expose_token_in_history(chat):
    env,model,fake,post=chat
    env.app.dependency_overrides[coach_commerce_dependency]=lambda:None
    r=post('连接麦当劳',commerce_action={'action':'connect'})
    assert r['commerce_connection']['status']=='token_required'
    assert r['commerce_connection']['guide_url']=='https://open.mcd.cn/mcp'
    assert fake.submits==0 and model.calls==[]


def test_catalog_button_cannot_use_foreign_id_or_submit_order(chat):
    env,model,fake,post=chat
    r=post('选门店',commerce_action={'action':'menu','target_id':'unknown'})
    assert r['overall_status']=='clarification_required' and fake.submits==0
    response=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':'下单','commerce_action':{'action':'submit_order','target_id':'unknown'}})
    assert response.status_code==422 and fake.submits==0


def test_natural_language_missing_token_also_opens_setup_instead_of_claiming_no_capability(chat):
    env,model,fake,post=chat;env.app.dependency_overrides[coach_commerce_dependency]=lambda:None
    model.responses.append(lambda d:{'snapshot_id':d['snapshot']['snapshot_id'],'mode':'load_context','context_request':{'commerce':True}})
    r=post('帮我查一下三里屯的麦当劳')
    assert r['commerce_connection']['status']=='token_required' and fake.submits==0


def test_authentication_failure_keeps_token_input_available(chat):
    from app.food.mcd_transport import McpAuthenticationError
    env,model,fake,post=chat
    def reject():raise McpAuthenticationError('mcp_authentication_failed')
    fake.discover=reject
    r=post('连接',commerce_action={'action':'connect'})
    assert r['commerce_connection']['status']=='token_required' and fake.submits==0
