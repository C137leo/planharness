const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript');
const source=fs.readFileSync('src/styles/thumbs.ts','utf8');
const context={exports:{},require:name=>({default:JSON.parse(fs.readFileSync(path.resolve('src/styles',name),'utf8'))})};
vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context);
const {thumbOf,mealThumb}=context.exports;
test('legacy warmup and cooldown have their own illustrations',()=>{
 for(const ref of ['dynamic_warmup','full_body_stretch']){
  assert.equal(thumbOf({action_ref:ref}),'/static/ex/generated-v2/'+ref+'.jpg');
  assert(fs.existsSync('src'+thumbOf({action_ref:ref})));
 }
});
test('unknown actions never inherit an unrelated squat or body-part image',()=>{
 assert.equal(thumbOf({action_ref:'unknown-custom-action',meta:{primary_muscle:'quads'}}),'');
});
test('wall sit and back extension use distinct exact action images, never the squat/bridge fallback',()=>{
 assert.equal(thumbOf({action_ref:'wall-sit'}),'/static/ex/wall-sit-v2.jpg');
 assert.equal(thumbOf({action_ref:'back-extension'}),'/static/ex/back-extension-v2.jpg');
 assert.notEqual(thumbOf({action_ref:'wall-sit'}),thumbOf({action_ref:'bodyweight-squat'}));
});
test('meal slots and persisted meal_ slots map to real category illustrations',()=>{
 for(const slot of ['breakfast','lunch','dinner']){
  assert.equal(mealThumb(slot),mealThumb('meal_'+slot));
  assert(fs.existsSync('src'+mealThumb(slot)));
 }
});
test('every manifest entry resolves to an existing image',()=>{
 const manifest=JSON.parse(fs.readFileSync('src/styles/exerciseImages.json','utf8'));
 for(const [slug,image] of Object.entries(manifest)){
  assert(fs.existsSync('src'+image),slug+' image missing');
  assert.equal(thumbOf({action_ref:slug}),image);
 }
});
