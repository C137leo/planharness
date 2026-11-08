'use strict';

const { planProgram, todaySession, nutritionTargets, reviewWeek, safetyGate } = require('./coach.js');

// 换成长自己的数据即可 —— 这里的字段名就是 athlete-state.js 的白名单
const PROFILE = {
  goal: '减脂',
  experience_level: 'beginner',
  training_days_per_week: 3,
  available_time_min: 40,
  training_venue: 'home',
  available_equipment: ['瑜伽垫', '一对哑铃'],
  injury_or_medical_constraints: '无',
  long_term_preferences: ['简化模式', '不做每日称重']
};

const BODY = { sex: 'male', age: 30, height_cm: 175, weight_kg: 80 };
const CURRENT_STATE = { sleep: 7.5, fatigue: 4, stress: 3, pain: '无', available_time_min: 40 };

const WEEK_RECORDS = [
  { date: '2026-10-05', exercise: '深蹲', load_kg: 40, sets: 3, reps: 10, rir: 3 },
  { date: '2026-10-12', exercise: '深蹲', load_kg: 45, sets: 3, reps: 10, rir: 3 },
  { date: '2026-10-05', exercise: '卧推', load_kg: 40, sets: 3, reps: 8, rir: 2 },
  { date: '2026-10-12', exercise: '卧推', load_kg: 40, sets: 3, reps: 6, rir: 2 }
];

function show(title, value) {
  console.log(`\n${'='.repeat(70)}\n${title}\n${'='.repeat(70)}`);
  console.log(JSON.stringify(value, null, 2));
}

console.log('skill root:', require('./coach.js').SKILL_ROOT);

// 0) 安全门先行：把「胸痛」换成"无"才放行
show('0. 安全门（无红旗）', safetyGate({ ...PROFILE, instruction: '帮我做训练计划' }));
show('0b. 安全门（原话含"胸痛"+当下语境 → stop）', safetyGate({ instruction: '我以前有过胸痛，现在想开始练' }));

// 1) 训练计划
const plan = planProgram(PROFILE, { instruction: '帮我制定一个减脂训练计划' });
show('1. 训练计划（structure+sessions+prescription）', {
  status: plan.status,
  structure: plan.program.structure,
  days: plan.program.days_per_week,
  minutes: plan.program.session_minutes,
  prescription: plan.program.prescription,
  cycle_metrics: plan.program.cycle_metrics,
  equipment_tier: plan.program.equipment_tier,
  sessions: plan.program.sessions
});

// 2) 今日训练（缺执行上下文 → assume）
show('2a. 今日训练（信息完整）', todaySession({ profile: PROFILE, program: null, currentState: CURRENT_STATE }));
show('2b. 今日训练（只睡 5 小时 → 只改强度一个变量）', todaySession({ profile: PROFILE, currentState: { ...CURRENT_STATE, sleep: 5 } }));
show('2c. 今日训练（膝盖不适 → regress 到动作变式）', todaySession({ profile: PROFILE, currentState: { ...CURRENT_STATE, pain: '膝盖有点不适' } }));

// 3) 营养
show('3. 营养目标（简化模式）', nutritionTargets({ profile: PROFILE, body: BODY, mode: 'simplified' }));

// 4) 周复盘
show('4. 周复盘（事实→判断→决策）', reviewWeek({
  records: WEEK_RECORDS,
  completionRate: 0.5,
  dateStart: '2026-10-05',
  dateEnd: '2026-10-18',
  currentProgram: {
    version: 'v1',
    session_slots: [{ id: 's1', focus: 'full_body' }],
    session_budget: { declared_minutes: 40 }
  }
}));
