"""Official stores only, no database and no order-writing client."""
import sys,json,datetime as dt
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.food.mcd_transport import ReadOnlyMcdClient
s=Settings();report={'mode':'real_official_stores_readonly','checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),'orders_created':0}
try:
 if not s.mcd_mcp_token:raise ValueError('token_not_configured')
 client=ReadOnlyMcdClient(url=s.mcd_mcp_url,token=s.mcd_mcp_token,timeout=20)
 try:
  rows=client.call_read('query-nearby-stores',{'city':'北京','keyword':'三里屯','beType':1,'searchType':2})
  report['stores']=[{'name':r.get('storeName'),'open':r.get('businessStatus'),'distance':r.get('distance'),'hours':str(r.get('businessStartTime'))+'–'+str(r.get('businessEndTime'))} for r in rows]
  report['status']='success'
 finally:client.close()
except Exception as exc:report.update(status='not_verified',error_type=type(exc).__name__)
out=Path('../docs/evidence/commerce-availability/official-stores-readonly.json');out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False))
