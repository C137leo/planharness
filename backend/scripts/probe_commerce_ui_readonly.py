"""Real MCP + authenticated manual UI contract, isolated DB, no order-writing client."""
import sys,json,hashlib,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.evals.dialogue_runner import DialogueEnvironment,DAY
from app.api.coach_routes import coach_commerce_dependency
from app.domain.commerce import CommerceProvider
from app.food.mcd_transport import ReadOnlyMcdClient
class NoModel:
    def available(self):return False
s=Settings();calls=[]
class LoggedReadOnly(ReadOnlyMcdClient):
    def call_read(self,name,args):
        calls.append({'name':name,'arguments':args})
        return super().call_read(name,args)
p=CommerceProvider(hashlib.sha256((s.mcd_mcp_url+'\0'+s.mcd_mcp_token).encode()).hexdigest(),lambda:LoggedReadOnly(url=s.mcd_mcp_url,token=s.mcd_mcp_token,timeout=25),False)
report={'mode':'real_mcp_readonly_manual_ui','real_order_writes':0,'steps':[],'complete':False}
out=Path('../docs/evidence/commerce-ui/manual-real-readonly.json')
with tempfile.TemporaryDirectory(prefix='mcd-ui-readonly-') as tmp:
 env=DialogueEnvironment(Path(tmp)/'db.sqlite','mcd-ui-readonly',NoModel())
 env.app.dependency_overrides[coach_commerce_dependency]=lambda:p
 def post(text,**fields):
  r=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':text,'thread':env.thread,'date':str(DAY),**fields})
  assert r.status_code==200,r.text
  data=r.json()['data'];report['steps'].append({'text':text,'request':fields,'result':data});out.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str));return data
 try:
  r=post('查看营养',commerce_action={'action':'nutrition'});assert len(r['commerce_nutrition']['items'])>=100
  r=post('查找门店',commerce_action={'action':'search_stores','city':'上海','keyword':'人民广场'})
  store=next(x for x in r['commerce_catalog']['items'] if x['open'])
  r=post('选这家',commerce_action={'action':'menu','target_id':store['id']})
  product=next(x for x in r['commerce_catalog']['items'] if x['name']=='麦香鸡')
  r=post('核对预览',commerce_action={'action':'prepare_order','target_id':store['id'],'items':[{'product_id':product['id'],'quantity':1,'use_default_options':True}]})
  quote=r['checkout'];way=next(x for x in quote['quote']['take_ways'] if x['title']=='外带')
  r=post('外带',checkout_action={'action':'choose_take_way','checkout_id':quote['id'],'version':quote['version'],'take_way_code':way['code']})
  assert r['checkout']['status']=='awaiting_confirmation' and r['overall_status']=='proposed'
  report.update(complete=True,calls=calls,model_calls=len(env.client.calls))
  out.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
  print({'passed':len(report['steps']),'nutrition_items':len(report['steps'][0]['result']['commerce_nutrition']['items']),'real_order_writes':0,'model_calls':len(env.client.calls)})
 finally:env.close()
