const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript'),{parse,compileTemplate}=require('@vue/compiler-sfc');
test('recovery menu action carries the actual store ID and cannot submit an order',async()=>{
 const s=fs.readFileSync('src/components/GlobalCoachWorkspace.vue','utf8');let code=s.slice(s.indexOf('async function runAction('),s.indexOf("    case 'checkout_way':",s.indexOf('async function runAction(')))+'  }\n}';
 const calls=[],ctx={busy:{value:false},send:(...x)=>calls.push(x)};vm.runInNewContext(ts.transpileModule(code+'\nglobalThis.run=runAction',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 await ctx.run({type:'commerce_recovery_menu',label:'刷新原店菜单',target_id:'actual-store'});
 assert.equal(calls[0][3].action,'menu');assert.equal(calls[0][3].target_id,'actual-store');assert.equal(calls[0][2],undefined);
});
test('availability card keeps confirmation near reason and unknown status distinct from closed',()=>{
 const source=fs.readFileSync('src/components/McdCommerceCard.vue','utf8');const {descriptor}=parse(source),compiled=compileTemplate({source:descriptor.template.content,filename:'McdCommerceCard.vue',id:'availability',compilerOptions:{expressionPlugins:['typescript']}});
 assert.deepEqual(compiled.errors,[]);assert(source.includes('确认换店，继续选餐'));assert(source.includes("!result?.commerce_availability && result?.pending_confirmation"));assert(source.includes("store.availability==='unknown'"));
});
