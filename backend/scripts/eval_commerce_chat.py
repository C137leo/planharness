"""Continuous real-model eval; real MCP mode only calls read methods. No real orders.
Oracles are evaluated after API calls, never provided to the model. Every turn
retains the same user/thread, task history and database.
"""
import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.llm.tool_client import tool_client_from_settings
from app.evals.dialogue_runner import DialogueEnvironment,DAY,digest,diff
from app.agent.business_snapshot import business_state
from app.api.coach_routes import coach_commerce_dependency
from app.domain.commerce import CommerceProvider
from app.food.mcd_transport import ReadOnlyMcdClient
from app.models import User
from app.seed import DEMO_USER_ID


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--suite',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--provider',choices=['real-readonly','mock','mock-coffee'],required=True);args=ap.parse_args()
    suite=json.loads(args.suite.read_text());settings=Settings();model=tool_client_from_settings(settings)
    if not model.available():raise SystemExit('live model required')
    if args.provider=='real-readonly' and not settings.mcd_mcp_token:raise SystemExit('real MCP credentials required')
    report={'id':suite['id'],'split':suite['split'],'provider':args.provider,'model':model.model,
        'suite_hash':digest(suite),'source_hash':digest({str(p):p.read_text() for p in sorted(Path('app').rglob('*.py'))}),
        'real_order_writes':0,'conversations':[],'complete':False,
        'evaluator_hash':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'provider_fixture_hash':hashlib.sha256(Path('tests/test_commerce_selection.py' if args.provider=='mock-coffee' else 'tests/test_commerce_checkout.py').read_bytes()).hexdigest() if args.provider.startswith('mock') else None}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def save():args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    for conversation in suite['conversations']:
        mcp_calls=[]
        if args.provider.startswith('mock'):
            from tests.test_commerce_checkout import FakeMcp
            if args.provider=='mock-coffee':
                from tests.test_commerce_selection import GenericCoffeeMcp
                FakeMcp=GenericCoffeeMcp
            fake=FakeMcp();fake.calls=mcp_calls;provider=CommerceProvider('explicit-mock',lambda:fake,True)
        else:
            class RecordedReadOnly(ReadOnlyMcdClient):
                def call_read(self,name,arguments):
                    entry={'tool':name,'arguments':arguments};mcp_calls.append(entry)
                    try:
                        result=super().call_read(name,arguments);entry['success']=True;return result
                    except Exception as exc:entry['error_type']=type(exc).__name__;raise
            provider=CommerceProvider(hashlib.sha256((settings.mcd_mcp_url+'\0'+settings.mcd_mcp_token).encode()).hexdigest(),
                lambda:RecordedReadOnly(url=settings.mcd_mcp_url,token=settings.mcd_mcp_token,timeout=25),False)
        record={'id':conversation['id'],'turns':[]};report['conversations'].append(record);save()
        with tempfile.TemporaryDirectory(prefix='commerce-chat-eval-') as tmp:
            env=DialogueEnvironment(Path(tmp)/'state.sqlite',conversation['id'],model)
            env.app.dependency_overrides[coach_commerce_dependency]=lambda:provider
            try:
                for i,turn in enumerate(conversation['turns'],1):
                    before=env.snapshot();start=len(env.client.calls);mcp_start=len(mcp_calls)
                    with env.factory() as db:fitness_before=business_state(db,db.get(User,DEMO_USER_ID),[str(DAY)])
                    response=env.http.post('/api/v1/agent/chat',headers=env.headers,json={
                        'text':turn['text'],'date':str(DAY),'thread':env.thread,'request_id':conversation['id']+':'+str(i)})
                    result=response.json().get('data') or {'overall_status':'http_error','reply':response.text}
                    after=env.snapshot()
                    with env.factory() as db:fitness_after=business_state(db,db.get(User,DEMO_USER_ID),[str(DAY)])
                    expected=turn['expect'];errors=[];caps=[x.get('capability_id') for x in result.get('task_results',[])]
                    if result.get('overall_status') not in expected['statuses']:errors.append('status:'+str(result.get('overall_status')))
                    for cap in expected.get('capabilities',[]):
                        if cap not in caps:errors.append('missing_capability:'+cap)
                    checkout=result.get('checkout') or {}
                    if expected.get('no_checkout') and checkout:errors.append('unexpected_checkout')
                    if expected.get('checkout_status') and checkout.get('status') not in expected['checkout_status']:errors.append('checkout:'+str(checkout.get('status')))
                    if expected.get('cart_names') is not None and [x['name'] for x in checkout.get('cart',[])]!=expected['cart_names']:errors.append('cart_names')
                    if expected.get('cart_matches_detail'):
                        from app.models import CommerceObject
                        with env.factory() as db:
                            for line in checkout.get('cart',[]):
                                product=db.get(CommerceObject,line['product_id'])
                                if not product or line['name']!=(product.payload.get('detail') or {}).get('name'):errors.append('cart_detail_name_mismatch')
                        if not checkout.get('cart'):errors.append('missing_cart')
                    if expected.get('cart_quantities') is not None and [x['quantity'] for x in checkout.get('cart',[])]!=expected['cart_quantities']:errors.append('cart_quantities')
                    if expected.get('take_way_title'):
                        title=next((w['title'] for w in checkout.get('quote',{}).get('take_ways',[]) if w['code']==checkout.get('take_way_code')),None)
                        if title!=expected['take_way_title']:errors.append('take_way:'+str(title))
                    if expected.get('same_checkout_as_turn'):
                        earlier=record['turns'][expected['same_checkout_as_turn']-1]['result'].get('checkout') or {}
                        if not earlier.get('id') or earlier['id']!=checkout.get('id'):errors.append('checkout_identity_changed')
                    if expected.get('total_cents') is not None and checkout.get('quote',{}).get('total_cents')!=expected['total_cents']:errors.append('total_cents')
                    for text in expected.get('reply_contains',[]):
                        if text not in result.get('reply',''):errors.append('reply:'+text)
                    unauthorized=diff(fitness_before,fitness_after)
                    if unauthorized:errors.append('fitness_modified')
                    if args.provider.startswith('mock') and fake.submits!=expected.get('total_mock_submits',0):errors.append('mock_submit_count:'+str(fake.submits))
                    row={'turn':i,'user_text':turn['text'],'understood_goals':result.get('task_plan'),
                        'context_and_model_calls':env.client.calls[start:],'tool_calls':mcp_calls[mcp_start:],
                        'actual_changes':diff(before,after),'unauthorized_changes':unauthorized,'reply':result.get('reply'),
                        'result':result,'history_after':env.history(),'before':before,'after':after,'oracle':expected,'errors':errors,'passed':not errors}
                    record['turns'].append(row);save()
                    print(json.dumps({'conversation':conversation['id'],'turn':i,'passed':not errors,'errors':errors,'reply':result.get('reply')},ensure_ascii=False),flush=True)
            finally:env.close()
    rows=[t for c in report['conversations'] for t in c['turns']]
    report.update(complete=True,total=len(rows),passed=sum(t['passed'] for t in rows),pass_rate=sum(t['passed'] for t in rows)/len(rows),
        unauthorized_changes=sum(len(t['unauthorized_changes']) for t in rows))
    save();model._client.close()


if __name__=='__main__':main()
