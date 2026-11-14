"""Real model, explicit Mock commerce with official nutrition fixture. No real orders."""
import json,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.serve_commerce_ui_smoke import UiMcp
from app.config import Settings
from app.llm.tool_client import tool_client_from_settings
from app.domain.commerce import CommerceProvider
from app.evals.dialogue_runner import DialogueEnvironment,DAY,diff,digest
from app.api.coach_routes import coach_commerce_dependency
from app.agent.business_snapshot import business_state
from app.models import User
from app.seed import DEMO_USER_ID
client=tool_client_from_settings(Settings());fake=UiMcp();p=CommerceProvider('ui-choices-mock',lambda:fake,False)
texts=['我在北京三里屯，先看看附近麦当劳哪些店还开着。','帮我挑距离最近而且营业的那家，把菜单和官方营养也看看，别下单。','我自己换第二家营业的门店，先只看它的菜单，别下单。','一份牛肉汉堡套餐就行，按原配搭配，外带，先给我核对报价，不要实际提交。']
report={'model':client.model,'provider':'mock','split':'new_verification','turns':[],'complete':False,'real_order_writes':0}
out=Path('../docs/evidence/commerce-ui/model-store-choices.json')
with tempfile.TemporaryDirectory(prefix='mcd-ui-model-') as tmp:
 env=DialogueEnvironment(Path(tmp)/'db.sqlite','ui-model-choice',client);env.app.dependency_overrides[coach_commerce_dependency]=lambda:p
 try:
  for i,text in enumerate(texts,1):
   before=env.snapshot();start=len(env.client.calls)
   with env.factory() as db:fitness_before=business_state(db,db.get(User,DEMO_USER_ID),[str(DAY)])
   response=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':text,'thread':env.thread,'date':str(DAY)})
   result=response.json()['data'];after=env.snapshot();errors=[]
   with env.factory() as db:fitness_after=business_state(db,db.get(User,DEMO_USER_ID),[str(DAY)])
   if fitness_before!=fitness_after:errors.append('unauthorized_fitness_changes')
   if fake.submits:errors.append('unexpected_submit')
   if result['overall_status'] not in ['success','proposed']:errors.append('status:'+result['overall_status'])
   catalog=result.get('commerce_catalog') or {}
   if i==1 and catalog.get('kind')!='stores':errors.append('missing_stores')
   if i in [2,3]:
    if catalog.get('kind')!='products':errors.append('missing_menu')
    expected='测试 · 广场餐厅' if i==2 else '测试 · 福州路餐厅'
    if (catalog.get('store') or {}).get('name')!=expected:errors.append('store_binding')
   if i==4:
    checkout=result.get('checkout') or {};quote=checkout.get('quote') or {}
    if (quote.get('store') or {}).get('name')!='测试 · 福州路餐厅':errors.append('lost_store_context')
    if checkout.get('status')!='awaiting_confirmation':errors.append('take_way_context')
   report['turns'].append({'turn':i,'user_text':text,'understood_goals':result.get('task_plan'),'model_calls_and_context':env.client.calls[start:],
       'result':result,'before':before,'after':after,'actual_changes':diff(before,after),'unauthorized_changes':diff(fitness_before,fitness_after),'errors':errors,'passed':not errors})
   out.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str));print({'turn':i,'passed':not errors,'errors':errors},flush=True)
  report.update(complete=True,passed=sum(t['passed'] for t in report['turns']),total=len(texts),source_hash=digest({str(p):p.read_text() for p in sorted(Path('app').rglob('*.py'))}))
  out.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
 finally:env.close();client._client.close()
