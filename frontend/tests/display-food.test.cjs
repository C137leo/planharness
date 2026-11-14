const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');const ts=require('typescript');
function load(path){const context={exports:{}};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context);return context.exports;}
const {actionLabel}=load('src/styles/copy.ts');const {displayMealItems}=load('src/styles/mealItems.ts');
test('backend Chinese name wins and old slug falls back to Chinese',()=>{
 assert.equal(actionLabel({action_ref:'jumping-jack',name:'开合跳'}),'开合跳');
 assert.equal(actionLabel({action_ref:'jumping-jack',name:'Jumping Jack'}),'开合跳');
 assert.equal(actionLabel({action_ref:'custom-id',name:'自定义中文动作'}),'自定义中文动作');
 assert.equal(actionLabel({action_ref:'bicep-curl',name:'哑铃弯举'}),'哑铃弯举');
});
test('historical duplicate rows display 250g without mutating business IDs',()=>{
 const items=[{id:'a',name:'鸡胸肉',grams:150},{id:'b',ingredient_id:'catalog',name:'鸡胸肉',grams:100},{id:'c',name:'米饭',grams:200}];
 const before=JSON.stringify(items);const result=displayMealItems(items);
 assert.equal(result.length,2);assert.equal(result[0].grams,250);assert.equal(JSON.stringify(items),before);
});
test('different cooking methods or known identities remain separate',()=>{
 assert.equal(displayMealItems([{name:'鸡胸肉',grams:100,preparation:'水煮'},{name:'鸡胸肉',grams:100,preparation:'香煎'}]).length,2);
 assert.equal(displayMealItems([{name:'鸡胸肉',grams:100,ingredient_id:'a'},{name:'鸡胸肉',grams:100,ingredient_id:'b'}]).length,2);
});
test('an untagged legacy row cannot bridge two different catalogue identities',()=>{
 const result=displayMealItems([{name:'鸡胸肉',grams:100},{name:'鸡胸肉',grams:50,ingredient_id:'a'},{name:'鸡胸肉',grams:30,ingredient_id:'b'}]);
 assert.equal(result.length,2);assert.equal(result[0].grams,150);assert.equal(result[1].grams,30);
});
