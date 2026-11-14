"""Real-model continuous synthetic-state tests. MCP and all writes are fake."""
import sys,json,copy,tempfile,argparse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.llm.tool_client import tool_client_from_settings
from app.evals.dialogue_runner import DialogueEnvironment,DAY,digest,diff
from app.domain.commerce import CommerceProvider,save_pickup_preferences
from app.api.coach_routes import coach_commerce_dependency
from app.seed import DEMO_USER_ID
from app.food.mcd_transport import McpBusinessError
from scripts.eval_day_revision_chat import hashes
from tests.test_commerce_checkout import FakeMcp

class AvailabilityMcp(FakeMcp):
    def __init__(self,fixture):super().__init__();self.fixture=fixture
    def call_read(self,name,args):
        self.calls.append((name,copy.deepcopy(args)))
        if name=='query-nearby-stores':return [
            {'storeCode':'closed','storeName':'合成近站店','businessStatus':self.fixture in {'stock_rejected','open_nearest'},'distance':90,'address':'合成近址','businessStartTime':'08:00','businessEndTime':'22:00'},
            {'storeCode':'open','storeName':'合成河畔店','businessStatus':self.fixture!='all_closed','distance':240,'address':'合成河畔地址','businessStartTime':'00:00','businessEndTime':'23:59'},
            {'storeCode':'far','storeName':'合成远站店','businessStatus':self.fixture!='all_closed','distance':600,'address':'合成远址','businessStartTime':'00:00','businessEndTime':'23:59'}]
        if name=='query-meals':return {'meals':{code:{'name':label,'currentPrice':'10','productType':1} for code,label in [('wings','麦辣鸡翅2块'),('burger','牛肉汉堡'),('coffee','热美式中杯')]}}
        if name=='query-meal-detail':return {'code':args['code'],'name':{'wings':'麦辣鸡翅2块','burger':'牛肉汉堡','coffee':'热美式中杯'}[args['code']],'rounds':[]}
        if name=='calculate-price' and self.fixture=='stock_rejected' and any(x['productCode']=='wings' for x in args['items']):
            raise McpBusinessError(provider_code='STOCK_EMPTY',provider_message='麦辣鸡翅2块本时段已售罄，请选择其他餐品',tool=name)
        return super().call_read(name,args)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--suite',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    suite=json.loads(args.suite.read_text());model=tool_client_from_settings(Settings())
    if not model.available():raise SystemExit('Real model required')
    report={'suite':suite['id'],'split':suite['split'],'suite_hash':digest(suite),'model':model.model,'provider':'fake MCP','real_user_data':False,'real_order_writes':0,'hashes_before':hashes(),'conversations':[],'complete':False}
    def save():args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    for case in suite['conversations']:
        fake=AvailabilityMcp(case['fixture']);p=CommerceProvider('synthetic-availability',lambda:fake,True);conversation={'id':case['id'],'turns':[]};report['conversations'].append(conversation)
        with tempfile.TemporaryDirectory(prefix='availability-live-') as tmp:
            env=DialogueEnvironment(Path(tmp)/'db.sqlite',case['id'][:30],model);env.app.dependency_overrides[coach_commerce_dependency]=lambda:p
            with env.factory() as db:save_pickup_preferences(db,DEMO_USER_ID,'北京','地标');db.commit()
            quote_id=None
            try:
                for n,turn in enumerate(case['turns'],1):
                    before=env.snapshot();history=env.history();start=len(env.client.calls);mcp_start=len(fake.calls)
                    response=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':turn['text'],'date':str(DAY),'thread':env.thread})
                    result=response.json().get('data') or {'overall_status':'http_error','reply':response.text};after=env.snapshot();expect=turn['expect'];errors=[];checkout=result.get('checkout') or {}
                    if result.get('overall_status') not in expect['statuses']:errors.append('status:'+str(result.get('overall_status')))
                    if expect.get('store_choice') and not any(t['capability_id']=='commerce.select_store' for t in (result.get('pending_confirmation') or {}).get('tasks',[])):errors.append('missing_store_consent')
                    if expect.get('no_menu') and any(name in {'query-meals','calculate-price'} for name,_ in fake.calls[mcp_start:]):errors.append('premature_menu_or_quote')
                    if expect.get('food'):
                        if not checkout.get('cart') or checkout['cart'][0]['name']!=expect['food']:errors.append('selected_food')
                        if expect.get('quantity') and (not checkout.get('cart') or checkout['cart'][0]['quantity']!=expect['quantity']):errors.append('quantity')
                        if expect.get('store') and checkout.get('quote',{}).get('store',{}).get('name')!=expect['store']:errors.append('chosen_store')
                        if checkout.get('id') and quote_id is None:quote_id=checkout['id']
                    if expect.get('reason') and (result.get('commerce_availability') or {}).get('code')!=expect['reason']:errors.append('business_reason')
                    if expect.get('no_confirmation') and result.get('pending_confirmation',{}).get('status')=='pending':errors.append('unnecessary_confirmation')
                    if fake.submits:errors.append('unexpected_order_write')
                    if 'mcp_business_rejected' in result.get('reply',''):errors.append('raw_error_leak')
                    protected=diff(before['days'],after['days'])
                    for table in ['profiles','meal_logs','workout_logs','workout_sessions','plans','plan_versions','memories']:
                        protected+=diff(before['tables'].get(table),after['tables'].get(table))
                    if protected:errors.append('unrelated_changes')
                    conversation['turns'].append({'turn':n,'user_text':turn['text'],'actual_goals':result.get('task_plan'),'context_and_model_calls':env.client.calls[start:],
                        'tools_and_parameters':result.get('task_results'),'mcp_calls':fake.calls[mcp_start:],'before':before,'after':after,'actual_changes':diff(before,after),'unauthorized_changes':protected,
                        'reply':result.get('reply'),'result':result,'history_before':history,'history_after':env.history(),'oracle':expect,'passed':not errors,'errors':errors});save()
                    print(json.dumps({'conversation':case['id'],'turn':n,'passed':not errors,'errors':errors,'reply':result.get('reply')},ensure_ascii=False),flush=True)
            finally:env.close()
    turns=[t for c in report['conversations'] for t in c['turns']];report.update(complete=True,total=len(turns),passed=sum(t['passed'] for t in turns),pass_rate=sum(t['passed'] for t in turns)/len(turns),unauthorized_changes=sum(len(t['unauthorized_changes']) for t in turns),hashes_after=hashes());save();model._client.close()

if __name__=='__main__':main()
