const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const source=fs.readFileSync('src/pages/console/index.vue','utf8');
function setup(admin){
 const writes=[];
 const ctx={Error,ref:value=>({value}),alive:true,api:{me:async()=>({is_llm_admin:admin}),getLlmSwitch:async()=>({paused:true}),setLlmSwitch:async paused=>{writes.push(paused);return {paused};}}};
 const script=source.slice(source.indexOf('const isLlmAdmin='),source.indexOf("const thread=ref("));
 vm.runInNewContext(ts.transpileModule(script+'\nglobalThis.controls={loadLlmSwitch,toggleLlm,isLlmAdmin,llmPaused,llmError};',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 return {...ctx.controls,api:ctx.api,writes};
}
test('only the server-authorized account can load and operate the switch',async()=>{
 const s=setup(false);let reads=0;s.api.getLlmSwitch=async()=>{reads++;return {paused:true};};
 await s.loadLlmSwitch();await s.toggleLlm();assert.equal(s.isLlmAdmin.value,false);assert.equal(reads,0);assert.equal(s.writes.length,0);
});
test('admin can resume and pause, and permission refresh removes the control',async()=>{
 const s=setup(true);await s.loadLlmSwitch();assert.equal(s.isLlmAdmin.value,true);assert.equal(s.llmPaused.value,true);
 await s.toggleLlm();assert.equal(s.llmPaused.value,false);await s.toggleLlm();assert.deepEqual(s.writes,[false,true]);
 s.api.me=async()=>({is_llm_admin:false});await s.loadLlmSwitch();assert.equal(s.isLlmAdmin.value,false);assert.equal(s.llmPaused.value,null);
});
test('failed state read leaves the switch disabled and supports retry',async()=>{
 const s=setup(true);s.api.getLlmSwitch=async()=>{throw new Error('offline');};await s.loadLlmSwitch();await s.toggleLlm();
 assert.equal(s.llmPaused.value,null);assert.equal(s.llmError.value,'offline');assert.equal(s.writes.length,0);
 s.api.getLlmSwitch=async()=>({paused:false});await s.loadLlmSwitch();assert.equal(s.llmPaused.value,false);assert.equal(s.llmError.value,'');
});
