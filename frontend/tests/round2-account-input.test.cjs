const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const source=fs.readFileSync('src/api/client.ts','utf8');
test('MCP credential requires an identified account and never adopts legacy or another account token',()=>{
 const store=new Map([['planharness_mcd_token','unowned-legacy']]);
 const ctx={exports:{},atob,uni:{getStorageSync:k=>store.get(k),setStorageSync:(k,v)=>store.set(k,v),removeStorageSync:k=>store.delete(k)}};
 const identity={exports:{},atob,uni:ctx.uni};
 vm.runInNewContext(ts.transpileModule(fs.readFileSync('src/api/accountStorage.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,identity);
 Object.assign(ctx,identity.exports);
 const code=source.slice(source.indexOf("const TOKEN_KEY ="),source.indexOf('function mcdHeaders('));
 vm.runInNewContext(ts.transpileModule(code,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 const api=ctx.exports,auth=id=>'x.'+Buffer.from(JSON.stringify({sub:id})).toString('base64url')+'.x';
 assert.equal(api.getStoredMcdToken(),'');api.setStoredMcdToken('anonymous');
 api.setStoredToken(auth('one'));assert.equal(api.getStoredMcdToken(),'');api.setStoredMcdToken('one-only');
 api.setStoredToken(auth('two'));assert.equal(api.getStoredMcdToken(),'');api.setStoredMcdToken('two-only');
 api.setStoredToken(auth('one'));assert.equal(api.getStoredMcdToken(),'one-only');
 assert.equal(store.get('planharness_mcd_token'),'unowned-legacy');
});
test('registration explains each disabled condition and enables only after valid fields',()=>{
 const src=fs.readFileSync('src/pages/login/index.vue','utf8');
 const ctx={exports:{},mode:{value:'register'},busy:{value:false},lockedSeconds:{value:0},form:{phone:'',password:''},computed:f=>({get value(){return f()}})};
 const code=src.slice(src.indexOf('const isRegister'),src.indexOf('const strength'))+'\nglobalThis.state={canSubmit,submitRestriction};';
 vm.runInNewContext(ts.transpileModule(code,{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 assert.match(ctx.state.submitRestriction.value,/手机号/);
 ctx.form.phone='19900061118';assert.match(ctx.state.submitRestriction.value,/至少8位/);
 ctx.form.password='longenough';assert.match(ctx.state.submitRestriction.value,/字母或数字/);
 ctx.form.password='ValidLocalPass42!';assert.equal(ctx.state.canSubmit.value,true);
 ctx.lockedSeconds.value=4;assert.match(ctx.state.submitRestriction.value,/4 秒/);assert.equal(ctx.state.canSubmit.value,false);
});

test('order cache initializes independently of API client and follows account changes',()=>{
 const store=new Map([['planharness_last_mcd_order',{order_id:'unowned'}]]);let owner='one';
 const ctx={exports:{},Date,uni:{getStorageSync:k=>store.get(k),setStorageSync:(k,v)=>store.set(k,v),removeStorageSync:k=>store.delete(k)},require:name=>{
  if(name==='vue')return {ref:value=>({value})};
  if(name==='../api/accountStorage')return {currentAccountId:()=>owner};
  throw Error('cache must not import API client: '+name);
 }};
 vm.runInNewContext(ts.transpileModule(fs.readFileSync('src/composables/coachWorkspace.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 const api=ctx.exports;assert.equal(api.coachWorkspaceLastMcdOrder.value,null);
 api.setLastMcdOrder({order_id:'one'});owner='two';api.resetCoachWorkspace();assert.equal(api.coachWorkspaceLastMcdOrder.value,null);
 api.setLastMcdOrder({order_id:'two'});owner='one';api.resetCoachWorkspace();assert.equal(api.coachWorkspaceLastMcdOrder.value.order_id,'one');
 owner='';api.resetCoachWorkspace();assert.equal(api.coachWorkspaceLastMcdOrder.value,null);
});
