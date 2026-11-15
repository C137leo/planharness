"""User-scoped pickup checkout. No natural language or legacy order handlers."""
import copy
import datetime as dt
import hashlib
import json
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse, parse_qs
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker
from app.models import Profile, CommerceObject, CheckoutDraft, ExternalOperation, User, new_id, utcnow
from app.domain.timeutil import as_utc
from app.domain import external_operations, order_lifecycle
from app.food.commerce_contract import CartLine
from app.food.mcd_transport import McpBusinessError, McpAuthenticationError


class CheckoutError(ValueError):
    pass


class CommerceCredentialError(CheckoutError):
    pass


@dataclass
class CommerceProvider:
    account_key: str
    client_factory: object
    allow_orders: bool = False

    @contextmanager
    def connect(self):
        client = self.client_factory()
        try:
            yield client
        finally:
            client.close()


def factory_for(db):
    return sessionmaker(bind=db.get_bind(), expire_on_commit=False, autoflush=False)


def lock(db, user_id):
    if db.bind.dialect.name == 'sqlite':
        db.execute(update(User).where(User.id == user_id).values(id=User.id, updated_at=User.updated_at))
    else:
        db.scalar(select(User).where(User.id == user_id).with_for_update())
    db.expire_all()


def provider(db):
    value = db.info.get('commerce_provider')
    if value is None:
        raise CommerceCredentialError('请先连接你的麦当劳 MCP Token。')
    return value


def object_row(db, user_id, thread, account, kind, external_key, parent='', payload=None):
    row = db.scalar(select(CommerceObject).where(CommerceObject.user_id==user_id, CommerceObject.thread==thread,
                    CommerceObject.account_key==account, CommerceObject.kind==kind,
                    CommerceObject.external_key==str(external_key), CommerceObject.parent_key==parent))
    if row is None:
        row=CommerceObject(id=new_id(),user_id=user_id,thread=thread,account_key=account,kind=kind,
                           external_key=str(external_key),parent_key=parent,payload=payload or {})
        db.add(row)
    elif payload is not None:
        row.payload=copy.deepcopy(payload)
    if payload is not None:
        row.observed_at=utcnow()
    db.flush()
    return row


def owned(db, user_id, thread, account, id, kind):
    row=db.get(CommerceObject,id)
    if row is None or row.user_id!=user_id or row.thread!=thread or row.account_key!=account or row.kind!=kind:
        raise CheckoutError('商品或门店不属于当前用户、账号及会话。')
    return row


def draft_owned(db,user_id,thread,account,id):
    row=db.get(CheckoutDraft,id)
    if row is None or (row.user_id,row.thread,row.account_key)!=(user_id,thread,account):
        raise CheckoutError('订单草案不属于当前用户、账号及会话。')
    return row


def public_draft(row):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    zone = row.quote.get('timezone') or 'Asia/Shanghai'
    try:
        local_expiry = as_utc(row.expires_at).astimezone(ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError):
        zone = 'Asia/Shanghai'
        local_expiry = as_utc(row.expires_at).astimezone(ZoneInfo(zone))
    return {'id':row.id,'type':'checkout','version':row.version,'status':row.status,'cart':row.cart,
            'store_id':row.store_id,
            'quote':row.quote,'take_way_code':row.take_way_code,'expires_at':as_utc(row.expires_at).isoformat(),
            'expires_at_label':local_expiry.strftime('%Y-%m-%d %H:%M')+'（'+zone+'）',
            'operation_id':row.operation_id,'name':'麦当劳订单预览',
            'order':order_lifecycle.present(row.quote.get('order'),utcnow()) if row.quote.get('order') else None,
            'order_conflict':row.quote.get('order_conflict')}


def snapshot_objects(db,user_id,thread,p):
    root=object_row(db,user_id,thread,p.account_key,'commerce_account','mcd',payload=None)
    rows=list(db.scalars(select(CommerceObject).where(CommerceObject.user_id==user_id,CommerceObject.thread==thread,
                      CommerceObject.account_key==p.account_key)))
    stores=[r for r in rows if r.kind=='commerce_store' and r.payload.get('menu_observed_at')]
    latest=max(stores,key=lambda r:r.payload['menu_observed_at']).id if stores else None
    focused=(root.payload.get('focused_product') or {}).get('product_id')
    product_ids={r.id for r in rows if r.kind=='commerce_product' and (r.parent_key==latest and r.payload.get('available',True) or r.id==focused)}
    selected=[r for r in rows if r.kind not in {'commerce_product','commerce_option'}
              or r.kind=='commerce_product' and r.id in product_ids
              or r.kind=='commerce_option' and r.parent_key in product_ids and r.payload.get('active',True)]
    result=[{'id':r.id,'type':r.kind,**{k:v for k,v in r.payload.items() if k!='detail'},
             'parent_id':r.parent_key or None,'observed_at':as_utc(r.observed_at).isoformat()} for r in selected]
    for obj in result:
        if obj['type']=='commerce_product' and obj.get('detail_summary'):
            obj['menu_name']=obj['name']
            obj['name']=obj['detail_summary']['name']
        if obj['id']==root.id:
            obj.update(name='麦当劳 MCP', context_loaded=bool(root.payload.get('tools')), order_submission_enabled=p.allow_orders)
            obj['saved_location']=pickup_preferences(db,user_id)
            obj['store_search']=root.payload.get('store_search')
            obj['selected_store']=root.payload.get('selected_store')
    drafts=list(db.scalars(select(CheckoutDraft).where(CheckoutDraft.user_id==user_id,CheckoutDraft.thread==thread,
                   CheckoutDraft.account_key==p.account_key).order_by(CheckoutDraft.created_at.desc()).limit(5)))
    result.extend(public_draft(r) for r in drafts)
    return result


