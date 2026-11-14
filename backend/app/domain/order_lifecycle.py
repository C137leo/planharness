"""Provider order lifecycle is independent of cart verification and write claims."""
import datetime as dt
from app.domain.timeutil import as_utc

SHANGHAI = dt.timezone(dt.timedelta(hours=8))
STATUS = {'1':'unpaid','待支付':'unpaid','待付款':'unpaid',
          '2':'paid','4':'paid','6':'paid','8':'paid','10':'paid',
          '已支付':'paid','已支付，配餐中':'paid','配送中':'paid','已完成':'paid','已评价':'paid','餐厅配餐中':'paid',
          '7':'cancelled','已取消':'cancelled','已过期':'expired','订单已过期':'expired'}


def provider_time(value):
    """Official local timestamps are China time; DB timestamps are UTC elsewhere."""
    try:
        stamp=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return as_utc(stamp.replace(tzinfo=SHANGHAI) if stamp.tzinfo is None else stamp)
    except (ValueError,TypeError):
        return None


def present(receipt,now,*,created_at=None):
    receipt=receipt or {}
    known=receipt.get('status_verified',False)
    state=STATUS.get(str(receipt.get('status_code') or receipt.get('status') or '')) if known else None
    created=provider_time(receipt.get('created_at')) or as_utc(created_at)
    deadline=provider_time(receipt.get('expire_at'))
    fallback=created+dt.timedelta(minutes=15) if created else None
    deadline=min(deadline,fallback) if deadline and fallback else deadline or fallback
    if state=='unpaid' and deadline and as_utc(now)>=deadline:state='expired'
    state=state or 'unknown'
    return {**receipt,'lifecycle':state,'payment_deadline':deadline.isoformat() if deadline else '',
            'active_unpaid':state=='unpaid','payment_expired':state=='expired',
            'payment_deadline_source':'official_or_15_minutes' if deadline else 'unknown'}
