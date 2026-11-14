const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript'),vue=require('vue'),{parse,compileTemplate}=require('@vue/compiler-sfc');
const source=fs.readFileSync('src/components/McdOrderHistory.vue','utf8');
function component(overrides={}){
 const mounted=[],unmounted=[],events=[],calls=[],timers=[];const api={commerceOrders:async group=>{calls.push(group);return {items:[],counts:{active:0,paid:0,inactive:0,unknown:0},next_cursor:'',as_of:'now'}},commerceOrderDetail:async id=>({id}),...overrides};
 const context={...vue,api,paymentPresentation:(order,now)=>({expired:order?.lifecycle==='unknown'||order?.lifecycle==='expired',canPay:true}),Date,setInterval:fn=>{timers.push(fn);return timers.length},clearInterval:()=>{},document:{addEventListener:()=>{},removeEventListener:()=>{},activeElement:null},uni:{setClipboardData:()=>{}},onMounted:fn=>mounted.push(fn),onUnmounted:fn=>unmounted.push(fn),defineEmits:()=> (...args)=>events.push(args)};
 const code=source.split('<script setup lang="ts">')[1].split('</script>')[0].replace(/^import .*$/gm,'');
 vm.runInNewContext(ts.transpileModule(code+'\nglobalThis.view={group,items,selected,counts,cursor,loading,load,chooseGroup,selectOrder,refreshOne,syncAll,payment,detailStatus,followOrder}',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,context);
 return {view:context.view,events,calls,timers,mount:mounted[0],unmount:unmounted[0]};
}
test('history template compiles with bounded list and details and no order mutation controls',()=>{
 const {descriptor}=parse(source);const compiled=compileTemplate({source:descriptor.template.content,filename:'McdOrderHistory.vue',id:'history',compilerOptions:{expressionPlugins:['typescript']}});
 assert.deepEqual(compiled.errors,[]);assert.equal((descriptor.template.content.match(/<StableScroll/g)||[]).length,2);
 assert(source.includes("chooseGroup('inactive')"));assert(!source.includes('submit_order'));assert(!source.includes('cancel_order'));
});
test('history entry opens separate UI without sending chat or replacing existing result',()=>{
 const workspace=fs.readFileSync('src/components/GlobalCoachWorkspace.vue','utf8');const entry=workspace.match(/<button class="composer-orders-entry"[^>]*>/)[0];
 assert(entry.includes('@click="orderHistoryOpen=true"'));assert(!entry.includes('send('));assert(workspace.includes('<McdOrderHistory v-if="orderHistoryOpen"'));
});
test('late list results cannot overwrite a newly selected category',async()=>{
 let resolve;const c=component({commerceOrders:group=>group==='active'?new Promise(r=>resolve=r):Promise.resolve({items:[{id:'paid'}],counts:{active:0,paid:1,inactive:0,unknown:0},next_cursor:'',as_of:'new'})});
 const first=c.view.load();c.view.chooseGroup('paid');await Promise.resolve();await Promise.resolve();resolve({items:[{id:'stale'}],counts:{active:1,paid:0,inactive:0,unknown:0},next_cursor:'old',as_of:'old'});await first;
 assert.equal(c.view.items.value[0].id,'paid');assert.equal(c.view.cursor.value,'');
});
test('late detail response cannot replace another selected order',async()=>{
 let a,b;const c=component({commerceOrderDetail:id=>new Promise(resolve=>id==='a'?a=resolve:b=resolve)});
 const first=c.view.selectOrder({id:'a'}),second=c.view.selectOrder({id:'b'});b({id:'b',store:{name:'right'}});await second;a({id:'a',store:{name:'wrong'}});await first;
 assert.equal(c.view.selected.value.id,'b');
});
test('unknown is never presented as expired or direct payment despite old deadline',()=>{
 const c=component();c.view.selected.value={group:'unknown',status_label:'待核实',order:{lifecycle:'unknown'}};
 assert.equal(c.view.payment.value.expired,false);assert.equal(c.view.payment.value.canPay,false);assert.equal(c.view.detailStatus.value,'待核实');
 c.view.selected.value={group:'active',status_label:'待支付',order:{lifecycle:'expired'}};assert.equal(c.view.detailStatus.value,'已过期');
});
test('unmount prevents late list writes and official link opening is explicit',async()=>{
 let resolve;const c=component({commerceOrders:()=>new Promise(r=>resolve=r)});const loading=c.view.load();c.unmount();resolve({items:[{id:'late'}],counts:{active:1},next_cursor:'',as_of:'now'});await loading;assert.equal(c.view.items.value.length,0);
 c.view.selected.value={order:{pay_url:'javascript:bad'}};c.view.followOrder();assert.equal(c.events.length,0);
 c.view.selected.value={order:{pay_url:'https://www.mcd.cn/pay'}};c.view.followOrder();assert.equal(c.events[0][0],'open');
});

test('closing while initial fetch is pending does not leave an expiry timer behind',async()=>{
 let resolve;const c=component({commerceOrders:()=>new Promise(r=>resolve=r)});const mounted=c.mount();c.unmount();
 resolve({items:[],counts:{active:0,paid:0,inactive:0,unknown:0},next_cursor:'',as_of:'now'});await mounted;
 assert.equal(c.timers.length,0);
});