def load_context(db,user_id,thread,p):
    db.commit()
    with p.connect() as client:
        tools=client.discover()
    root=object_row(db,user_id,thread,p.account_key,'commerce_account','mcd')
    root.payload={**root.payload,'tools':sorted(tools)}
    db.commit()
    return root.payload['tools']


def nutrition_table(db,uid,thread,p,client):
    from app.food.official_nutrition import parse_table
    root=object_row(db,uid,thread,p.account_key,'commerce_account','mcd')
    cached=root.payload.get('nutrition')
    if cached and (utcnow()-as_utc(dt.datetime.fromisoformat(cached['fetched_at']))).total_seconds()<21600:
        return copy.deepcopy(cached)
    db.commit()
    items=parse_table(client.call_read('list-nutrition-foods',{}))
    value={'items':items,'fetched_at':utcnow().isoformat(),'source':'麦当劳官方营养库','portion_note':'按官方条目对应份量；不同大小、配方与套餐不能直接套用。'}
    root.payload={**root.payload,'nutrition':value};db.flush()
    return value


def store_payload(row):
    return {'provider_code':str(row['storeCode']),'name':str(row.get('storeName') or ''),
            'address':str(row.get('address') or ''),'distance_m':row.get('distance'),
            'open':row.get('businessStatus') in (True,1),'availability':'open' if row.get('businessStatus') in (True,1) else 'closed' if row.get('businessStatus') in (False,0) else 'unknown','reservation':bool(row.get('reservation')),
            'hours':str(row.get('businessStartTime') or '')+'–'+str(row.get('businessEndTime') or '')}


def save_menu(db,uid,thread,p,store,data):
    meals=data.get('meals')
    if not isinstance(meals,dict):raise CheckoutError('门店菜单格式无法验证。')
    active=[]
    for code, raw in meals.items():
        if not isinstance(raw,dict) or not raw.get('name'):continue
        data={'provider_code':str(code),'name':raw['name'],'menu_price':raw.get('currentPrice'),
              'product_type':raw.get('productType'),'store_id':store.id,'available':True}
        product=object_row(db,uid,thread,p.account_key,'commerce_product',str(code),store.id)
        cached={k:product.payload[k] for k in ('detail','detail_summary') if k in product.payload} if all(product.payload.get(k)==v for k,v in data.items()) else {}
        product.payload={**data,**cached}
        product.observed_at=utcnow()
        active.append(product.id)
    for old in db.scalars(select(CommerceObject).where(CommerceObject.user_id==uid,CommerceObject.thread==thread,CommerceObject.account_key==p.account_key,CommerceObject.kind=='commerce_product',CommerceObject.parent_key==store.id)):
        if old.id not in active:old.payload={**old.payload,'available':False}
    store.payload={**store.payload,'product_ids':active,'menu_observed_at':utcnow().isoformat()}
    db.flush()
    return active


def modification_summary(value):
    groups=[]
    for index,group in enumerate((value or {}).get('items') or [],1):
        groups.append({'name':str(group.get('name') or '官方特制组 '+str(index)),
            'min_values':group.get('minValues'),'max_values':group.get('maxValues'),
            'choices':[{'code':str(v.get('code') or ''),'name':str(v.get('name') or ''),
                        'selected_quantity':v.get('selectedQuantity',0),'price_cents':v.get('price'),
                        'selected_key':v.get('selectedKey')} for v in group.get('values',[])]})
    return groups


