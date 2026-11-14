"""Explicit read-only integration probe. Cannot create or cancel orders."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.food.mcd_transport import ReadOnlyMcdClient
from app.food.commerce_adapter import CommerceReadAdapter, StoreSearch, PickupMenu


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--city');p.add_argument('--keyword');args=p.parse_args()
    s=Settings();report={'mode':'real_mcp_readonly','configured':bool(s.mcd_mcp_token),'steps':[],'orders_created':0}
    client=None
    try:
        client=ReadOnlyMcdClient(url=s.mcd_mcp_url,token=s.mcd_mcp_token,timeout=s.mcd_mcp_timeout)
        tools=client.discover();report['tool_names']=sorted(tools)
        report['schema_sha256']=hashlib.sha256(json.dumps(tools,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        report['steps'].append({'step':'discover','status':'ok','count':len(tools)})
        adapter=CommerceReadAdapter(client);nutrition=adapter.nutrition()
        report['steps'].append({'step':'nutrition','status':'ok','data_type':type(nutrition).__name__})
        if args.city and args.keyword:
            stores=adapter.stores(StoreSearch(city=args.city,keyword=args.keyword,be_type=1))
            if not isinstance(stores,list):raise ValueError('unexpected_store_payload')
            store=next((s for s in stores if s.get('storeCode') and s.get('businessStatus')),None)
            report['steps'].append({'step':'stores','status':'ok','count':len(stores)})
            if store:
                menu=adapter.menu(PickupMenu(store_code=str(store['storeCode'])))
                report['steps'].append({'step':'menu','status':'ok','store_code':store['storeCode'],'product_count':len(menu.get('meals',{}))})
            else:report['steps'].append({'step':'menu','status':'not_run','reason':'no_open_store'})
        report['status']='ok'
    except Exception as exc:report.update(status='failed',error_type=type(exc).__name__)
    finally:
        if client:client.close()
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report['status']=='ok' else 1


if __name__=='__main__':raise SystemExit(main())
