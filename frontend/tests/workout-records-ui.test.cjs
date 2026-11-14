const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
test('finish form leaves actual duration unknown instead of inventing it from planned or elapsed minutes',()=>{
 const source=fs.readFileSync('src/pages/workout/index.vue','utf8');const code=source.slice(source.indexOf('function openFinish()'),source.indexOf('\nasync function submitFinish()'));
 const ctx={session:{value:{planned_minutes:90,elapsed_minutes:25}},finishForm:{},finishOpen:{value:false}};
 vm.runInNewContext(ts.transpileModule(code+'\nglobalThis.open=openFinish',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);ctx.open();
 assert.equal(ctx.finishForm.actual_minutes,'');assert.equal(ctx.finishOpen.value,true);
});
test('a persisted training fact response attaches to actual records despite also carrying today plan',()=>{
 const source=fs.readFileSync('src/components/GlobalCoachWorkspace.vue','utf8');const code=source.slice(source.indexOf('function attachmentFor('),source.indexOf('\nfunction showAttachment('));
 const ctx={};vm.runInNewContext(ts.transpileModule(code+'\nglobalThis.attach=attachmentFor',{compilerOptions:{target:ts.ScriptTarget.ES2020}}).outputText,ctx);
 assert.equal(ctx.attach({workout_records:{session:{status:'finished'}},day:{summary:'原计划'}}).kind,'训练记录');
});