def save_detail(db,uid,thread,p,product,detail):
    if not isinstance(detail,dict) or str(detail.get('code'))!=product.external_key:
        raise CheckoutError('商品详情与所选商品不一致。')
    # Retire cached options no longer present; keep their IDs for history.
    for row in db.scalars(select(CommerceObject).where(CommerceObject.user_id==uid,CommerceObject.thread==thread,
            CommerceObject.account_key==p.account_key,CommerceObject.kind=='commerce_option',CommerceObject.parent_key==product.id)):
        row.payload={**row.payload,'active':False}
    rounds=[]
    for group in detail.get('rounds') or []:
        choices=[]
        for choice in group.get('choices') or []:
            row=object_row(db,uid,thread,p.account_key,'commerce_option',str(group['id'])+':'+str(choice['code']),product.id,
                {'name':choice.get('name'),'round_id':str(group['id']),'round_name':group.get('name'),
                 'provider_code':str(choice['code']),'product_id':product.id,'default':choice.get('isDefault')==1,
                 'min_quantity':group.get('minQuantity'),'max_quantity':group.get('maxQuantity'),'active':True})
            choices.append({'id':row.id,'name':choice.get('name'),'default':choice.get('isDefault')==1,
                            'quantity':choice.get('quantity'),'modification_groups':modification_summary(choice.get('modification'))})
        rounds.append({'id':str(group['id']),'name':group.get('name'),'choices':choices,
                       'min_quantity':group.get('minQuantity'),'max_quantity':group.get('maxQuantity')})
    mods=modification_summary(detail.get('modification'))
    summary={'product_id':product.id,'provider_code':product.external_key,'name':detail.get('name') or product.payload['name'],
             'menu_name':product.payload['name'],
             'detail_name':detail.get('name'),'store_id':product.parent_key,'round_groups':rounds,
             'modification_groups':mods,'option_group_count':len(rounds)+len(mods),'observed_at':utcnow().isoformat()}
    product.payload={**product.payload,'detail':detail,'detail_summary':summary}
    root=object_row(db,uid,thread,p.account_key,'commerce_account','mcd')
    root.payload={**root.payload,'focused_product':{'product_id':product.id,'store_id':product.parent_key,
                  'name':product.payload['name'],'source':'product_detail','authorization':'read_only',
                  'observed_at':summary['observed_at']}}
    db.flush()


def default_modification(item):
    values=[]
    for group in (item.get('modification') or {}).get('items') or []:
        selected=[v for v in group.get('values',[]) if int(v.get('selectedQuantity') or 0)>0]
        if len(selected)<int(group.get('minValues') or 0) or len(selected)>int(group.get('maxValues') or 100):
            raise CheckoutError('商品存在未明确或超出范围的必选特制项，请在官方客户端完善。')
        for value in group.get('values',[]):
            quantity=int(value.get('selectedQuantity') or 0)
            if quantity<0:raise CheckoutError('商品默认规格数量不合法。')
            key=value.get('selectedKey') if quantity else value.get('unselectedKey')
            if not quantity and key in (None,''):continue
            if not value.get('code') or key is None:raise CheckoutError('无法验证商品默认规格。')
            values.append({'code':str(value['code']),'key':str(key),'quantity':quantity})
    return {'values':values} if values else None


def build_cart(db,uid,thread,p,store,lines,client,menu):
    save_menu(db,uid,thread,p,store,menu)
    db.commit()
    cart=[];seen=set()
    for raw in lines:
        line=CartLine.model_validate(raw)
        if line.product_id in seen:raise CheckoutError('同一商品请合并数量后再准备订单。')
        seen.add(line.product_id)
        product=owned(db,uid,thread,p.account_key,line.product_id,'commerce_product')
        if product.parent_key!=store.id:raise CheckoutError('商品不属于所选门店，请重新选择。')
        if product.id not in store.payload['product_ids']:
            from app.domain.commerce_availability import AvailabilityError
            raise AvailabilityError('product_unavailable',f'「{store.payload["name"]}」当前菜单已没有「{product.payload["name"]}」，可能已售罄或下架。请查看最新菜单重新选择，本次没有创建订单。',store={'id':store.id,**store.payload},product={'id':product.id,'name':product.payload['name']})
        db.commit()
        detail=client.call_read('query-meal-detail',{'storeCode':store.external_key,'orderType':1,'beType':1,'code':product.external_key})
        save_detail(db,uid,thread,p,product,detail)
        groups=detail.get('rounds') or []; round_list=[];spec=[]
        selections={}
        for selection in line.selections:
            option=owned(db,uid,thread,p.account_key,selection.option_id,'commerce_option')
            if option.parent_key!=product.id or not option.payload.get('active',True) or option.external_key in selections:
                raise CheckoutError('套餐选项重复或不属于所选商品。')
            selections[option.external_key]=selection.quantity
        consumed=set()
        for group in groups:
            picked=[];total=0
            for choice in group.get('choices') or []:
                key=str(group['id'])+':'+str(choice['code'])
                quantity=selections.get(key,0)
                if not line.selections and line.use_default_options and choice.get('isDefault')==1:
                    quantity=int(choice.get('quantity') or 1)
                if quantity:
                    maximum=int(choice.get('maxQuantity') or -1)
                    if maximum>=0 and quantity>maximum:raise CheckoutError('套餐选项数量超出限制。')
                    entry={'code':str(choice['code']),'quantity':quantity}
                    mod=default_modification(choice)
                    if mod:entry['modification']=mod
                    picked.append(entry);total+=quantity;consumed.add(key)
                    spec.append({'name':choice['name'],'quantity':quantity})
            if not int(group.get('minQuantity') or 0)<=total<=int(group.get('maxQuantity') or group.get('quantity') or 20):
                raise CheckoutError('套餐必选项未选完整，请先查看商品详情并选择规格，或明确使用官方默认套餐。')
            round_list.append({'round':str(group['id']),'comboItemList':picked})
        if set(selections)-consumed:raise CheckoutError('套餐选项已变化，请重新选择。')
        official={'productCode':product.external_key,'quantity':line.quantity}
        if round_list:official['roundList']=round_list
        mod=default_modification(detail)
        if mod:official['modification']=mod
        cart.append({'product_id':product.id,'name':detail.get('name') or product.payload['name'],'quantity':line.quantity,
                     'specifications':spec,'provider_names':list(dict.fromkeys([product.payload['name'],detail.get('name') or product.payload['name']])),
                     'official':official,'selection':line.model_dump()})
    if sum(x['quantity'] for x in cart)>100:raise CheckoutError('订单商品数量超出单次限制。')
    return cart


