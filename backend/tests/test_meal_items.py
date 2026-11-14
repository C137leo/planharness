import copy
from app.domain.meal_items import merge_food_items
from app.domain.catalog import display_action_name


def food(id,grams,**extra):
    return {'id':id,'name':'鸡胸肉','grams':grams,'kcal':grams*1.65,'protein':grams*.31,'fat':grams*.036,'carb':0,**extra}


def test_merge_same_food_keeps_id_and_sums_original_nutrition():
    a=food('a',150);a['kcal']=180
    b=food('b',100,ingredient_id='catalog-chicken',ingredient_key='鸡胸肉')
    original=copy.deepcopy([a,b]);result=merge_food_items(original)
    assert len(result)==1
    assert result[0]['id']=='a' and result[0]['grams']==250
    assert result[0]['kcal']==345 and result[0]['protein']==77.5
    assert original==[a,b]


def test_different_preparation_or_catalogue_are_separate():
    assert len(merge_food_items([food('a',100,preparation='水煮'),food('b',100,preparation='香煎')]))==2
    assert len(merge_food_items([food('a',100,ingredient_id='a'),food('b',100,ingredient_id='b')]))==2
    assert len(merge_food_items([food('a',100),{**food('b',100),'name':'牛肉'}]))==2


def test_targeted_merge_does_not_change_other_food_rows():
    chicken=food('a',150);rice={**food('r1',100),'name':'米饭'}
    items=[chicken,rice,{**rice,'id':'r2'},food('b',100)]
    result=merge_food_items(items,target=items[-1])
    assert [i['id'] for i in result]==['a','r1','r2']
    assert result[1:]==items[1:3]


def test_existing_english_name_can_display_localized_label():
    assert display_action_name({'action_ref':'jumping-jack','name':'Jumping Jack'})=='开合跳'
    assert display_action_name({'action_ref':'bicep-curl','name':'Bicep Curl'})=='哑铃弯举'
    assert display_action_name({'action_ref':'squat','name':'自定义深蹲'})=='自定义深蹲'
