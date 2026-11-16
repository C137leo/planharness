const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript'),vue=require('vue');
function card(status,initial=null,stored='') {
 const mounted=[],props=vue.reactive({result:initial,actions:[],busy:false});
 const ctx={...vue,props,api:{commercePreferences:()=>Promise.resolve({city:'',keyword:''}),commerceStatus:()=>typeof status==='function'?status():Promise.resolve({status})},getStoredMcdToken:()=>stored,setStoredMcdToken:()=>{},setInterval:()=>0,clearInterval:()=>{},defineProps:()=>props,defineEmits:()=>()=>{},onMounted:f=>mounted.push(f),onUnmounted:()=>{}};
 const script=fs.readFileSync('src/components/McdCommerceCard.vue','utf8').split('<script setup lang="ts">')[1].split('</script>')[0].replace(/^import .*$/gm,'').replace('const props = defineProps','const propsUnused = defineProps');
 vm.runInNewContext(ts.transpileModule(script+'\nglobalThis.view={tokenOpen,connection,toggleTokenPanel,saveToken,token}',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 return {ctx,props,view:ctx.view,mount:mounted[0]};
}
test('fresh ready status replaces stale missing-token attachment',async()=>{
 const c=card('ready',{commerce_connection:{status:'token_required'}});await c.mount();assert.equal(c.view.tokenOpen.value,false);assert.equal(c.view.connection.value,'ready');
});
test('missing credential automatically shows input and saved local credentials stay collapsed',async()=>{
 const missing=card('token_required');await missing.mount();assert.equal(missing.view.tokenOpen.value,true);
 const saved=card('ready',null,'saved-token');await saved.mount();assert.equal(saved.view.tokenOpen.value,false);
});
test('explicit settings click survives delayed status response and toggles closed',async()=>{
 let resolve;const c=card(()=>new Promise(r=>resolve=r));const pending=c.mount();c.view.toggleTokenPanel();resolve({status:'ready'});await pending;
 assert.equal(c.view.tokenOpen.value,true);c.view.toggleTokenPanel();assert.equal(c.view.tokenOpen.value,false);
});
test('fresh authentication failure outranks in-flight configured response',async()=>{
 let resolve;const c=card(()=>new Promise(r=>resolve=r));const pending=c.mount();c.props.result={commerce_connection:{status:'token_required'}};await vue.nextTick();resolve({status:'ready'});await pending;
 assert.equal(c.view.connection.value,'token_required');assert.equal(c.view.tokenOpen.value,true);
});
test('saving collapses input; authentication failure reopens it even if a token was saved',async()=>{
 const c=card('ready',null,'saved-token');await c.mount();c.view.toggleTokenPanel();c.view.token.value='replacement';c.view.saveToken();assert.equal(c.view.tokenOpen.value,false);
 c.props.result={commerce_connection:{status:'token_required'}};await vue.nextTick();assert.equal(c.view.tokenOpen.value,true);
});
test('status outage remains unknown and allows manual credential settings',async()=>{
 const c=card(()=>Promise.reject(Error('offline')));await c.mount();assert.equal(c.view.connection.value,'unknown');assert.equal(c.view.tokenOpen.value,false);
 c.view.toggleTokenPanel();assert.equal(c.view.tokenOpen.value,true);
});