def quote_cart(client,store,cart):
    data=client.call_read('calculate-price',{'storeCode':store.external_key,'orderType':1,'beType':1,
          'items':[x['official'] for x in cart],'needTableware':False})
    try:
        amount=Decimal(str(data['price']))
        if not amount.is_finite() or amount<0 or amount!=amount.to_integral_value():raise ValueError()
    except (KeyError,ValueError,ArithmeticError):raise CheckoutError('接口没有返回可验证的订单总额。') from None
    ways=[{'code':str(w['code']),'title':str(w.get('title') or w['code'])} for w in data.get('takeWayList',[]) if w.get('code')]
    return {'total_cents':int(amount),'currency':'CNY','take_ways':ways,'store':copy.deepcopy(store.payload),
            'fulfillment':'到店自取','tableware':False,'fees_note':'总额以官方算价为准；接口未单列的费用不另行推测。'}


def quote_signature(cart,quote,take_way):
    return {'items':[{'name':x['name'],'specifications':x['specifications'],'official':x['official']} for x in cart],
            'total_cents':quote['total_cents'],'store_code':quote['store']['provider_code'],
            'store_name':quote['store']['name'],'address':quote['store']['address'],
            'take_way':take_way,'ways':sorted(quote['take_ways'],key=lambda w:w['code'])}


def quote_preview(db,uid,thread,p,store_id,lines,take_way,*,key=None,draft_id=None,version=None,take_way_preference=None):
    store=owned(db,uid,thread,p.account_key,store_id,'commerce_store')
    db.commit()
    with p.connect() as client:
        from app.domain import commerce_availability as availability
        availability.check(db,uid,thread,p,store,client)
        menu=client.call_read('query-meals',{'storeCode':store.external_key,'orderType':1,'beType':1})
        cart=build_cart(db,uid,thread,p,store,lines,client,menu)
        db.commit()
        quote=quote_cart(client,store,cart)
    preference=take_way_preference or 'takeaway'
    # These are provider option labels, not user-language parsing or fixed codes.
    titles={'takeaway':{'外带','打包','带走'},'dine_in':{'堂食'}}[preference]
    if not take_way:
        matching=[w for w in quote['take_ways'] if w['title'] in titles]
        if len(matching)==1:
            take_way=matching[0]['code']
            quote['take_way_source']='explicit_preference' if take_way_preference else 'default_takeaway'
    elif take_way_preference and not any(w['code']==take_way and w['title'] in titles for w in quote['take_ways']):
        raise CheckoutError('取餐方式代码与要求不一致，请重新选择。')
    if take_way and take_way not in {x['code'] for x in quote['take_ways']}:
        raise CheckoutError('取餐方式不在当前报价选项中。')
    lock(db,uid)
    if draft_id:
        row=draft_owned(db,uid,thread,p.account_key,draft_id)
        if row.version!=version or row.status in {'submitting','submitted','unknown','verifying','failed'}:
            raise CheckoutError('草案版本已变化或已提交，不能修改。')
        row.version+=1
    else:
        row=db.scalar(select(CheckoutDraft).where(CheckoutDraft.user_id==uid,CheckoutDraft.request_key==key))
        if row:
            if (row.thread!=thread or row.account_key!=p.account_key or row.store_id!=store_id or row.take_way_code!=take_way
                    or [x['selection'] for x in row.cart]!=[CartLine.model_validate(x).model_dump() for x in lines]):
                raise CheckoutError('订单请求标识冲突。')
            return row
        row=CheckoutDraft(id=new_id(),user_id=uid,thread=thread,account_key=p.account_key,store_id=store_id,request_key=key)
        db.add(row)
    profile = db.get(Profile, uid)
    quote = {**quote, 'timezone': getattr(profile, 'timezone', 'Asia/Shanghai')}
    row.cart,row.quote,row.take_way_code=cart,quote,take_way
    row.status='awaiting_confirmation' if take_way else 'needs_selection'
    row.expires_at=utcnow()+dt.timedelta(minutes=5)
    db.flush();return row


def preview_text(row):
    q=row.quote;ways={x['code']:x['title'] for x in q['take_ways']}
    items=['、'.join(x['name']+'×'+str(x['quantity']) for x in row.cart)]
    specs=[s['name']+'×'+str(s['quantity']) for x in row.cart for s in x['specifications']]
    if specs:items.append('套餐组成：'+'、'.join(specs))
    items.extend(['门店：'+q['store']['name']+'；'+q['store']['address'],
                  '方式：到店自取；'+ways.get(row.take_way_code,'请先选择取餐方式'),
                  '官方总额：¥'+format(Decimal(q['total_cents'])/100,'.2f'),
                  '餐具：不需要；本次将创建待支付订单，不会自动付款。'])
    return '\n'.join(items)


