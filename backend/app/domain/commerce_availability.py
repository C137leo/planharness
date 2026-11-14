"""Fresh provider facts and explicit store-switch consent; no user-language rules."""
import hashlib
import json
import math
from sqlalchemy import select
from app.models import CommerceObject
from app.domain.commerce import CheckoutError


class AvailabilityError(CheckoutError):
    def __init__(self,code,message,*,stores=None,choice=None,store=None,product=None):
        super().__init__(message)
        self.code,self.stores,self.choice,self.store,self.product=code,stores or [],choice,store,product


def distance(value):
    try:
        number=float(value)
        return number if not isinstance(value,bool) and math.isfinite(number) and number>=0 else math.inf
    except (TypeError,ValueError):return math.inf


def search(db,uid,thread,p,client,query,policy=None):
    from app.domain import commerce as c
    rows=client.call_read('query-nearby-stores',query)
    if not isinstance(rows,list):raise c.CheckoutError('门店返回格式不正确。')
    root=c.object_row(db,uid,thread,p.account_key,'commerce_account','mcd')
    previous=root.payload.get('store_search') or {}
    policy=policy or (previous.get('selection_policy','browse') if previous.get('query')==query else 'browse')
    stores=[]
    for raw in rows:
        if not isinstance(raw,dict) or not raw.get('storeCode'):continue
        row=c.object_row(db,uid,thread,p.account_key,'commerce_store',raw['storeCode'],payload={**c.store_payload(raw),'search':query})
        stores.append({'id':row.id,**row.payload})
    stores.sort(key=lambda x:(distance(x.get('distance_m')),x['id']))
    revision=hashlib.sha256(json.dumps({'query':query,'stores':[(x['id'],x['open'],x.get('distance_m'),x['name'],x['hours']) for x in stores]},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    opened=[x for x in stores if x['open']]
    context={'query':query,'selection_policy':policy,'revision':revision,'store_ids':[x['id'] for x in stores],
             'nearest_id':stores[0]['id'] if stores else None,'nearest_open_id':opened[0]['id'] if opened else None}
    root.payload={**root.payload,'store_search':context}
    db.commit()
    return stores,context


def current_context(db,uid,thread,p):
    root=db.scalar(select(CommerceObject).where(CommerceObject.user_id==uid,CommerceObject.thread==thread,
        CommerceObject.account_key==p.account_key,CommerceObject.kind=='commerce_account'))
    return root,((root.payload.get('store_search') or {}) if root else {})


def switch_error(stores,context,requested=None):
    opened=[x for x in stores if x['open']]
    if not stores:return AvailabilityError('no_stores','没有查到该位置附近的麦当劳餐厅，请换个地标或搜索范围。')
    if not opened:return AvailabilityError('no_open_store','该搜索范围内目前没有确认营业中的餐厅，请换个位置或稍后再试。',stores=stores,store=requested)
    target=opened[0];nearest=stores[0]
    reason=(f'距离最近的「{nearest["name"]}」目前未营业。' if not nearest['open'] else
            f'原选门店「{requested["name"]}」目前不可用。' if requested and not requested['open'] else '需要先确认这次使用的门店。')
    meters=target.get('distance_m');where=(f'，官方距离 {meters} 米' if distance(meters)!=math.inf else '，官方未提供可比较的距离')
    message=reason+f'当前最近的营业中餐厅是「{target["name"]}」'+where+f'，营业时间 {target["hours"]}。是否改为这家继续选餐？确认后仅准备报价，不会直接下单。'
    return AvailabilityError('store_switch_required',message,stores=stores,store=requested,choice={
        'store_id':target['id'],'search_revision':context['revision'],'store':target,'nearest':nearest})


def check(db,uid,thread,p,store,client,*,manual=False):
    from app.domain import commerce as c
    query=store.payload.get('search')
    if not query:raise c.CheckoutError('缺少门店搜索依据，请重新查询门店。')
    stores,context=search(db,uid,thread,p,client,query,'specific' if manual else None)
    current=next((x for x in stores if x['id']==store.id),None)
    if not current or not current['open']:raise switch_error(stores,context,current or {'name':store.payload['name'],'open':False})
    root,_=current_context(db,uid,thread,p)
    approved=root.payload.get('selected_store') or {}
    if not manual and context['selection_policy']=='nearest':
        if context['nearest_id']!=store.id and (approved.get('store_id')!=store.id or approved.get('search_revision')!=context['revision']):
            raise switch_error(stores,context,current)
    if manual:
        root.payload={**root.payload,'selected_store':{'store_id':store.id,'search_revision':context['revision']}}
        db.commit()
    return current


def choose(db,uid,thread,p,id,revision):
    from app.domain import commerce as c
    store=c.owned(db,uid,thread,p.account_key,id,'commerce_store');query=store.payload.get('search')
    if not query:raise c.CheckoutError('门店缺少当前搜索依据，请重新查询。')
    db.commit()
    with p.connect() as client:stores,context=search(db,uid,thread,p,client,query)
    current=next((x for x in stores if x['id']==id),None)
    if revision!=context['revision'] or not current or not current['open']:raise switch_error(stores,context,current)
    root,_=current_context(db,uid,thread,p)
    root.payload={**root.payload,'selected_store':{'store_id':id,'search_revision':revision}}
    db.commit();return current
