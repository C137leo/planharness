import sys,json,tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root/'backend'))
from app.evals.dialogue_runner import DialogueEnvironment,DAY
from app.api.coach_routes import coach_commerce_dependency
from app.api.routes import router
from app.api.auth_routes import router as auth_router
from app.domain.commerce import CommerceProvider
from tests.test_commerce_selection import GenericCoffeeMcp,detail_plan,quote_plan,HOT
from tests.test_semantic_binding import Scripted
from starlette.staticfiles import StaticFiles
import uvicorn
class UiCoffee(GenericCoffeeMcp):
 def call_read(self,name,args):
  data=super().call_read(name,args)
  if name=='query-meals':data['meals']['coffee']['name']=HOT
  if name=='query-meal-detail':data['name']=HOT
  return data
fake=UiCoffee()
model=Scripted([])
with tempfile.TemporaryDirectory(prefix='commerce-selection-ui-') as tmp:
 env=DialogueEnvironment(Path(tmp)/'db.sqlite','selection-ui',model)
 env.app.dependency_overrides[coach_commerce_dependency]=lambda:CommerceProvider('test-account',lambda:fake,True)
 env.app.include_router(auth_router);env.app.include_router(router)
 def post(text,**fields):
  return env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':text,'date':str(DAY),'thread':env.thread,**fields}).json()['data']
 post('连接',commerce_action={'action':'connect'})
 r=post('查门店',commerce_action={'action':'search_stores','city':'北京','keyword':'三里屯'})
 store=r['commerce_catalog']['items'][0]['id']
 post('看菜单',commerce_action={'action':'menu','target_id':store})
 def pending(d):
  p=detail_plan(d,pending=True);p['question']='已选定热美式中杯，确认读取详情并继续报价吗？';return p
 model.responses.extend([pending,quote_plan])
 r=post('麦咖啡™热美式中杯，读详情再给报价，先不要提交')
 assert r['overall_status']=='confirmation_required',r
 Path('/tmp/commerce-selection-auth.json').write_text(json.dumps(env.headers))
 @env.app.get('/api/v1/system/status')
 def status():return {'data':{'paused':False}}
 @env.app.get('/__mock_evidence')
 def evidence():return {'provider':'mock','model':'scripted','submits':fake.submits,'model_calls':len(model.calls)}
 env.app.mount('/',StaticFiles(directory='/tmp/planharness-commerce-selection-h5',html=True),name='ui')
 try:uvicorn.run(env.app,host='127.0.0.1',port=5192)
 finally:env.close()
