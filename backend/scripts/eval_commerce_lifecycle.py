"""Real model on synthetic SQLite with fake MCP. Never calls a live order write."""
import sys,json,copy,tempfile,argparse,datetime as dt
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from app.config import Settings
from app.llm.tool_client import tool_client_from_settings
from app.evals.dialogue_runner import DialogueEnvironment,DAY,digest,diff
from app.api.coach_routes import coach_commerce_dependency
from app.domain import commerce as c
from app.models import CheckoutDraft,ExternalOperation,CommerceObject,utcnow
from app.seed import DEMO_USER_ID
from scripts.eval_day_revision_chat import hashes
from tests.test_commerce_checkout import FakeMcp

class LifecycleMcp(FakeMcp):
    def __init__(self):super().__init__();self.cancellations=[]
    def discover(self):return {**super().discover(),'cancel-order':{}}
    def submit_pickup(self,args):
        value=super().submit_pickup(args);row=self.orders[value['orderId']]
        row.update(orderStatus='1',payH5Url='https://www.mcd.cn/pay?orderId='+value['orderId'],expirePayTime=(utcnow()+dt.timedelta(minutes=15)).isoformat())
        return {**value,'orderStatus':'1','payH5Url':row['payH5Url'],'expirePayTime':row['expirePayTime']}
    def cancel_pickup(self,args):
        self.cancellations.append(copy.deepcopy(args));self.orders[args['orderId']]['orderStatus']='7';return {'cancelResult':True}


def initialize(env,fake,p,fixture):
    with env.factory() as db:
        c.save_pickup_preferences(db,DEMO_USER_ID,'北京','三里屯');db.commit()
        c.load_context(db,DEMO_USER_ID,env.thread,p)
        store=c.object_row(db,DEMO_USER_ID,env.thread,p.account_key,'commerce_store','store1',payload={
            'provider_code':'store1','name':'真实门店','address':'测试地址','open':True,'search':{'city':'北京','keyword':'三里屯','beType':1,'searchType':2}})
        c.save_menu(db,DEMO_USER_ID,env.thread,p,store,fake.call_read('query-meals',{}));db.commit()
        product=db.scalar(select(CommerceObject).where(CommerceObject.kind=='commerce_product',CommerceObject.external_key=='wings'))
        old=c.quote_preview(db,DEMO_USER_ID,env.thread,p,store.id,[{'product_id':product.id,'quantity':1}],None,key='fixture-old');db.commit()
        result=c.submit(db,DEMO_USER_ID,env.thread,p,old.id,old.version,confirmed=True)
        assert result['status']=='success',result
        if fixture=='cancelled_incomplete':
            fake.orders['order-1']={'orderId':'order-1','orderStatus':'已取消'}
            old=db.get(CheckoutDraft,old.id);old.status='unknown';old.quote={**old.quote,'order':None}
            op=db.get(ExternalOperation,old.operation_id);op.result={};op.status='unknown';db.commit()
        elif fixture=='expired':
            fake.orders['order-1']['expirePayTime']=(utcnow()-dt.timedelta(minutes=2)).isoformat()
        elif fixture=='expired_fallback':
            stamp=utcnow()-dt.timedelta(minutes=16)
            fake.orders['order-1'].pop('expirePayTime',None)
            old=db.get(CheckoutDraft,old.id);old.quote={**old.quote,'order':None,'order_created_at':stamp.isoformat()}
            op=db.get(ExternalOperation,old.operation_id);op.result={**op.result,'created_at':stamp.isoformat(),'expire_at':''};op.created_at=stamp;db.commit()
        elif fixture=='paid':fake.orders['order-1']['orderStatus']='2'
        return old.id


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--suite',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    suite=json.loads(args.suite.read_text());model=tool_client_from_settings(Settings())
    if not model.available():raise SystemExit('Real model required')
    report={'suite':suite['id'],'suite_hash':digest(suite),'split':suite['split'],'model':model.model,'provider':'fake MCP','real_user_data':False,
      'hashes_before':hashes(),'conversations':[],'complete':False,'real_order_writes':0}
    def save():args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    for case in suite['conversations']:
        fake=LifecycleMcp();p=c.CommerceProvider('synthetic-lifecycle',lambda:fake,True);record={'id':case['id'],'turns':[]};report['conversations'].append(record)
        with tempfile.TemporaryDirectory(prefix='lifecycle-live-') as tmp:
            env=DialogueEnvironment(Path(tmp)/'synthetic.sqlite',case['id'][:30],model)
            env.app.dependency_overrides[coach_commerce_dependency]=lambda:p
            old_id=initialize(env,fake,p,case['fixture']);quote_id=None
            try:
                for n,turn in enumerate(case['turns'],1):
                    before=env.snapshot();history=env.history();start=len(env.client.calls);mcp_start=len(fake.calls);cancel_start=len(fake.cancellations)
                    response=env.http.post('/api/v1/agent/chat',headers=env.headers,json={'text':turn['text'],'date':str(DAY),'thread':env.thread})
                    result=response.json().get('data') or {'overall_status':'http_error','reply':response.text};after=env.snapshot();errors=[];expect=turn['expect'];checkout=result.get('checkout') or {}
                    if result.get('overall_status') not in expect['statuses']:errors.append('status:'+str(result.get('overall_status')))
                    if fake.submits!=expect['submits']:errors.append('submission_count')
                    if len(fake.cancellations)!=expect.get('cancels',0):errors.append('cancellation_count')
                    if expect.get('new_preview'):
                        if checkout.get('id')==old_id or not checkout.get('cart') or checkout.get('status')!='awaiting_confirmation':errors.append('new_preview')
                        quote_id=checkout.get('id') or quote_id
                    if expect.get('same_new') and checkout.get('id')!=quote_id:errors.append('same_new_checkout')
                    if expect.get('conflict') and not result.get('order_conflict'):errors.append('missing_old_order_choice')
                    if expect.get('no_conflict') and result.get('order_conflict'):errors.append('unnecessary_old_order_choice')
                    if expect.get('pending_cap') and not any(t['capability_id']==expect['pending_cap'] for t in (result.get('pending_confirmation') or {}).get('tasks',[])):errors.append('pending_capability')
                    protected=diff(before['days'],after['days'])
                    for table in ['profiles','meal_logs','workout_logs','workout_sessions','plans','plan_versions','memories']:
                        protected+=diff(before['tables'].get(table),after['tables'].get(table))
                    if protected:errors.append('unrelated_fitness_change')
                    record['turns'].append({'turn':n,'user_text':turn['text'],'actual_goals':result.get('task_plan'),'context_and_model_calls':env.client.calls[start:],
                        'tools_and_parameters':result.get('task_results'),'mcp_calls':fake.calls[mcp_start:],'cancellation_calls':fake.cancellations[cancel_start:],
                        'before':before,'after':after,'actual_changes':diff(before,after),'unauthorized_changes':protected,'reply':result.get('reply'),'result':result,
                        'history_before':history,'history_after':env.history(),'oracle':expect,'passed':not errors,'errors':errors});save()
                    print(json.dumps({'conversation':case['id'],'turn':n,'passed':not errors,'errors':errors,'reply':result.get('reply')},ensure_ascii=False),flush=True)
            finally:env.close()
    turns=[t for v in report['conversations'] for t in v['turns']]
    report.update(complete=True,total=len(turns),passed=sum(t['passed'] for t in turns),pass_rate=sum(t['passed'] for t in turns)/len(turns),unauthorized_changes=sum(len(t['unauthorized_changes']) for t in turns),hashes_after=hashes());save();model._client.close()

if __name__=='__main__':main()
