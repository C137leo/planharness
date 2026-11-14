"""Authenticated order-history projection across chat threads and legacy audit records.

Lists are local reads. Refresh calls query-order only; no create/cancel or chat writes.
"""
import base64
import datetime as dt
import json
from decimal import Decimal,InvalidOperation
from sqlalchemy import select,or_
from app.models import CheckoutDraft,ExternalOperation,AuditEvent,CommerceObject,utcnow
from app.domain import commerce as c,order_lifecycle as life
from app.domain.timeutil import as_utc

GROUPS=('active','paid','inactive','unknown')


def _money(value):
    try:
        number=Decimal(str(value))*100
        return int(number) if number.is_finite() and number>=0 and number==number.to_integral_value() else None
    except (ValueError,InvalidOperation,OverflowError):return None


def _safe_order(receipt,created,now):
    value=life.present(receipt,now,created_at=created)
    return {k:v for k,v in {**value,'pay_url':c.official_pay_url(value.get('pay_url'),value.get('order_id'))}.items()
        if k in {'order_id','status','status_code','verified','status_verified','created_at','observed_at','expire_at','payment_deadline','payment_expired','lifecycle','pay_url'}}


def _group(order,failed=False):
    if failed:return 'inactive','提交失败'
    state=order['lifecycle']
    return {'unpaid':('active','待支付'),'paid':('paid',order.get('status') or '已付款'),
            'cancelled':('inactive','已取消'),'expired':('inactive','已过期')}.get(state,('unknown','待核实'))


def _cart(items):
    return [{'name':str(x.get('name') or '餐品'),'quantity':x.get('quantity'),
             'specifications':[{'name':s.get('name',''),'quantity':s.get('quantity')} for s in x.get('specifications',[]) if isinstance(s,dict)]}
            for x in items if isinstance(x,dict)]


def _checkout(db,row,op,p,now):
    if not op and row.status not in {'unknown','submitting','verifying','submitted'}:return None
    # Keep verification and lifecycle independent. A legacy incomplete receipt is
    # UNKNOWN until a matched query proves status; never invent a paid order.
    candidates=[v for v in [row.quote.get('order'),op.result if op else None] if v and ('status_code' in v or 'order_id' in v)]
    receipt=max(candidates,key=lambda x:life.provider_time(x.get('observed_at')) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) if candidates else {}
    created=life.provider_time(receipt.get('created_at')) or as_utc(op.created_at if op else row.created_at)
    order=_safe_order(receipt,created,now)
    if not order.get('order_id') and op and op.remote_id:order['order_id']=op.remote_id
    group,label=_group(order,failed=bool(op and op.status=='failed'))
    return {'id':'checkout:'+row.id,'source':'chat','group':group,'status_label':label,'order':order,
            'created_at':created.isoformat(),'store':{k:row.quote.get('store',{}).get(k,'') for k in ['name','address','hours']},
            'items':_cart(row.cart),'total_cents':row.quote.get('total_cents'),'amount_label':'官方核实金额' if order.get('verified') else '预览金额',
            'take_way':next((w['title'] for w in row.quote.get('take_ways',[]) if w['code']==row.take_way_code),''),
            'can_refresh':bool(p and p.account_key==row.account_key and op and op.remote_id),
            'needs_connection':not bool(p and p.account_key==row.account_key),'details_complete':bool(row.cart),
            'sync_note':'按最近已保存的官方状态分类，付款后可同步更新。'}


def _legacy(db,row,p,legacy_key,now,cancelled=False):
    payload=row.payload;remote=str(payload.get('order_id') or '')
    if not remote:return None
    cache=db.scalar(select(CommerceObject).where(CommerceObject.user_id==row.actor_id,CommerceObject.kind=='commerce_order_record',CommerceObject.parent_key==row.id))
    data=cache.payload if cache else {}
    raw_status=str(payload.get('status') or '')
    receipt=data.get('order') or {'order_id':remote,'status':raw_status,'status_code':raw_status,
        'status_verified':raw_status in life.STATUS,'verified':False,'created_at':as_utc(row.created_at).isoformat()}
    if cancelled:receipt={**receipt,'status_code':'7','status':'已取消','status_verified':True}
    order=_safe_order(receipt,row.created_at,now);group,label=_group(order)
    connected=bool(p and legacy_key and payload.get('account_key')==legacy_key)
    return {'id':'legacy:'+row.id,'source':'legacy','group':group,'status_label':label,'order':order,
        'created_at':as_utc(row.created_at).isoformat(),'store':{'name':payload.get('store_name','麦当劳'),'address':data.get('address',''),'hours':''},
        'items':_cart(data.get('items',[])),'total_cents':data.get('total_cents',_money(payload.get('total'))),'amount_label':'订单记录金额',
        'take_way':data.get('take_way',''),'can_refresh':connected,'needs_connection':not connected,'details_complete':bool(data.get('items')),
        'sync_note':'旧版记录未保存完整明细，可同步官方状态；不会修改原审计记录。'}


