const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const cache={};function load(file){if(cache[file])return cache[file];const ctx={exports:{},require:name=>name.endsWith('.json')?{default:JSON.parse(fs.readFileSync(require('node:path').resolve(require('node:path').dirname(file),name),'utf8'))}:load(name==='./copy'?'src/styles/copy.ts':name)};vm.runInNewContext(ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,ctx);return cache[file]=ctx.exports;}
const {trainingActions,weeklyMeals,datedView}=load('src/styles/planView.ts');
test('mixed persisted actions separate meals from training even when slot or name resembles exercise',()=>{
 const actions=[{id:'a',day_index:1,action_type:'exercise',slot:'strength',action_ref:'row'},{id:'b',day_index:2,action_type:'meal',slot:'strength',action_ref:'row'},{id:'c',day_index:6,action_type:'meal',slot:'meal_dinner',action_ref:'tofu_veggies'}];
 assert.equal(trainingActions(actions).length,1);assert.equal(trainingActions(actions)[0].id,'a');
 const groups=weeklyMeals({actions,meals:[]});assert.equal(groups.length,2);assert.equal(groups[1].legacy[0].id,'c');
});
test('rich meal details supersede legacy slots with canonical meal_ prefix without hiding other meals',()=>{
 const groups=weeklyMeals({actions:[{id:'old',day_index:1,action_type:'meal',slot:'meal_lunch'},{id:'dinner',day_index:1,action_type:'meal',slot:'meal_dinner'}],meals:[{day_index:1,slot:'lunch',title:'new'}]});
 assert.equal(groups[0].meals[0].title,'new');assert.equal(groups[0].legacy.length,1);assert.equal(groups[0].legacy[0].id,'dinner');
});
test('date count duration actions and meals share the exact effective day including empty overrides',()=>{
 const override={actions:[{slot:'warmup'},{slot:'strength'},{slot:'strength'},{slot:'strength'}],total_minutes:41,meals:[],adjustment:{adjustment_id:'new'}};
 const view=datedView(override);assert.equal(view.countLabel,'4 项');assert.equal(view.totalMinutes,41);assert.equal(view.actions,override.actions);assert.equal(view.meals,override.meals);assert.equal(view.adjustment,override.adjustment);
 assert.equal(datedView({actions:[],meals:[],total_minutes:0}).countLabel,'休');
});
