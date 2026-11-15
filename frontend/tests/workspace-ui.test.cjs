const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript'),vue=require('vue'),{parse,compileTemplate}=require('@vue/compiler-sfc');
const read=name=>fs.readFileSync('src/pages/'+name+'/index.vue','utf8');
test('all adjusted page templates compile without invalid nesting',()=>{
 for(const name of ['profile','progress','workout','plan','today']){const {descriptor,errors}=parse(read(name));assert.deepEqual(errors,[]);const result=compileTemplate({source:descriptor.template.content,filename:name+'.vue',id:name,compilerOptions:{expressionPlugins:['typescript']}});assert.deepEqual(result.errors,[],name);}
});
test('zero calories and completion render zero; percent uses absolute 100-percent scale',()=>{
 const source=read('progress').split('<script setup lang="ts">')[1].split('</script>')[0].replace(/^import [\s\S]*? from [^\n]+\n/gm,'');
 const context={...vue,onShow:()=>{},todayStr:()=>'',uni:{},api:{}};
 vm.runInNewContext(ts.transpileModule(source+'\nglobalThis.testView={stats,checkins,weights,weightDelta,dayWidth,trendHeight}',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,context);
 const v=context.testView;v.stats.value={days:[{kcal:0},{kcal:100}]};assert.equal(v.dayWidth(0),'0%');assert.equal(v.dayWidth(50),'50%');assert.equal(v.trendHeight(0),'0%');assert.equal(v.trendHeight(70),'70%');assert.equal(v.trendHeight(120),'100%');
 v.checkins.value=Array.from({length:20},(_,n)=>({weight_kg:75+n}));assert.equal(v.weights.value.length,20);assert.equal(v.weightDelta.value,-7,'retains original latest-eight-weight delta while displaying all available weights');
});
const js=ts.transpileModule(fs.readFileSync('src/styles/scrollChain.ts','utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2020,module:ts.ModuleKind.CommonJS}}).outputText;
const ctx={exports:{}};vm.runInNewContext(js,ctx);const forward=ctx.exports.forwardBoundaryWheel;
function scroller(top=199.4){
 const page={scrollTop:200,scrollHeight:2000,clientHeight:800},body={},doc={scrollingElement:page,body,defaultView:{getComputedStyle:e=>({overflowY:e.overflow||'visible',overscrollBehaviorY:e.isolation||'auto'})}};
 const parent={scrollTop:0,scrollHeight:100,clientHeight:100,parentElement:body},element={scrollTop:top,scrollHeight:500,clientHeight:300,parentElement:parent,ownerDocument:doc};
 let prevented=false;const event={deltaX:0,deltaY:120,deltaMode:0,preventDefault:()=>{prevented=true}};
 return {element,event,page,parent,prevented:()=>prevented};
}
test('fractional bottom and top transfer wheel to page, middle stays native',()=>{
 const c=scroller();assert(forward(c.element,c.event));assert(c.prevented());assert.equal(c.page.scrollTop,320);
 c.element.scrollTop=.4;c.event.deltaY=-80;assert(forward(c.element,c.event));assert.equal(c.page.scrollTop,240);
 c.element.scrollTop=100;assert.equal(forward(c.element,c.event),false);assert.equal(c.page.scrollTop,240);
});
test('nearest scrollable ancestor receives wheel and modal isolation is respected',()=>{
 const c=scroller();c.parent.overflow='auto';c.parent.scrollHeight=600;c.parent.clientHeight=200;assert(forward(c.element,c.event));assert.equal(c.parent.scrollTop,120);assert.equal(c.page.scrollTop,200);
 c.parent.scrollTop=400;c.parent.isolation='contain';assert.equal(forward(c.element,c.event),false);assert.equal(c.page.scrollTop,200);
});
test('zoom/horizontal/consumed gestures and page end are not intercepted',()=>{
 for(const values of [{ctrlKey:true},{metaKey:true},{deltaX:200},{defaultPrevented:true}]){const c=scroller();Object.assign(c.event,values);assert.equal(forward(c.element,c.event),false);assert.equal(c.prevented(),false);}
 const c=scroller();c.page.scrollTop=1200;assert.equal(forward(c.element,c.event),false);assert.equal(c.prevented(),false);
});
test('line and page wheel units scale without altering the child progress',()=>{
 const c=scroller();c.event.deltaMode=1;c.event.deltaY=2;assert(forward(c.element,c.event));assert.equal(c.page.scrollTop,232);assert.equal(c.element.scrollTop,199.4);
 c.event.deltaMode=2;c.event.deltaY=1;assert(forward(c.element,c.event));assert.equal(c.page.scrollTop,532);
});

test('non-tab workout page does not call hideTabBar; tab pages consume native failure',()=>{
 const source=fs.readFileSync('src/composables/desktopChrome.ts','utf8');
 let route='pages/workout/index';const calls=[];
 const context={exports:{},window:{innerWidth:1440},getCurrentPages:()=>[{route}],uni:{hideTabBar:o=>{calls.push('hide');assert.equal(typeof o.fail,'function');o.fail({errMsg:'not TabBar page'});},showTabBar:()=>calls.push('show')}};
 vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2020,module:ts.ModuleKind.CommonJS}}).outputText,context);
 context.exports.syncDesktopChrome();assert.deepEqual(calls,[]);
 route='pages/today/index';context.exports.syncDesktopChrome();assert.deepEqual(calls,['hide']);
});
