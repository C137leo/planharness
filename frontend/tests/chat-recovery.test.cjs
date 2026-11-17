const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const source=fs.readFileSync('src/api/client.ts','utf8');
const snippet=source.slice(source.indexOf('async function chatWithRecovery('),source.indexOf('/** 后端版本可能比前端旧'));
class ApiError extends Error {constructor(message,statusCode=0,detail=null){super(message);this.statusCode=statusCode;this.detail=detail;}}
function ledger(storage){
 const ctx={exports:{},Date,uni:{getStorageSync:k=>storage.get(k),setStorageSync:(k,v)=>storage.set(k,structuredClone(v))}};
 const code=fs.readFileSync('src/api/chatRequests.ts','utf8');
 vm.runInNewContext(ts.transpileModule(code,{compilerOptions:{target:ts.ScriptTarget.ES2020,module:ts.ModuleKind.CommonJS}}).outputText,ctx);
 return ctx.exports;
}
function setup(responses,storage=new Map()){
 const calls=[];let auth='account-a';
 const ctx={...ledger(storage),accountStorageId:()=>auth,ApiError,Date,Promise,encodeURIComponent,getStoredToken:()=>auth,setTimeout:fn=>{fn();return 0;},request:async(...args)=>{
  calls.push(args);const next=responses.shift();if(next instanceof Error)throw next;return typeof next==='function'?next():next;
 }};
 vm.runInNewContext(ts.transpileModule(snippet+'\nglobalThis.run=chatWithRecovery;globalThis.recover=recoverSavedChat',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 return {run:ctx.run,recover:ctx.recover,storage,calls,account:value=>{auth=value;}};
}
test('timeout reconciles the same claim without a second POST or copied MCP credentials',async()=>{
 const result={reply:'已完成',run_id:'original-run'};
 const s=setup([new ApiError('timeout'),{status:'running',result:null},{status:'succeeded',result}]);
 assert.equal(await s.run({request_id:'same-id',text:'确认'},{'X-MCD-Token':'test-only'}),result);
 assert.deepEqual(s.calls.map(c=>c[0]),['POST','GET','GET']);
 assert(s.calls.slice(1).every(c=>c[1]==='/agent/requests/same-id' && c.length===2));
});
test('request-id conflict and validation rejection never become an unrelated old result',async()=>{
 for(const status of [409,422]){
  const s=setup([new ApiError('rejected',status),{status:'succeeded',result:{reply:'wrong'}}]);
  await assert.rejects(s.run({request_id:'same-id'},{}),/rejected/);assert.equal(s.calls.length,1);
 }
});
test('unknown outcome is explicit and never resubmitted',async()=>{
 const s=setup([new ApiError('network'),{status:'needs_reconciliation',result:null}]);
 await assert.rejects(s.run({request_id:'same-id'},{}),e=>e.detail.request_pending===true);
 assert.deepEqual(s.calls.map(c=>c[0]),['POST','GET']);
});
test('account switch discards the old response and stops polling',async()=>{
 let s; s=setup([()=>{s.account('account-b');return {reply:'private-old-account'};}]);
 await assert.rejects(s.run({request_id:'id'},{}),/账户已变化/);assert.equal(s.calls.length,1);
});

function requestSetup(){
 let auth='account-a',reject,resolve,logouts=0,start; const started=new Promise(r=>{start=r;});
 const ctx={ApiError,Promise,getStoredToken:()=>auth,ensureToken:async()=>auth,
  handleUnauthorized:()=>{logouts++;auth='';},rawRequest:()=>new Promise((yes,no)=>{resolve=yes;reject=no;start();})};
 const wrapper=source.slice(source.indexOf('async function request<T>'),source.indexOf('async function chatWithRecovery('));
 vm.runInNewContext(ts.transpileModule(wrapper+'\nglobalThis.run=request',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 return {run:ctx.run,started,account:value=>{auth=value;},succeed:()=>resolve({private:'old-account'}),fail:()=>reject(new ApiError('expired',401)),getAuth:()=>auth,logouts:()=>logouts};
}
test('late 401 from an old account cannot sign out the new account',async()=>{
 const s=requestSetup(),pending=s.run('GET','/me');await s.started;
 s.account('account-b');s.fail();
 await assert.rejects(pending,/expired/);assert.equal(s.getAuth(),'account-b');assert.equal(s.logouts(),0);
});
test('401 for the current account still signs it out',async()=>{
 const s=requestSetup(),pending=s.run('GET','/me');await s.started;s.fail();
 await assert.rejects(pending,/expired/);assert.equal(s.getAuth(),'');assert.equal(s.logouts(),1);
});
test('late successful response from an old account is discarded',async()=>{
 const s=requestSetup(),pending=s.run('GET','/me');await s.started;s.account('account-b');s.succeed();
 await assert.rejects(pending,/账户已变化/);assert.equal(s.getAuth(),'account-b');assert.equal(s.logouts(),0);
});

test('page reload resumes a saved claim with GET only and stores no bodies or credentials',async()=>{
 const storage=new Map();
 const old=setup([()=>new Promise(()=>{})],storage);
 void old.run({request_id:'reload-id',thread:'saved-thread',text:'private-message'},{'X-MCD-Token':'secret-token'});
 assert(!JSON.stringify([...storage]).includes('private-message'));
 assert(!JSON.stringify([...storage]).includes('secret-token'));
 const fresh=setup([{request_id:'reload-id',thread:'saved-thread',status:'succeeded',result:{reply:'已保存'}}],storage);
 assert.equal((await fresh.recover('saved-thread')).result.reply,'已保存');
 assert.deepEqual(fresh.calls.map(c=>c[0]),['GET']);
 assert.equal(await fresh.recover('saved-thread'),null);
});
test('unknown saved claim survives refresh; another account cannot load it',async()=>{
 const storage=new Map(),old=setup([new ApiError('offline'),{status:'needs_reconciliation',result:null}],storage);
 await assert.rejects(old.run({request_id:'unknown-id',thread:'thread'},{}));
 const fresh=setup([{request_id:'unknown-id',thread:'thread',status:'needs_reconciliation',result:null}],storage);
 assert.equal((await fresh.recover('thread')).status,'needs_reconciliation');
 fresh.account('account-b');assert.equal(await fresh.recover('thread'),null);assert.equal(fresh.calls.length,1);
 assert.equal(ledger(storage).savedChatRequests('account-a').length,1);
});
