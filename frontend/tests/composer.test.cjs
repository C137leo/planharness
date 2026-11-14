const {test}=require('node:test'); const assert=require('node:assert/strict');
const fs=require('node:fs'); const vm=require('node:vm'); const ts=require('typescript');
const source=fs.readFileSync('src/components/GlobalCoachWorkspace.vue','utf8');
function harness(){
 const handlers=source.slice(source.indexOf('const composerEpoch'),source.indexOf('async function send(text?:'));
 const sendStart=source.slice(source.indexOf('  const raw = (text ?? input.value).trim()'),source.indexOf('  // 发新消息'));
 const context={ref:value=>({value}), input:{value:'待发送的消息'}, busy:{value:false}, ticks:[], calls:0, redactMcdToken:x=>x};
 context.nextTick=fn=>context.ticks.push(fn);
 const code=handlers.replace(/\/\/ #ifndef H5[\s\S]*?\/\/ #endif/g,'')+
 '\nfunction send(text){ calls++;'+sendStart+'}\nglobalThis.api={onComposerInput,onComposerKeydown,onComposerConfirm,send,epoch:composerEpoch};';
 vm.runInNewContext(ts.transpileModule(code,{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,context);return context;
}
test('Enter sends once, clears state, ignores stale input and replaces textarea',()=>{
 const c=harness(); let prevented=0;c.api.onComposerKeydown({key:'Enter',preventDefault(){prevented++}});
 assert.equal(c.calls,1); assert.equal(prevented,1);assert.equal(c.input.value,'');assert.equal(c.api.epoch.value,1);
 c.api.onComposerConfirm();assert.equal(c.calls,1);
 c.api.onComposerInput({detail:{value:'待发送的消息'}});assert.equal(c.input.value,'');
 c.ticks.forEach(fn=>fn());c.api.onComposerInput({detail:{value:'下一条'}});assert.equal(c.input.value,'下一条');
 assert.match(source,/:key="composerEpoch"/);assert.doesNotMatch(source,/v-model="input"/);
});
test('IME confirmation and Shift Enter never submit',()=>{
 const c=harness();for(const event of [{key:'Enter',isComposing:true},{key:'Enter',keyCode:229},{key:'Enter',shiftKey:true}]) c.api.onComposerKeydown({...event,preventDefault(){throw Error('unexpected prevention')}});
 assert.equal(c.calls,0);assert.equal(c.input.value,'待发送的消息');
});
test('busy or empty submission does not clear a new draft',()=>{
 const c=harness();c.busy.value=true;c.api.send();assert.equal(c.input.value,'待发送的消息');assert.equal(c.api.epoch.value,0);
 c.busy.value=false;c.input.value='  ';c.api.send();assert.equal(c.api.epoch.value,0);
});
