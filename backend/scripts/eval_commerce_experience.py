"""Real model, synthetic state and fake MCP; never creates a real order."""
import sys,json,tempfile,argparse,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.llm.tool_client import tool_client_from_settings
from app.domain.commerce import CommerceProvider,save_pickup_preferences,pickup_preferences
from app.evals.dialogue_runner import DialogueEnvironment,DAY,diff,digest
from app.api.coach_routes import coach_commerce_dependency
from app.seed import DEMO_USER_ID
from tests.test_commerce_checkout import FakeMcp
from scripts.eval_day_revision_chat import hashes


class MenuMcp(FakeMcp):
    def call_read(self,name,args):
        if name=='query-nearby-stores':
            self.calls.append((name,copy.deepcopy(args)))
            return [{'storeCode':args['city']+'-'+key,'storeName':'合成·'+args['city']+label,'address':'合成地址 '+key,
                'businessStatus':opened,'distance':distance,'businessStartTime':'00:00','businessEndTime':'23:59'}
                for key,label,opened,distance in [('closed','近店休息',False,20),('near','近站餐厅',True,80),('far','远站餐厅',True,300)]]
        if name=='query-meals':
            self.calls.append((name,copy.deepcopy(args)))
            return {'meals':{key:{'name':label,'currentPrice':'10','productType':1} for key,label in [
                ('spicy','麦辣鸡翅2块'),('plain','原味鸡翅2块'),('burger','牛肉汉堡'),('coffee','热美式中杯'),
                *[('side'+str(n),'合成其他餐品'+str(n)) for n in range(8)]]}}
        if name=='query-meal-detail':
            self.calls.append((name,copy.deepcopy(args)))
            names={'spicy':'麦辣鸡翅2块','plain':'原味鸡翅2块','burger':'牛肉汉堡','coffee':'热美式中杯'}
            return {'code':args['code'],'name':names.get(args['code'],'合成其他餐品'+args['code'][4:]),'rounds':[]}
        value=super().call_read(name,args)
        if name=='calculate-price':value['takeWayList']=[{'code':'provider-dine-87','title':'堂食'},{'code':'provider-carry-91','title':'外带'}]
        return value


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--suite',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    suite=json.loads(args.suite.read_text());model=tool_client_from_settings(Settings())
    if not model.available():raise SystemExit('real model required')
    report={'suite':suite['id'],'split':suite['split'],'suite_hash':digest(suite),'hashes_before':hashes(),'conversations':[],
        'model':model.model,'provider':'fake MCP','real_user_data':False,'real_order_writes':0,'complete':False}
    def save():args.output.parent.mkdir(exist_ok=True,parents=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    for case in suite['conversations']:
        fake=MenuMcp();provider=CommerceProvider('synthetic-mcp',lambda:fake,True)
        conversation={'id':case['id'],'turns':[]};report['conversations'].append(conversation)
        with tempfile.TemporaryDirectory(prefix='commerce-experience-live-') as tmp:
            env=DialogueEnvironment(Path(tmp)/'db.sqlite',case['id'][:30],model);env.app.dependency_overrides[coach_commerce_dependency]=lambda:provider
            if case.get('saved_location'):
                with env.factory() as db:save_pickup_preferences(db,DEMO_USER_ID,**case['saved_location']);db.commit()
            first_id=None
            try:
                for number,turn in enumerate(case['turns'],1):
                    before=env.snapshot();history_before=env.history();start=len(env.client.calls);mcp_start=len(fake.calls)
                    response=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':turn['text'],'date':str(DAY),'thread':env.thread})
                    result=response.json().get('data') or {'overall_status':'http_error','reply':response.text};after=env.snapshot();expected=turn['expect'];errors=[]
                    checkout=result.get('checkout') or {};cart=checkout.get('cart',[])
                    if result.get('overall_status') not in expected['statuses']:errors.append('response:'+str(result.get('overall_status')))
                    if expected.get('quote'):
                        if checkout.get('status')!='awaiting_confirmation' or not cart:errors.append('missing_selected_quote')
                        if len(cart)!=1:errors.append('cart_count')
                        if 'quantity' in expected and (not cart or cart[0]['quantity']!=expected['quantity']):errors.append('quantity')
                        if expected.get('names') and (not cart or cart[0]['name'] not in expected['names']):errors.append('selected_food')
                        if checkout.get('take_way_code')!=expected.get('way','provider-carry-91'):errors.append('take_way')
                        if not any(a['type']=='confirm_tasks' for a in result.get('actions',[])):errors.append('missing_final_confirmation')
                        if first_id is None:first_id=checkout.get('id')
                        if expected.get('same_quote') and checkout.get('id')!=first_id:errors.append('same_quote')
                        if expected.get('city') and checkout.get('quote',{}).get('store',{}).get('search',{}).get('city')!=expected['city']:errors.append('city_binding')
                    if expected.get('no_search') and any(name=='query-nearby-stores' for name,_ in fake.calls[mcp_start:]):errors.append('guessed_city')
                    protected=diff(before['days'],after['days'])
                    for table in ['profiles','meal_logs','workout_logs','workout_sessions','plans','plan_versions','memories']:
                        protected+=diff(before['tables'].get(table),after['tables'].get(table))
                    if protected:errors.append('unauthorized_fitness_changes')
                    if fake.submits:errors.append('unexpected_submit')
                    with env.factory() as db:location=pickup_preferences(db,DEMO_USER_ID)
                    if location!=case.get('saved_location',{'city':'','keyword':''}):errors.append('changed_saved_location')
                    entry={'turn':number,'user_text':turn['text'],'actual_goals':result.get('task_plan'),'context_and_model_calls':env.client.calls[start:],
                        'tools_and_parameters':result.get('task_results'),'mcp_calls':fake.calls[mcp_start:],
                        'before':before,'after':after,'actual_changes':diff(before,after),'unauthorized_changes':protected,'reply':result.get('reply'),'result':result,
                        'history_before':history_before,'history_after':env.history(),'mock_submits':fake.submits,'oracle':expected,'passed':not errors,'errors':errors}
                    conversation['turns'].append(entry);save();print(json.dumps({'conversation':case['id'],'turn':number,'passed':not errors,'errors':errors,'reply':result.get('reply')},ensure_ascii=False),flush=True)
            finally:env.close()
    turns=[t for c in report['conversations'] for t in c['turns']]
    report.update(complete=True,total=len(turns),passed=sum(t['passed'] for t in turns),pass_rate=sum(t['passed'] for t in turns)/len(turns),
        unauthorized_changes=sum(len(t['unauthorized_changes']) for t in turns),hashes_after=hashes());save();model._client.close()


if __name__=='__main__':main()
