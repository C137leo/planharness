const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs'), vm = require('node:vm'), ts = require('typescript')
function load(file, extra = {}) {
  const ctx = { exports: {}, ...extra }
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText, ctx)
  return ctx.exports
}
const dates = load('src/styles/dates.ts')
const bounds = load('src/styles/workspaceHeight.ts')
test('native touch uses real window height and removes resize listener', () => {
  const mounts=[],unmounts=[],events=new Map()
  const api=load('src/composables/workspaceResize.ts',{
    uni:{getWindowInfo:()=>({windowHeight:320}),getStorageSync:()=>9999,setStorageSync(){},onWindowResize:f=>events.set('resize',f),offWindowResize:f=>{assert.equal(events.get('resize'),f);events.delete('resize')}},
    require:name=>name==='vue'?{ref:value=>({value}),computed:f=>({get value(){return f()}}),onMounted:f=>mounts.push(f),onUnmounted:f=>unmounts.push(f)}:bounds,
  })
  const panel=api.useWorkspaceResize(()=>'modal');mounts.forEach(f=>f())
  assert.equal(panel.height.value,296)
  panel.touchStart({touches:[{clientY:200}]});panel.touchMove({touches:[{clientY:0}]});assert.equal(panel.height.value,296)
  events.get('resize')({size:{windowHeight:260}});assert.equal(panel.height.value,236)
  unmounts.forEach(f=>f());assert.equal(events.size,0)
})
test('7 local dates start today, retain weekdays and roll across year/leap boundaries', () => {
  for (const [start, end] of [['2026-11-17', '2026-11-23'], ['2026-12-29', '2027-01-04'], ['2028-02-27', '2028-03-04']]) {
    const days = dates.upcomingWeek(start)
    assert.equal(days.length, 7); assert.equal(days[0].date, start); assert.equal(days[6].date, end)
    assert.equal(new Set(days.map(d => d.weekday)).size, 7)
    days.forEach((day, i) => { assert.equal(day.isToday, i === 0); assert.ok(day.date >= start) })
  }
  assert.equal(dates.upcomingWeek('2026-11-17')[0].week, '周二')
})
test('height clamps invalid cache, short screens and out-of-range values', () => {
  assert.equal(bounds.clampWorkspaceHeight('999', 800), 600)
  assert.equal(bounds.clampWorkspaceHeight(NaN, 800), 600)
  assert.equal(bounds.clampWorkspaceHeight(-1, 800), 360)
  assert.equal(bounds.clampWorkspaceHeight(9999, 800), 776)
  assert.equal(bounds.clampWorkspaceHeight(600, 300), 276)
})
test('drag persists, keyboard clamps, viewport resize reclamps and unmount removes listeners', () => {
  const listeners = new Map(), store = new Map(), mounts = [], unmounts = []
  const window = { innerHeight: 900, addEventListener: (k,v) => listeners.set(k,v), removeEventListener: k => listeners.delete(k) }
  const api = load('src/composables/workspaceResize.ts', {
    window, uni: { getStorageSync: k => store.get(k), setStorageSync: (k,v) => store.set(k,v) },
    require: name => name === 'vue' ? { ref: value => ({ value }), computed: f => ({ get value() { return f() } }), onMounted: f => mounts.push(f), onUnmounted: f => unmounts.push(f) } : bounds,
  })
  const panel = api.useWorkspaceResize(() => 'inline'); mounts.forEach(f => f())
  panel.start({ button: 0, pointerId: 1, clientY: 100, preventDefault() {} })
  listeners.get('pointermove')({ pointerId: 1, clientY: 200, preventDefault() {} })
  assert.equal(panel.height.value, 700)
  listeners.get('pointerup')(); assert.equal(store.get('planharness_coach_height:inline'), 700)
  panel.keyboard({ key: 'End', preventDefault() {} }); assert.equal(panel.height.value, 876)
  window.innerHeight = 400; listeners.get('resize')(); assert.equal(panel.height.value, 376)
  unmounts.forEach(f => f()); assert.equal(listeners.size, 0)
})