def records(db,uid,p=None,legacy_key=''):
    now=utcnow();result=[];known=set()
    pairs=db.execute(select(CheckoutDraft,ExternalOperation).outerjoin(ExternalOperation,ExternalOperation.id==CheckoutDraft.operation_id).where(
        CheckoutDraft.user_id==uid,or_(CheckoutDraft.operation_id.is_not(None),CheckoutDraft.status.in_(['unknown','submitting','verifying','submitted','failed'])))).all()
    for row,op in pairs:
        if op and (op.user_id!=uid or op.capability_id!='commerce.submit_order'):op=None
        if op is None:op=db.scalar(select(ExternalOperation).where(ExternalOperation.user_id==uid,ExternalOperation.provider=='mcd:'+row.account_key[:16],ExternalOperation.request_key=='checkout:'+row.id))
        value=_checkout(db,row,op,p,now)
        if value:
            result.append(value)
            if p and row.account_key==p.account_key and value['order'].get('order_id'):known.add(value['order']['order_id'])
    audits=list(db.scalars(select(AuditEvent).where(AuditEvent.actor_id==uid,AuditEvent.action.in_(['mcd_order_created','mcd_order_cancelled']))))
    cancels={str(x.payload.get('order_id')) for x in audits if x.action=='mcd_order_cancelled'}
    for row in audits:
        if row.action!='mcd_order_created':continue
        if legacy_key and row.payload.get('account_key')==legacy_key and str(row.payload.get('order_id')) in known:continue
        value=_legacy(db,row,p,legacy_key,now,str(row.payload.get('order_id')) in cancels)
        if value:result.append(value)
    result.sort(key=lambda x:(x['created_at'],x['id']),reverse=True)
    return result


def list_orders(db,uid,group='active',cursor='',limit=20,*,p=None,legacy_key=''):
    values=records(db,uid,p,legacy_key);counts={name:sum(x['group']==name for x in values) for name in GROUPS}
    selected=[x for x in values if x['group']==group]
    if cursor:
        try:
            raw=json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if set(raw)!={'group','created_at','id'} or raw['group']!=group or not all(isinstance(v,str) for v in raw.values()):raise ValueError()
            selected=[x for x in selected if (x['created_at'],x['id'])<(raw['created_at'],raw['id'])]
        except Exception:raise ValueError('分页位置无效，请重新打开订单列表。') from None
    page=selected[:limit];next_cursor=''
    if len(selected)>limit:
        last=page[-1];next_cursor=base64.urlsafe_b64encode(json.dumps({'group':group,'created_at':last['created_at'],'id':last['id']}).encode()).decode()
    return {'items':page,'counts':counts,'next_cursor':next_cursor,'as_of':utcnow().isoformat(),'scope':'本应用保存的订单与提交记录'}


def _owned(db,uid,id):
    kind,_,key=id.partition(':')
    if kind=='checkout':
        row=db.get(CheckoutDraft,key)
        if row and row.user_id==uid:return kind,row
    elif kind=='legacy':
        row=db.get(AuditEvent,key)
        if row and row.actor_id==uid and row.action=='mcd_order_created':return kind,row
    raise LookupError('订单记录不存在或不属于当前用户。')


def detail(db,uid,id,*,p=None,legacy_key=''):
    _owned(db,uid,id)
    return next((v for v in records(db,uid,p,legacy_key) if v['id']==id),None)


def refresh(db,uid,id,p,*,legacy_key=''):
    if p is None:raise ValueError('请先在麦当劳点餐的Token设置中连接下单账号。')
    kind,row=_owned(db,uid,id)
    if kind=='checkout':
        if row.account_key!=p.account_key:raise ValueError('当前连接与下单时的账号凭证不同，请连接原下单账号后同步。')
        op=db.get(ExternalOperation,row.operation_id) if row.operation_id else db.scalar(select(ExternalOperation).where(ExternalOperation.user_id==uid,ExternalOperation.provider=='mcd:'+row.account_key[:16],ExternalOperation.request_key=='checkout:'+row.id))
        if not op or not op.remote_id:raise ValueError('这笔提交未返回可查询的订单号，请到麦当劳账号核对；没有重新下单。')
        value=c.query_checkout(db,uid,row.thread,p,row.id)
        if value.get('status')=='unknown' and (value.get('order') or {}).get('query_error'):
            raise ValueError('暂时无法同步官方订单状态，已保留原记录，请稍后重试。')
    else:
        if not legacy_key or row.payload.get('account_key')!=legacy_key:raise ValueError('请连接旧订单原下单账号后同步。')
        remote=str(row.payload.get('order_id') or '')
        db.commit()
        with p.connect() as client:value=client.call_read('query-order',{'orderId':remote})
        data=value.get('orderDetail') if isinstance(value.get('orderDetail'),dict) else value
        if str(value.get('orderId') or data.get('orderId') or '')!=remote:raise ValueError('官方返回的订单编号不匹配，未更新记录。')
        amount=_money(data.get('totalAmount'));quote={'total_cents':amount if amount is not None else _money(row.payload.get('total'))}
        receipt=c.order_observation(value,remote,quote,False,{'created_at':as_utc(row.created_at).isoformat()})
        items=[{'name':x.get('productName') or x.get('name') or '餐品','quantity':x.get('quantity'),'specifications':[]} for x in data.get('orderProductList',[]) if isinstance(x,dict)]
        c.object_row(db,uid,'__order_history__',legacy_key,'commerce_order_record',remote,parent=row.id,payload={
            'order':receipt,'items':items,'total_cents':quote['total_cents'],'address':data.get('storeAddress',''),'take_way':data.get('takeWay','')})
        db.commit()
    return detail(db,uid,id,p=p,legacy_key=legacy_key)


def sync_recent(db,uid,p,*,legacy_key=''):
    if p is None:raise ValueError('请先连接麦当劳Token后同步官方状态；已保存的记录仍可查看。')
    candidates=[x for x in records(db,uid,p,legacy_key) if x['can_refresh'] and (x['group'] in {'active','unknown'} or x['order']['lifecycle']=='expired')]
    checked=0;failed=0
    for value in candidates[:10]:
        try:refresh(db,uid,value['id'],p,legacy_key=legacy_key)
        except Exception:db.rollback();failed+=1
        checked+=1
    return {'checked':checked,'failed':failed,'remaining':max(0,len(candidates)-checked),'synced_at':utcnow().isoformat()}