def pickup_preferences(db,uid):
    row=db.scalar(select(CommerceObject).where(CommerceObject.user_id==uid,CommerceObject.thread=='__preferences__',
        CommerceObject.account_key=='user-settings',CommerceObject.kind=='commerce_preferences',CommerceObject.external_key=='mcd'))
    return {'city':(row.payload.get('city') or '') if row else '', 'keyword':(row.payload.get('keyword') or '') if row else ''}


def save_pickup_preferences(db,uid,city,keyword):
    city,keyword=city.strip(),keyword.strip()
    if bool(city)!=bool(keyword):raise CheckoutError('请同时填写默认城市和地标，或同时清空。')
    object_row(db,uid,'__preferences__','user-settings','commerce_preferences','mcd',payload={'city':city,'keyword':keyword})
    db.flush();return pickup_preferences(db,uid)


def cancel_draft(db,uid,thread,p,id,version):
    lock(db,uid);row=draft_owned(db,uid,thread,p.account_key,id)
    if row.version!=version:raise CheckoutError('草案版本已变化。')
    if row.status in {'submitted','unknown','submitting','verifying'}:
        raise CheckoutError('订单已提交或结果待核实，不能当作未提交草案取消。请先查询订单。')
    row.status='cancelled';db.flush();return row


def verify_remote(data,remote_id,cart,quote,take_way):
    if not isinstance(data,dict):return False
    detail=data.get('orderDetail') if isinstance(data.get('orderDetail'),dict) else data
    rows=detail.get('orderProductList')
    if str(data.get('orderId') or detail.get('orderId') or '')!=remote_id or not isinstance(rows,list):return False
    try:
        if Decimal(str(detail['totalAmount']))*100!=quote['total_cents']:return False
    except (KeyError,ValueError,ArithmeticError):return False
    store=quote['store']
    if detail.get('storeCode'):
        if str(detail['storeCode'])!=store['provider_code']:return False
    elif (detail.get('storeName'),detail.get('storeAddress'))!=(store['name'],store['address']):
        return False
    title=next((w['title'] for w in quote['take_ways'] if w['code']==take_way),None)
    if not (detail.get('takeWayCode')==take_way or title and detail.get('takeWay')==title):return False
    if detail.get('orderType',1)!=1 or detail.get('beType',1)!=1:return False
    # Actual discovered query-order schema returns productName/comboItemList,
    # not productCode/roundList. Accept exact provider names only when unique;
    # never use fuzzy semantic matching to verify a financial side effect.
    remaining=list(rows)
    for item in cart:
        official=item['official'];names=item.get('provider_names') or [item['name']]
        matches=[r for r in remaining if (str(r.get('productCode'))==official['productCode']
                 if r.get('productCode') else r.get('productName') in names)]
        if len(matches)!=1:return False
        remote=matches[0];remaining.remove(remote)
        if remote.get('quantity')!=official['quantity']:return False
        if official.get('roundList'):
            if remote.get('roundList') is not None:
                if remote['roundList']!=official['roundList']:return False
            else:
                flat=remote.get('comboItemList')
                if not isinstance(flat,list):return False
                try:
                    actual=Counter((r['itemName'],r['itemQuantity']) for r in flat)
                    expected=Counter((s['name'],s['quantity']) for s in item['specifications'])
                    if actual!=expected:return False
                except (KeyError,TypeError):return False
                # Flat labels cannot prove hidden modifier codes/keys. Keep UNKNOWN
                # if the provider omits evidence needed for exact verification.
                if any(c.get('modification') for g in official['roundList'] for c in g['comboItemList']):return False
        if official.get('modification') and remote.get('modification')!=official['modification']:return False
    return not remaining and bool(detail.get('orderStatus'))


def official_pay_url(value, order_id=None):
    url=str(value or "")
    if any(ord(c)<33 for c in url):return ""
    try:
        parsed=urlparse(url)
        port=parsed.port
    except ValueError:
        return ""
    if (parsed.scheme=="https" and (parsed.hostname=="mcd.cn" or (parsed.hostname or "").endswith(".mcd.cn"))
            and not parsed.username and not parsed.password and port in (None,443)):
        ids=parse_qs(parsed.query).get("orderId",[])
        if order_id and ids and ids!=[order_id]:return ""
        return url
    return ""


def order_observation(data,remote_id,quote,verified,receipt=None):
    prior=receipt or {}
    detail=data.get("orderDetail") if isinstance(data.get("orderDetail"),dict) else data
    matches=str(data.get("orderId") or detail.get("orderId") or "")==remote_id
    raw=str(detail.get("orderStatus") or "") if matches else str(prior.get("status_code") or "")
    labels={"1":"待支付","2":"已支付，配餐中","4":"配送中","6":"已完成","7":"已取消","8":"已评价","10":"餐厅配餐中"}
    url=official_pay_url((data.get("payH5Url") or detail.get("payH5Url")) if matches else "",remote_id) or official_pay_url(prior.get("pay_url"),remote_id)
    return {"order_id":remote_id,"status":labels.get(raw,raw) or "待核实", "status_code":raw,
        "status_verified":bool(matches and raw in order_lifecycle.STATUS) or bool(prior.get("status_verified") and not matches),
        "observed_at":utcnow().isoformat() if matches else prior.get("observed_at", ""),
        "created_at":prior.get("created_at") or quote.get("order_created_at", ""),
        "total_cents":quote["total_cents"],"pay_url":url,"verified":bool(verified),
        "expire_at":str(detail.get("expirePayTime") or prior.get("expire_at") or "") if matches else str(prior.get("expire_at") or "")}


