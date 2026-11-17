const {test}=require('node:test'), assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const guidance=JSON.parse(fs.readFileSync('../backend/app/domain/exercise_guidance.json','utf8'));
const calls=[];
let opened=0;
const timers=new Map();let timerId=0;
const ctx={exports:{},setTimeout:(f,ms)=>{assert.equal(ms,1000);timers.set(++timerId,f);return timerId},clearTimeout:id=>timers.delete(id),require:path=>({default:JSON.parse(fs.readFileSync(require('node:path').resolve('src/styles',path),'utf8'))}),uni:{showModal:opts=>calls.push(opts),showToast:opts=>calls.push(opts)},open:()=>{opened++;return {}}};
vm.runInNewContext(ts.transpileModule(fs.readFileSync('src/styles/exerciseDb.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,ctx);
test('wall sit has local guidance without a false upstream squat match',()=>{
 assert.equal(ctx.exports.hasExerciseGuidance('靠墙静蹲'),true);
 assert.equal(ctx.exports.exerciseSearchTerm('靠墙静蹲'), '');
 ctx.exports.openExerciseGuidance('靠墙静蹲');assert.match(calls.at(-1).content,/背部贴墙/);assert.equal(calls.at(-1).showCancel,false);
});
test('back extension has correct guidance and upstream search name',()=>{
 assert.equal(ctx.exports.exerciseSearchTerm('山羊挺身'),'Hyperextensions (Back Extensions)');
 ctx.exports.openExerciseGuidance('山羊挺身');assert.match(calls.at(-1).content,/不要超过直线/);
 assert.equal(ctx.exports.exerciseSearchTerm('俯卧撑'),'Pushups');
 assert.equal(ctx.exports.exerciseSearchTerm('臀桥'),'Butt Lift (Bridge)');
});
test('clipboard confirmation follows actual completion',()=>{
 let pending;
 ctx.uni.setClipboardData=opts=>{pending=opts};
 ctx.exports.openExerciseDbTerm('Plank');
 assert.doesNotMatch(calls.at(-1).title,/已复制/);
 pending.success();assert.match(calls.at(-1).title,/已经复制动作名称，请在新页面搜索框直接粘贴查看/);
 ctx.exports.openExerciseDbTerm('Plank');pending.fail();assert.match(calls.at(-1).title,/复制失败/);
});
test('opening local guidance also copies the action name and confirms only on success',()=>{
 let pending;
 ctx.uni.setClipboardData=opts=>{pending=opts};
 ctx.exports.openExerciseGuidance('靠墙静蹲');
 assert.equal(pending.data,'靠墙静蹲');
 pending.success();assert.match(calls.at(-1).title,/已经复制动作名称，请在新页面搜索框直接粘贴查看/);
});
test('catalog exact matching never maps unknown variants to a generic exercise',()=>{
 assert.equal(ctx.exports.exerciseSearchTerm('弹力带死虫式'),'');
 assert.equal(ctx.exports.hasExerciseGuidance('弹力带死虫式'),false);
 assert.equal(ctx.exports.hasExerciseGuidance('Plank'),true);
 ctx.exports.openExerciseGuidance('Plank');assert.match(calls.at(-1).content,/前臂/);
});
test('automatic navigation waits for clipboard completion then a readable delay without a modal',()=>{
 let clipboard;ctx.uni.setClipboardData=opts=>{clipboard=opts};
 const before=opened,modalCount=calls.filter(x=>x.confirmText).length;
 ctx.exports.openExerciseDbTerm('Plank');assert.equal(opened,before);
 assert.equal(clipboard.data,'Plank');clipboard.success();
 assert.match(calls.at(-1).title,/已经复制动作名称/);
 assert.equal(calls.filter(x=>x.confirmText).length,modalCount);
 assert.equal(opened,before);timers.get(timerId)();assert.equal(opened,before+1);
});
test('foreground notice is attached above the sheet and native clipboard failure is not success',()=>{
 const nodes=[];
 ctx.document={createElement:()=>({style:{},setAttribute(){},select(){},setSelectionRange(){},remove(){}}),
  body:{appendChild:n=>nodes.push(n),removeChild:n=>nodes.splice(nodes.indexOf(n),1)},execCommand:()=>true};
 const before=opened;ctx.exports.openExerciseDbTerm('Dead Bug');
 const notice=nodes.at(-1);assert.match(notice.style.cssText,/z-index:2147483647/);
 assert.match(notice.textContent,/已经复制动作名称/);assert.equal(opened,before);
 timers.get(timerId)();assert.equal(opened,before+1);
 delete ctx.document;
});
