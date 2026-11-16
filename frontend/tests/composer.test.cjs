const {test}=require('node:test'); const assert=require('node:assert/strict');
const fs=require('node:fs'); const vm=require('node:vm'); const ts=require('typescript');
const source=fs.readFileSync('src/components/GlobalCoachWorkspace.vue','utf8');
function harness(){
 const handlers=source.slice(source.indexOf('const composing'),source.indexOf('async function send(text?:'));
 const sendStart=source.slice(source.indexOf('  const raw = (text ?? input.value).trim()'),source.indexOf('  // 发新消息'));
 const context={ref:value=>({value}), input:{value:'待发送的消息'}, busy:{value:false}, initializing:{value:false}, ticks:[], calls:0, redactMcdToken:x=>x};
 context.nextTick=fn=>context.ticks.push(fn);
 const code=handlers.replace(/\/\/ #ifndef H5[\s\S]*?\/\/ #endif/g,'')+
 '\nfunction send(text){ calls++;'+sendStart+'}\nglobalThis.api={onComposerInput,onComposerKeydown,onComposerConfirm,onCompositionEnd,composing,send,epoch:composerEpoch};';
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

test('composition end followed by Enter commits IME text without submitting',()=>{
 const c=harness();c.api.composing.value=true;
 c.api.onComposerKeydown({key:'Enter',isComposing:false,preventDefault(){throw Error('IME must receive Enter')}});
 c.api.onComposerInput({detail:{value:'我想做英文动作'}});c.api.onCompositionEnd();
 c.api.onComposerKeydown({key:'Enter',isComposing:false,preventDefault(){throw Error('composition acceptance is not send')}});
 assert.equal(c.calls,0);assert.equal(c.input.value,'我想做英文动作');
 assert.match(source, /workspace-composer" @compositionstart\.capture=/);
 assert.match(source, /@compositionend\.capture="onCompositionEnd"/);
});


test('an immediate stop retries an unregistered request without cancelling a later request',async()=>{
 const code=source.slice(source.indexOf('async function stopProcessing()'),source.indexOf('const composerEpoch'));
 class ApiError extends Error {constructor(code){super();this.statusCode=code}}
 const seen=[],toasts=[],ctx={activeRequestId:{value:'original'},stopping:{value:false},ApiError,setTimeout:f=>f(),uni:{showToast:t=>toasts.push(t)}};
 ctx.api={cancelAgentRequest:async id=>{seen.push(id);if(seen.length<3)throw new ApiError(404);return {status:'cancel_requested'}}};
 vm.runInNewContext(ts.transpileModule(code+'\nglobalThis.stop=stopProcessing',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 await ctx.stop();assert.deepEqual(seen,['original','original','original']);assert.equal(ctx.stopping.value,false);assert.equal(toasts.length,0);
 seen.length=0;ctx.api.cancelAgentRequest=async id=>{seen.push(id);ctx.activeRequestId.value='later';throw new ApiError(404)};
 await ctx.stop();assert.deepEqual(seen,['original']);assert.equal(ctx.stopping.value,false);
});
