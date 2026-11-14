const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript'),vue=require('vue');
const helper={exports:{}};vm.runInNewContext(ts.transpileModule(fs.readFileSync('src/styles/commerceCart.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,helper);
const {selectedCart}=helper.exports;
function card(result=null,apiOverrides={}) {
 const mounted=[],events=[],saved=[],props=vue.reactive({result,actions:[],busy:false});
 const api={commerceStatus:()=>Promise.resolve({status:'ready'}),commercePreferences:()=>Promise.resolve({city:'北京',keyword:'三里屯'}),saveCommercePreferences:async(city,keyword)=>{saved.push({city,keyword});return {city,keyword}},...apiOverrides};
 const ctx={...vue,selectedCart,paymentPresentation:()=>({expired:false,label:''}),api,getStoredMcdToken:()=>'',setStoredMcdToken:()=>{},setInterval:()=>0,clearInterval:()=>{},defineProps:()=>props,defineEmits:()=> (...args)=>events.push(args),onMounted:f=>mounted.push(f),onUnmounted:()=>{}};
 const script=fs.readFileSync('src/components/McdCommerceCard.vue','utf8').split('<script setup lang="ts">')[1].split('</script>')[0].replace(/^import .*$/gm,'');
 vm.runInNewContext(ts.transpileModule(script+'\nglobalThis.view={city,keyword,savedLocation,saveLocation,menuOpen,quantities,preview,changeMenu,connection,localError,markEdited:()=>{locationEdited=true}}',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 return {props,events,saved,view:ctx.view,mount:mounted[0]};
}
test('quantity edits preserve explicit official option choices and new items use defaults',()=>{
 const line={product_id:'wing',quantity:1,use_default_options:false,selections:[{option_id:'spice',quantity:1}]};
 const selected=selectedCart({wing:2,burger:1,removed:0},[{product_id:'wing',selection:line}]);
 assert.equal(JSON.stringify(selected),JSON.stringify([{...line,quantity:2},{product_id:'burger',use_default_options:true,quantity:1}]));
 assert.equal(line.quantity,1);
});
test('saved location fills empty fields and can be explicitly saved or cleared',async()=>{
 const c=card();await c.mount();assert.equal(c.view.city.value,'北京');assert.equal(c.view.keyword.value,'三里屯');
 c.view.city.value='上海';c.view.keyword.value='人民广场';await c.view.saveLocation(false);assert.equal(c.saved[0].city,'上海');
 await c.view.saveLocation(true);assert.equal(c.saved[1].city,'');assert.equal(c.view.savedLocation.value.city,'');
 c.props.busy=true;await c.view.saveLocation(false);assert.equal(c.saved.length,2);
});
test('late saved location never overwrites fields the user has started editing',async()=>{
 let resolve;const c=card(null,{commercePreferences:()=>new Promise(r=>resolve=r)});await c.mount();
 c.view.city.value='杭州';c.view.keyword.value='西湖';c.view.markEdited();resolve({city:'北京',keyword:'三里屯'});await Promise.resolve();
 assert.equal(c.view.city.value,'杭州');assert.equal(c.view.keyword.value,'西湖');
});
test('location loading outage does not block token connection or expose a fabricated city',async()=>{
 const c=card(null,{commercePreferences:()=>Promise.reject(Error('offline'))});await c.mount();await Promise.resolve();
 assert.equal(c.view.connection.value,'ready');assert.equal(c.view.city.value,'');
});
test('change-menu uses actual checkout version and preview revises that same cart',()=>{
 const checkout={id:'quote-id',version:4,status:'awaiting_confirmation',cart:[{product_id:'wing',quantity:1,selection:{product_id:'wing',quantity:1,use_default_options:false,selections:[{option_id:'official-option',quantity:1}]}}]};
 const c=card({checkout,commerce_view:'edit_menu',commerce_catalog:{kind:'products',store_id:'store-id',items:[]}});
 assert.equal(c.view.menuOpen.value,true);c.view.changeMenu();assert.equal(c.events[0][1].action,'checkout_menu');assert.equal(c.events[0][1].version,4);
 c.view.quantities.value={wing:2};c.view.preview();const command=c.events[1][1];assert.equal(command.action,'revise_order');assert.equal(command.target_id,'quote-id');assert.equal(command.version,4);assert.equal(command.items[0].selections[0].option_id,'official-option');
});
test('workspace menu and revision buttons send explicit labels and versioned actions',()=>{
 const source=fs.readFileSync('src/components/GlobalCoachWorkspace.vue','utf8');
 const code=source.slice(source.indexOf('function runCommerceAction('),source.indexOf('\nconst MEAL_LABELS'));
 const calls=[],ctx={send:(...args)=>calls.push(args)};
 vm.runInNewContext(ts.transpileModule(code+'\nglobalThis.run=runCommerceAction',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 for(const action of ['checkout_menu','revise_order'])ctx.run({action,target_id:'checkout',version:3});
 assert.equal(calls[0][0],'查看当前订单的门店菜单');assert.equal(calls[1][0],'更新当前订单预览');assert.equal(calls[1][3].version,3);
});