def account_draft(db,uid,p,id):
    row=db.get(CheckoutDraft,id)
    if row is None or row.user_id!=uid or row.account_key!=p.account_key:
        raise CheckoutError('订单不属于当前用户及麦当劳账号。')
    return row


def existing_orders(db,uid,p,new_id,*,refresh=False):
    rows=list(db.scalars(select(CheckoutDraft).where(CheckoutDraft.user_id==uid,CheckoutDraft.account_key==p.account_key,
        CheckoutDraft.status.in_(['unknown','submitting','verifying','submitted']),CheckoutDraft.id!=new_id).order_by(CheckoutDraft.created_at)))
    conflicts=[]
    for row in rows:
        observed=order_lifecycle.present(row.quote.get('order'),utcnow())
        if observed['lifecycle'] in {'cancelled','expired','paid'}:continue
        if refresh:
            query_checkout(db,uid,row.thread,p,row.id)
            row=account_draft(db,uid,p,row.id)
            observed=order_lifecycle.present(row.quote.get('order'),utcnow())
            if observed['lifecycle'] in {'cancelled','expired','paid'}:continue
        started=order_lifecycle.provider_time(row.quote.get('order_created_at')) or as_utc(row.created_at)
        inflight=row.status in {'submitting','verifying'} and (utcnow()-started).total_seconds()<120
        conflicts.append({'checkout_id':row.id,'version':row.version,'order':observed,
            'cart':[{'name':x['name'],'quantity':x['quantity']} for x in row.cart],
            'store_name':row.quote.get('store',{}).get('name',''),'in_flight':inflight,
            'can_cancel':observed['lifecycle']=='unpaid' and bool(observed.get('order_id')),
            'can_create_separately':not inflight})
    return conflicts


def conflict_view(draft,orders):
    payload={'new_checkout':draft.id,'version':draft.version,'quote':quote_signature(draft.cart,draft.quote,draft.take_way_code),
             'existing':[(x['checkout_id'],x['order'].get('order_id'),x['order']['lifecycle'],x['in_flight']) for x in orders]}
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return {'fingerprint':fingerprint,'orders':orders,'can_create_separately':all(x['can_create_separately'] for x in orders)}


def conflict_result(db,draft,orders):
    conflict=conflict_view(draft,orders)
    draft.quote={**draft.quote,'order_conflict':conflict};draft.status='awaiting_confirmation';db.commit()
    unpaid=any(x['order']['lifecycle']=='unpaid' for x in orders)
    message=('已有未支付且未过期的订单。可继续支付、取消旧单，或保留旧单另下一单。' if unpaid else
             '已有提交结果尚不明确的订单，不能当作已过期或重发旧请求。可先查询核实；若明确要另下一单，可单独确认。')
    if not conflict['can_create_separately']:message='另一笔订单正在提交，请稍后查询结果；本次新预览已保留。'
    return {'status':'existing_order','checkout':public_draft(draft),'order_conflict':conflict,'message':message}


def refresh_conflict(db,uid,thread,p,id,version):
    draft=draft_owned(db,uid,thread,p.account_key,id)
    if draft.version!=version:raise CheckoutError('新订单预览已变化，请使用最新版本。')
    orders=existing_orders(db,uid,p,id,refresh=True)
    lock(db,uid);draft=draft_owned(db,uid,thread,p.account_key,id)
    if draft.status!='awaiting_confirmation':raise CheckoutError('新订单已结束或正在提交，请查询结果。')
    if orders:return conflict_result(db,draft,orders)
    draft.quote={k:v for k,v in draft.quote.items() if k!='order_conflict'};db.commit()
    return {'status':'confirmation_required','checkout':public_draft(draft),'message':'旧订单已不再阻塞新单。请核对本次预览后确认创建。'}


