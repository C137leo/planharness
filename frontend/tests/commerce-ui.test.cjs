const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const context={exports:{},Date};vm.runInNewContext(ts.transpileModule(fs.readFileSync('src/styles/commerceUi.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context);
const {paymentPresentation}=context.exports;
test('official naive payment deadlines use China time in any client timezone',()=>{
 const order={expire_at:'2026-11-12 01:46:41',verified:true,status_code:'1'};
 assert.equal(paymentPresentation(order,Date.parse('2026-11-11T17:46:40Z')).expired,false);
 assert.equal(paymentPresentation(order,Date.parse('2026-11-11T17:46:42Z')).expired,true);
});
test('paid, cancelled and expired receipts link to view without inviting another payment',()=>{
 for(const status_code of ['2','4','6','7','8','10'])assert.equal(paymentPresentation({status_code},0).label,'查看官方订单 →');
 assert.equal(paymentPresentation({verified:false,status_code:'1'},0).label,'前往官方核对并付款 →');
 assert.equal(paymentPresentation({verified:true,status_code:'1'},0).label,'去麦当劳付款 →');
});
test('token is saved locally and never emitted as a chat command field',()=>{
 const source=fs.readFileSync('src/components/McdCommerceCard.vue','utf8');
 const code=source.slice(source.indexOf('function saveToken()'),source.indexOf('function findStores()'));
 const emitted=[],saved=[];const ctx={props:{busy:false},token:{value:' personal-token '},localError:{value:''},tokenOpen:{value:true},setStoredMcdToken:v=>saved.push(v),emit:(...v)=>emitted.push(v)};
 vm.runInNewContext(ts.transpileModule(code+'\nglobalThis.save=saveToken',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 ctx.save();assert.equal(saved[0],'personal-token');assert.equal(ctx.token.value,'');assert.equal(emitted[0][1].action,'connect');assert.equal(JSON.stringify(emitted).includes('personal-token'),false);
 ctx.props.busy=true;ctx.token.value='second-token';ctx.save();assert.equal(saved.length,1);
});

test('link opener preserves existing HTTP sources and opens HTTPS payment, rejecting script schemes',()=>{
 const source=fs.readFileSync('src/components/GlobalCoachWorkspace.vue','utf8');
 const code=source.slice(source.indexOf('function openSource('),source.indexOf('\nfunction applyGuidance('));
 const calls=[],ctx={open:(...args)=>calls.push(args),uni:{}};
 vm.runInNewContext(ts.transpileModule(code+'\nglobalThis.follow=openSource',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 ctx.follow('http://example.test/source');ctx.follow('https://m.mcd.cn/pay');ctx.follow('javascript:alert(1)');
 assert.equal(calls.length,2);assert.equal(calls[1][0],'https://m.mcd.cn/pay');
});

const {isCommerceResult,dayAttachment}=context.exports;
test('pending commerce read is never classified as a daily adjustment merely for carrying a snapshot',()=>{
 const result={day:{summary:'按当前计划'},task_plan:[{capability_id:'commerce.product_detail'}],pending_confirmation:{tasks:[{capability_id:'commerce.product_detail'}]}};
 assert.equal(isCommerceResult(result),true);assert.equal(dayAttachment(result),undefined);
 const source=fs.readFileSync('src/components/GlobalCoachWorkspace.vue','utf8');
 const code=source.slice(source.indexOf('function attachmentFor('),source.indexOf('\nfunction showAttachment('));
 const ctx={isCommerceResult,dayAttachment,checkoutLabels:{}};
 vm.runInNewContext(ts.transpileModule(code+'\nglobalThis.attachment=attachmentFor',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 assert.equal(ctx.attachment(result).kind,'麦当劳点餐');
});
test('only real day changes get adjustment labels; profile changes and generic snapshots do not',()=>{
 assert.equal(dayAttachment({day:{summary:'原安排'},task_results:[]}),undefined);
 assert.equal(dayAttachment({day:{},task_results:[{status:'success',actual_changes:[{kind:'profile'}]}]}),undefined);
 assert.equal(dayAttachment({day:{summary:'训练已更新'},task_results:[{status:'success',actual_changes:[{kind:'actions'}]}]}).kind,'今日调整');
});
test('day inspections are labelled current arrangement, never a mutation',()=>{
 assert.equal(dayAttachment({day:{},task_plan:[{capability_id:'context.inspect'}],task_results:[]}).kind,'今日安排');
});

test('editing another date cannot label the accompanying current snapshot as today changed',()=>{
 assert.equal(dayAttachment({day:{date:'2026-11-12'},task_results:[{status:'success',actual_changes:[{kind:'actions',date:'2026-11-13'}]}]}),undefined);
});
