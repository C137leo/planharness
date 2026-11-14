const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const ctx={exports:{}};vm.runInNewContext(ts.transpileModule(fs.readFileSync('src/styles/numbers.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,ctx);
test('calorie display rounds floating arithmetic to exactly two places without changing numeric input',()=>{
 const gap=2092.4-1947;assert.equal(ctx.exports.formatKcal(gap),'145.40');assert.equal(ctx.exports.formatKcal(1947),'1947.00');assert.equal(ctx.exports.formatKcal(0),'0.00');
 assert.equal(gap,2092.4-1947);
});
test('unknown or invalid calories are displayed as unknown rather than zero',()=>{
 for(const value of [null,undefined,'',NaN,Infinity])assert.equal(ctx.exports.formatKcal(value),'—');
});
