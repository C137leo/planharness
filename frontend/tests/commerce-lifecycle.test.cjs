const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const ctx={exports:{}};vm.runInNewContext(ts.transpileModule(fs.readFileSync('src/styles/commerceUi.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,ctx);
const {paymentPresentation:p}=ctx.exports;
test('paid and cancelled orders never show expired payment instructions',()=>{
 for(const order of [{lifecycle:'paid'},{lifecycle:'cancelled'},{status_code:'2'},{status:'已取消'}]){
  const view=p({...order,pay_url:'https://www.mcd.cn/pay',expire_at:'2026-11-12 01:30:00'},Date.parse('2026-11-13T00:00:00Z'));
  assert.equal(view.expired,false);assert.equal(view.canPay,false);assert.equal(view.closed,true);
 }
});
test('effective backend deadline and China local timestamps disable late payment',()=>{
 const now=Date.parse('2026-11-13T01:00:00Z');
 assert.equal(p({lifecycle:'unpaid',pay_url:'https://www.mcd.cn/pay',payment_deadline:'2026-11-13T01:00:00+00:00'},now).canPay,false);
 assert.equal(p({lifecycle:'unpaid',pay_url:'https://www.mcd.cn/pay',expire_at:'2026-11-13 09:01:00'},now).canPay,true);
 assert.equal(p({lifecycle:'expired',pay_url:'https://www.mcd.cn/pay'},now).canPay,false);
});
test('unverified full cart does not hide a valid old order payment link',()=>{
 const view=p({lifecycle:'unpaid',verified:false,pay_url:'https://www.mcd.cn/pay',payment_deadline:'2026-11-13T01:15:00Z'},Date.parse('2026-11-13T01:00:00Z'));
 assert.equal(view.canPay,true);assert.equal(view.label,'前往官方核对并付款 →');
});
test('existing-order buttons preserve target, version, related order and decision fingerprint',async()=>{
 const src=fs.readFileSync('src/components/GlobalCoachWorkspace.vue','utf8');let code=src.slice(src.indexOf('async function runAction('),src.indexOf("    case 'confirm_tasks':",src.indexOf('async function runAction(')))+'  }\n}';
 const calls=[],ctx={busy:{value:false},send:(...args)=>calls.push(args)};
 vm.runInNewContext(ts.transpileModule(code+'\nglobalThis.run=runAction',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 for(const [type,action] of [['checkout_existing_query','query_existing'],['checkout_existing_cancel','cancel_existing'],['checkout_place_new','place_new']]){
  await ctx.run({type,label:'明确选择',checkout_id:'new',version:4,existing_checkout_id:'old',decision_fingerprint:'fingerprint'});
  const c=calls.at(-1)[2];assert.equal(c.action,action);assert.equal(c.checkout_id,'new');assert.equal(c.version,4);assert.equal(c.existing_checkout_id,'old');assert.equal(c.decision_fingerprint,'fingerprint');
 }
});