def submit(db,uid,thread,p,id,version,*,confirmed,existing_order_decision=''):
    if not confirmed:raise CheckoutError('订单必须在完整预览后明确确认。')
    if not p.allow_orders:raise CheckoutError('当前配置关闭了订单写操作。')
    lock(db,uid);draft=draft_owned(db,uid,thread,p.account_key,id)
    if draft.status in {'submitted','unknown','submitting','verifying'}:
        return query_checkout(db,uid,thread,p,id)
    if draft.version!=version:raise CheckoutError('确认的订单草案版本已过期，请查看最新报价。')
    if draft.status!='awaiting_confirmation' or as_utc(draft.expires_at)<=utcnow():
        raise CheckoutError('草案未准备完整、已取消或报价已过期，请重新报价。')
    # Reserve the new intent while refreshing other orders without holding a lock.
    # Two different simultaneous drafts cannot both pass the account-level gate.
    draft.status='verifying';draft.quote={**draft.quote,'order_created_at':utcnow().isoformat()};db.commit()
    try:existing_orders(db,uid,p,id,refresh=True)
    except Exception:
        db.rollback();lock(db,uid);draft=draft_owned(db,uid,thread,p.account_key,id)
        draft.status='awaiting_confirmation';db.commit();raise
    lock(db,uid);draft=draft_owned(db,uid,thread,p.account_key,id)
    if draft.version!=version or draft.status!='verifying':raise CheckoutError('新订单状态已变化，请查询。')
    conflicts=existing_orders(db,uid,p,id)
    if conflicts:
        current=conflict_view(draft,conflicts)
        if (not current['can_create_separately'] or not existing_order_decision or existing_order_decision!=current['fingerprint']
                or existing_order_decision!=(draft.quote.get('order_conflict') or {}).get('fingerprint')):
            return conflict_result(db,draft,conflicts)
    draft.quote={k:v for k,v in draft.quote.items() if k!='order_conflict'}
    old_signature=quote_signature(draft.cart,draft.quote,draft.take_way_code)
    # Reserve this exact draft before network preflight; competing confirmations
    # may query the claim but cannot send another create-order.
    draft.status='submitting';db.commit()
    try:
        with p.connect() as client:
            store=owned(db,uid,thread,p.account_key,draft.store_id,'commerce_store')
            search=store.payload.get('search')
            if not search:raise CheckoutError('缺少门店搜索依据，请重新查询门店。')
            rows=client.call_read('query-nearby-stores',search)
            fresh=next((x for x in rows if str(x.get('storeCode'))==store.external_key),None)
            if fresh is None or fresh.get('businessStatus') not in (True,1):
                from app.domain import commerce_availability as availability
                stores,context=availability.search(db,uid,thread,p,client,search)
                raise availability.switch_error(stores,context,{'name':store.payload['name'],'open':False})
            store.payload={**store.payload,**store_payload(fresh)}
            db.commit()
            menu=client.call_read('query-meals',{'storeCode':store.external_key,'orderType':1,'beType':1})
            cart=build_cart(db,uid,thread,p,store,[x['selection'] for x in draft.cart],client,menu)
            db.commit()
            quote=quote_cart(client,store,cart)
            if old_signature!=quote_signature(cart,quote,draft.take_way_code):
                draft.cart,draft.quote=cart,quote;draft.version+=1
                if draft.take_way_code not in {x['code'] for x in quote['take_ways']}:draft.take_way_code=''
                draft.status='awaiting_confirmation' if draft.take_way_code else 'needs_selection'
                draft.expires_at=utcnow()+dt.timedelta(minutes=5);db.commit()
                return {'status':'confirmation_required' if draft.take_way_code else 'needs_selection','checkout':public_draft(draft),'message':'订单内容或价格已变化，请重新确认。\n'+preview_text(draft)}
            arguments={'storeCode':store.external_key,'orderType':1,'beType':1,'items':[x['official'] for x in cart],
                       'takeWayCode':draft.take_way_code,'needTableware':False}
            factory=factory_for(db);db.commit()
            created={};rejected=[]
            def dispatch():
                dispatched_at=utcnow().isoformat()
                try:result=client.submit_pickup(arguments)
                except (McpBusinessError,McpAuthenticationError) as exc:
                    rejected.append(exc)
                    raise external_operations.DefiniteRejection() from exc
                remote_id=str(result.get('orderId') or '')
                receipt=order_observation(result,remote_id,{**quote,'order_created_at':dispatched_at},False)
                created.update(receipt)
                return external_operations.DispatchReceipt(remote_id,receipt)
            def verify(remote_id):
                data=client.call_read('query-order',{'orderId':remote_id})
                okay=verify_remote(data,remote_id,cart,quote,draft.take_way_code)
                return okay, order_observation(data,remote_id,quote,okay,created)
            outcome=external_operations.execute(factory,user_id=uid,provider='mcd:'+p.account_key[:16],
                capability_id='commerce.submit_order',request_key='checkout:'+id,
                payload={'account':p.account_key,**arguments},dispatch=dispatch,verify=verify)
        db.expire_all();draft=draft_owned(db,uid,thread,p.account_key,id)
        draft.operation_id=outcome.operation_id
        draft.status='submitted' if outcome.verified else 'failed' if outcome.state=='failed' else 'unknown'
        observed=order_lifecycle.present(outcome.data,utcnow())
        draft.quote={**draft.quote,'order':observed}
        db.commit()
        if outcome.state=='failed' and rejected:raise rejected[0]
        return {'status':'success' if outcome.verified else draft.status,'checkout':public_draft(draft),
                'order':observed,'message':'订单已创建并经查询核实；尚未自动付款。' if outcome.verified else '订单提交结果未能核实，禁止自动重提，请查询订单状态。'}
    except Exception:
        # If a write-ahead claim exists, the remote result may be unknown; without
        # a claim preflight failed and it is safe to prepare a new quote.
        db.rollback();draft=draft_owned(db,uid,thread,p.account_key,id)
        op=db.scalar(select(ExternalOperation).where(ExternalOperation.user_id==uid,ExternalOperation.provider=='mcd:'+p.account_key[:16],ExternalOperation.request_key=='checkout:'+id))
        draft.status=('failed' if op.status=='failed' else 'unknown') if op else 'stale'
        if op:draft.operation_id=op.id
        db.commit();raise


