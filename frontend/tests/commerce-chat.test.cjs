const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');const ts=require('typescript');
const source=fs.readFileSync('src/components/GlobalCoachWorkspace.vue','utf8');
function compile(s,c){vm.runInNewContext(ts.transpileModule(s,{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,c);}
test('checkout chips preserve server draft/version and use structured actions',async()=>{
 const code=source.slice(source.indexOf('async function runAction('),source.indexOf('\nfunction openSource('));
 const calls=[];const ctx={busy:{value:false},send:async(...args)=>calls.push(args)};
 compile(code+'\nglobalThis.run=runAction',ctx);
 await ctx.run({type:'checkout_way',label:'外带',checkout_id:'draft-id',version:3,take_way_code:'locker-out'});
 assert.equal(calls[0][0],'外带');assert.equal(calls[0][1],undefined);
 assert.equal(JSON.stringify(calls[0][2]),JSON.stringify({action:'choose_take_way',checkout_id:'draft-id',version:3,take_way_code:'locker-out'}));
 await ctx.run({type:'checkout_query',label:'查询',checkout_id:'draft-id',version:3});assert.equal(calls[1][2].action,'query');
 ctx.busy.value=true;await ctx.run({type:'checkout_query',checkout_id:'draft-id',version:3});assert.equal(calls.length,2);
});
test('chat API sends token in header and checkout action in body',()=>{
 const client=fs.readFileSync('src/api/client.ts','utf8');
 const code=client.slice(client.indexOf('  agentChat: ('),client.indexOf('  agentMessages: ('));
 const calls=[];const ctx={request:(...args)=>calls.push(args),mcdHeaders:t=>({'X-MCD-Token':t}),getStoredMcdToken:()=> 'test-token'};
 compile('const api={'+code+'};globalThis.api=api',ctx);
 ctx.api.agentChat('外带','today','2026-11-12','',{},'thread','req',undefined,{action:'choose_take_way',checkout_id:'id',version:2,take_way_code:'locker-out'});
 assert.equal(calls[0][1],'/agent/chat');assert.equal(calls[0][2].checkout_action.version,2);assert.equal(calls[0][3]['X-MCD-Token'],'test-token');
 assert.equal(JSON.stringify(calls[0][2]).includes('test-token'),false);
});
test('checkout and catalogue cards render independently of fitness cards',()=>{
 const computedBody=source.slice(source.indexOf('const hasRichResult'),source.indexOf('const visibleOrderMessageId'));
 assert.match(computedBody,/result\.value\?\.checkout/);assert.match(computedBody,/result\.value\?\.commerce_catalog/);
 const card=fs.readFileSync('src/components/McdCommerceCard.vue','utf8');
 assert.match(source,/<McdCommerceCard/);assert.doesNotMatch(source,/openAgentLink/);
 assert.match(card,/emit\('open',order.pay_url\)/);assert.match(card,/quote.total_cents\/100/);
 assert.match(card,/item.specifications/);

});