def query_checkout(db,uid,thread,p,id):
    draft=draft_owned(db,uid,thread,p.account_key,id)
    op=db.get(ExternalOperation,draft.operation_id) if draft.operation_id else db.scalar(select(ExternalOperation).where(
        ExternalOperation.user_id==uid,ExternalOperation.provider=='mcd:'+p.account_key[:16],ExternalOperation.request_key=='checkout:'+id))
    if op and op.user_id==uid and op.remote_id:
        db.commit()
        with p.connect() as client:
            def verify(remote_id):
                data=client.call_read('query-order',{'orderId':remote_id})
                okay=verify_remote(data,remote_id,draft.cart,draft.quote,draft.take_way_code)
                return okay,order_observation(data,remote_id,draft.quote,okay,op.result)
            try:
                verified,observed=verify(op.remote_id)
            except Exception:
                verified,observed=False,{**order_observation({},op.remote_id,draft.quote,False,op.result),'query_error':'查询失败，未重新提交'}
        db.expire_all();draft=draft_owned(db,uid,thread,p.account_key,id)
        saved=db.get(ExternalOperation,op.id)
        saved.status='succeeded' if verified else 'unknown'
        saved.result=observed
        draft.operation_id=op.id
        draft.status='submitted' if verified else 'unknown'
        observed=order_lifecycle.present(observed,utcnow(),created_at=saved.created_at)
        draft.quote={**draft.quote,'order':observed}
        db.commit()
        return {'status':'success' if verified else 'unknown','checkout':public_draft(draft),'order':observed,
                'message':('已核实订单状态。' if verified else '已读取官方订单状态；餐品等完整信息仍待核实。') if observed['lifecycle']!='unknown' else '订单仍待核实，未重新提交。'}
    if draft.status in {'unknown', 'submitting', 'verifying'}:
        message = '没有可验证的远程订单编号；未重新下单，请在麦当劳账号核对。'
    elif draft.status in {'awaiting_confirmation', 'needs_selection'}:
        message = '只有报价预览，尚未提交订单。'
        if as_utc(draft.expires_at) <= utcnow():
            message += '报价已过期，需要重新报价。'
    elif draft.status == 'cancelled':
        message = '订单草案已取消，未提交订单。'
    else:
        message = preview_text(draft)
    return {'status': 'unknown' if draft.status in {'unknown', 'submitting', 'verifying'} else draft.status,
            'checkout': public_draft(draft), 'message': message}


def cancel_existing_order(db,uid,thread,p,id,version,existing_id,fingerprint,*,confirmed):
    if not confirmed:raise CheckoutError('取消旧订单需要单独确认。')
    if not p.allow_orders:raise CheckoutError('当前配置关闭订单写操作。')
    draft=draft_owned(db,uid,thread,p.account_key,id)
    saved=draft.quote.get('order_conflict') or {}
    if draft.version!=version or saved.get('fingerprint')!=fingerprint or existing_id not in {x['checkout_id'] for x in saved.get('orders',[])}:
        raise CheckoutError('旧订单选择或新预览已经变化，请重新核对。')
    old=account_draft(db,uid,p,existing_id)
    query_checkout(db,uid,old.thread,p,old.id)
    old=account_draft(db,uid,p,existing_id)
    observed=order_lifecycle.present(old.quote.get('order'),utcnow())
    if observed['lifecycle'] in {'cancelled','expired'}:
        return refresh_conflict(db,uid,thread,p,id,version)
    if observed['lifecycle']!='unpaid' or not observed.get('order_id'):
        raise CheckoutError('旧单已支付或状态未知，未执行取消；请重新查询。')
    remote_id=observed['order_id'];factory=factory_for(db);db.commit()
    with p.connect() as client:
        if 'cancel-order' not in client.discover():raise CheckoutError('当前官方接口未提供取消能力，请前往官方订单页处理。')
        def dispatch():
            value=client.cancel_pickup({'orderId':remote_id,'cancelReasonCode':'2'})
            if not isinstance(value,dict) or value.get('cancelResult') is not True:
                raise external_operations.DefiniteRejection()
            return external_operations.DispatchReceipt(remote_id,{})
        def verify(order_id):
            value=client.call_read('query-order',{'orderId':order_id})
            receipt=order_observation(value,order_id,old.quote,False,observed)
            return receipt['status_verified'] and receipt['status_code'] in {'7','已取消'},receipt
        outcome=external_operations.execute(factory,user_id=uid,provider='mcd:'+p.account_key[:16],
            capability_id='commerce.cancel_order',request_key='cancel:'+old.id,payload={'orderId':remote_id},dispatch=dispatch,verify=verify)
    # A timeout never retries cancellation. The following call is query-only.
    result=refresh_conflict(db,uid,thread,p,id,version)
    result['message']=('旧订单已取消；新订单尚未创建，请确认新预览。' if outcome.verified else
                       '取消结果未核实，未重复取消或自动创建新单。请查询或前往官方订单页处理。')
    return result
