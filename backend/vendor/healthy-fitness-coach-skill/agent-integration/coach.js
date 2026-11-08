'use strict';

/**
 * 把自己的 agent 接到 healthy-fitness-coach skill 上的门面层。
 *
 * 设计原则（与 skill 自身一致）：
 *  1. 确定性判断走 skill 的纯函数，LLM 只负责"选动作 + 写人话"。
 *  2. 安全门是第一优先级，任何函数入口都先过 safetyGate。
 *  3. 未知就是未知：不替用户编造 profile 字段。
 *  4. 营养部分是「本项目新增层」，不来自 skill（skill 只有 references/nutrition.md 的规则文本，
 *     没有营养引擎）。参数出处见 nutrition 函数注释。
 */

const path = require('node:path');
const fs = require('node:fs');

const SKILL_ROOT = path.join(__dirname, '..', 'healthy-fitness-coach');
const ref = (name) => require(path.join(SKILL_ROOT, 'references', name));

const { buildWorkflowDecision } = ref('training-orchestrator.js');
const { compileProgramRules, renderCurrentProgram } = ref('program-rules.js');
const { buildTodayReadinessGuidance } = ref('training-guidance.js');
const { inspectSafety } = ref('safety-context.js');
const {
  deriveReviewFacts,
  buildReviewJudgments,
  selectProgramChanges,
  buildDecisionLogEntry
} = ref('review-decision-engine.js');
const { matchExercise } = ref('workout-guide.js');
const { normalizeAthleteProfile } = ref('athlete-state.js');

// ---------------------------------------------------------------------------
// 动作映射：skill 的 movement_slots 只给「动作模式」，具体动作由这里补全。
// 每个动作都会用 workout-guide 的 302 动作目录校验一遍，目录里没有的直接丢弃，
// 保证产出的图卡链接不会 404。
// ---------------------------------------------------------------------------
const MOVEMENT_LIBRARY = {
  knee_dominant: {
    barbell: ['杠铃深蹲', '腿举'],
    dumbbell: ['高脚杯深蹲'],
    bodyweight: ['徒手深蹲']
  },
  hip_dominant: {
    barbell: ['罗马尼亚硬拉', '杠铃臀推'],
    dumbbell: ['臀桥'],
    bodyweight: ['臀桥']
  },
  horizontal_push: {
    barbell: ['杠铃卧推'],
    dumbbell: ['哑铃卧推'],
    bodyweight: ['俯卧撑']
  },
  horizontal_pull: {
    barbell: ['杠铃划船'],
    dumbbell: ['单臂哑铃划船'],
    bodyweight: ['引体向上']
  },
  vertical_push_or_pull: {
    barbell: ['杠铃推举'],
    dumbbell: ['哑铃侧平举'],
    bodyweight: ['引体向上']
  },
  single_leg: {
    barbell: ['保加利亚分腿蹲'],
    dumbbell: ['行走弓步'],
    bodyweight: ['臀桥']
  },
  core_or_carry: {
    barbell: ['平板支撑'],
    dumbbell: ['反向卷腹'],
    bodyweight: ['平板支撑']
  }
};

const EQUIPMENT_TIER_PATTERNS = [
  { tier: 'barbell', pattern: /barbell|杠铃|rack|架|smith|史密斯/iu },
  { tier: 'dumbbell', pattern: /dumbbell|哑铃|kettlebell|壶铃|band|弹力带/iu },
  { tier: 'bodyweight', pattern: /bodyweight|自重|徒手|垫|mat|pull.?up|单杠/iu }
];

function equipmentTier(availableEquipment = []) {
  const text = availableEquipment.join(' ');
  for (const { tier, pattern } of EQUIPMENT_TIER_PATTERNS) if (pattern.test(text)) return tier;
  return 'bodyweight';
}

function pickExercise(movementMode, tier) {
  const table = MOVEMENT_LIBRARY[movementMode];
  if (!table) return null;
  const order = [tier, 'dumbbell', 'bodyweight']; // 依次降级，永远有可用项
  for (const candidateTier of order) {
    for (const name of table[candidateTier] || []) {
      const matched = matchExercise(name);
      if (matched.status === 'match') {
        return {
          movement_mode: movementMode,
          name_zh: name,
          name_en: matched.exercise.name,
          slug: matched.exercise.slug,
          equipment: matched.exercise.equipment,
          primary_muscle: matched.exercise.muscles.primary,
          degraded_from: candidateTier === tier ? null : tier,
          guide_frames: matched.exercise.frames.map((frame) => frame.displayPath)
        };
      }
    }
  }
  return null; // 目录里查不到就明说，不编
}

// ---------------------------------------------------------------------------
// 1. 安全门 —— 所有入口都要先过
// ---------------------------------------------------------------------------
function safetyGate(input = {}) {
  const verdict = inspectSafety(input);
  return {
    ...verdict,
    blocked: verdict.status !== 'clear',
    // clarify = 历史症状未确认当下，不是直接停止，但必须先问
    required_action:
      verdict.status === 'stop' ? '停止自动训练处方，建议线下评估（这是 skill 的硬边界）'
        : verdict.status === 'clarify' ? '先确认历史症状是否已评估、现在是否仍存在，再安排训练'
          : null
  };
}

// ---------------------------------------------------------------------------
// 2. 生成训练计划 —— 结构由确定性引擎给，动作与文案由 LLM 补
// ---------------------------------------------------------------------------
function planProgram(profile = {}, { instruction = '', outputDirectory = null } = {}) {
  // 注意：compileProgramRules 只做"画像里写明的风险"检查（classifySafetyRisk），
  // 不看用户原话。红旗可能只出现在 instruction 里（"我昨天胸痛还能练吗"），
  // 所以这里必须显式把 instruction 一起送进安全门，否则会绕过 skill 的硬边界。
  const safety = safetyGate({ ...profile, instruction });
  if (safety.status === 'stop') {
    return { status: 'blocked', safety, next: safety.required_action };
  }

  const decision = buildWorkflowDecision({ instruction, profile });
  const plan = compileProgramRules(profile);

  if (plan.status === 'blocked') {
    return { status: 'blocked', safety: plan.safety_risk, next: plan.next_step };
  }
  if (plan.status === 'needs_input') {
    // 只问会改变方案结构的字段，不做长问卷
    return {
      status: 'needs_input',
      missing_fields: plan.missing_fields,
      ask_user: plan.missing_fields.map(describeMissingField),
      decision
    };
  }

  const normalized = normalizeAthleteProfile(profile);
  const tier = equipmentTier(normalized.available_equipment || []);
  const sessions = plan.session_slots.map((slot, index) => ({
    day: index + 1,
    session_id: slot.id,
    focus: slot.focus,
    minimum_version: slot.minimum_version,
    exercises: slot.movement_slots
      .map((movement) => pickExercise(movement.movement_mode, tier))
      .filter(Boolean)
  }));

  const targetRir = plan.intensity_rules.resistance.rir_target;
  const workSets = plan.session_budget.sets_per_slot;

  const assets = {};
  const templatePath = path.join(SKILL_ROOT, 'assets', 'current-program-template.md');
  if (fs.existsSync(templatePath)) {
    assets.CURRENT_PROGRAM = renderCurrentProgram(plan, fs.readFileSync(templatePath, 'utf8'));
  }

  const result = {
    status: 'ready',
    decision,
    safety,
    program: {
      goal: normalized.goal,
      experience_level: normalized.experience_level,
      days_per_week: normalized.training_days_per_week,
      session_minutes: plan.session_budget.declared_minutes,
      structure: plan.structure.kind,
      structure_reason_codes: plan.reason_codes,
      sessions,
      prescription: {
        sets_per_movement: `${workSets.min}-${workSets.max}`,
        rir_target: `${targetRir.min}-${targetRir.max}`,
        rest_seconds: plan.session_budget.rest_seconds || null,
        progression_rule: plan.progression_rule,
        regression_rule: plan.regression_rule,
        failure_policy: plan.intensity_rules.resistance.failure_policy
      },
      cardio: plan.intensity_rules.cardio,
      cycle_metrics: plan.cycle_metrics,
      equipment_tier: tier
    },
    assets,
    // 这几份是 LLM 必须读的知识，不是代码
    read_next: ['references/programming.md', 'references/exercise-coaching.md', 'references/nutrition.md']
  };

  if (outputDirectory) result.written = writeAssets(outputDirectory, result);
  return result;
}

function describeMissingField(field) {
  return {
    goal: '你的主要目标是什么（减脂 / 增肌 / 力量 / 心肺 / 综合健康）？',
    training_days_per_week: '每周能训练几天？',
    available_time_min: '每次能拿出多少分钟？',
    available_equipment: '有哪些器械或场地（健身房 / 哑铃 / 只有垫子）？',
    injury_or_medical_constraints: '有没有需要避开的伤病或医疗限制？（没有就说"无"）',
    experience_level: '训练经验（新手 / 进阶）？'
  }[field] || field;
}

// ---------------------------------------------------------------------------
// 3. 今日训练 —— 只在无红旗时调整，且只改 1 个变量
// ---------------------------------------------------------------------------
function todaySession({ profile = {}, program = null, currentState = {}, sessionId, instruction = '' } = {}) {
  const compiled = program && program.session_slots ? program : compileProgramRules(profile);
  if (compiled.status === 'blocked') return { status: 'blocked', next: compiled.next_step };

  const readiness = buildTodayReadinessGuidance({
    currentState,
    program: compiled,
    session_id: sessionId,
    instruction
  });

  const tier = equipmentTier((normalizeAthleteProfile(profile).available_equipment) || []);
  const slot = compiled.session_slots.find((item) => item.id === sessionId) || compiled.session_slots[0];

  return {
    status: readiness.status,
    facts: readiness.facts,
    reason_codes: readiness.reason_codes,
    adjustment: readiness.adjustment,           // primary_variables_changed 恒 ≤1
    minimum_task: readiness.minimum_task,
    stop_conditions: readiness.stop_conditions,
    log_after_training: readiness.post_training_record_fields,
    session: slot ? {
      session_id: slot.id,
      focus: slot.focus,
      exercises: slot.movement_slots.map((m) => pickExercise(m.movement_mode, tier)).filter(Boolean)
    } : null,
    read_next: ['references/exercise-coaching.md', 'references/recovery-pain.md']
  };
}

// ---------------------------------------------------------------------------
// 4. 营养 —— 本项目新增层（skill 只有 nutrition.md 的规则，无引擎）
//    参数出处：references/nutrition.md
//      · 蛋白质 1.4～2.0 g/kg/日（健康运动人群一般参考区间）
//      · 减脂：温和缺口，结合 2～4 周体重趋势判断，不同时大改摄入与活动
//      · 增肌：小幅盈余或维持附近起步
//      · 三种执行模式：精确 / 简化 / 习惯
//    BMR 用 Mifflin-St Jeor（通用公式，非 skill 内容，属于常见的营养估算方法）。
// ---------------------------------------------------------------------------
function nutritionTargets({ profile = {}, body = {}, mode = 'simplified', instruction = '' } = {}) {
  // 红旗是全局硬边界，营养建议同样不越过
  const safety = safetyGate({ ...profile, instruction });
  if (safety.status === 'stop') {
    return { status: 'blocked', safety, next: safety.required_action };
  }

  const missing = ['sex', 'age', 'height_cm', 'weight_kg'].filter((k) => body[k] === undefined);
  if (missing.length) {
    return {
      status: 'needs_input',
      missing_fields: missing,
      ask_user: {
        sex: '生理性别（影响 BMR 常数）',
        age: '年龄',
        height_cm: '身高（cm）',
        weight_kg: '当前体重（kg）'
      },
      modes: describeModes()
    };
  }

  const { sex, age, height_cm: height, weight_kg: weight } = body;
  const bmr = 10 * weight + 6.25 * height - 5 * age + (String(sex).toLowerCase() === 'female' ? -161 : 5);
  const activity = body.activity_factor || activityFactorFrom(profile.training_days_per_week);
  const tdee = bmr * activity;
  const goal = String(profile.goal || '').toLowerCase();

  let kcal = tdee;
  let adjustment = 0;
  if (/减脂|减重|fat|cut|weight\s*loss/iu.test(goal)) adjustment = -Math.min(0.20, 0.15) * tdee; // 温和缺口，不超 20%
  else if (/增肌|增重|muscle|bulk|gain/iu.test(goal)) adjustment = 0.08 * tdee;                 // 小幅盈余

  kcal = Math.round((tdee + adjustment) / 10) * 10;

  const protein = { low_g: Math.round(weight * 1.4), high_g: Math.round(weight * 2.0) };
  const fat = { low_g: Math.round(weight * 0.6), high_g: Math.round(weight * 1.0) };
  const carbsLow = Math.round((kcal - protein.high_g * 4 - fat.high_g * 9) / 4);
  const carbsHigh = Math.round((kcal - protein.low_g * 4 - fat.low_g * 9) / 4);

  const bmi = Number((weight / Math.pow(height / 100, 2)).toFixed(1));
  const cautions = [];
  if (/减脂|减脂|减重|cut/iu.test(goal) && bmi < 18.5) cautions.push('BMI 偏低仍要求减脂：不建议制造能量缺口，优先力量训练与饮食质量。');
  if (kcal - tdee <= -1000) cautions.push('缺口过大：已按温和缺口上限收敛，不支持极端减脂。');
  if (mode === 'habit') cautions.push('习惯模式：不给热量数字，只给一个待改进行为。');

  return {
    status: 'ready',
    mode,
    mode_description: describeModes()[mode],
    body: { bmi, weight_kg: weight },
    bmr: Math.round(bmr),
    tdee: Math.round(tdee),
    target_kcal: mode === 'habit' ? null : kcal,
    delta_kcal: mode === 'habit' ? null : Math.round(adjustment),
    macros: {
      protein_g: protein,
      fat_g: fat,
      carb_g: { low: Math.max(0, carbsLow), high: Math.max(0, carbsHigh) }
    },
    scale_check: '用 2～4 周体重趋势（同条件称重）判断，不看单日数字；同时看围度、力量、精力、饥饿、执行率。',
    cautions,
    boundaries: [
      '肾脏/代谢/消化疾病、复杂用药、妊娠产后、疑似进食障碍：不套用以上范围，转专业评估。',
      '不提供补剂处方、药物或 PED 指导。',
      '出现食物恐惧、暴食催吐、体重焦虑：停止强化赤字与每日称重。'
    ],
    read_next: ['references/nutrition.md', 'references/multidimensional-analysis.md']
  };
}

function activityFactorFrom(days) {
  const n = Number(days);
  if (!Number.isFinite(n)) return 1.4;
  if (n <= 0) return 1.3;
  if (n <= 2) return 1.4;
  if (n <= 4) return 1.5;
  if (n <= 6) return 1.6;
  return 1.7;
}

function describeModes() {
  return {
    precise: '精确模式：称量记录，用总能量 + 蛋白质 + 体重趋势调整。适合愿意记录且不加重饮食焦虑的人。',
    simplified: '简化模式：手掌/餐盘份量法，不强制算热量。适合大多数人起步。',
    habit: '习惯模式：每轮只改一个行为（含糖饮料/蛋白质/蔬菜/零食环境/规律进餐）。'
  };
}

// ---------------------------------------------------------------------------
// 5. 周复盘 —— 只改 1～2 个变量，写清回退与验证条件
// ---------------------------------------------------------------------------
function reviewWeek({ records = [], currentProgram = {}, completionRate = null, dateStart, dateEnd } = {}) {
  const facts = deriveReviewFacts({
    records,
    date_start: dateStart,
    date_end: dateEnd,
    completion_rate: completionRate === null ? undefined : completionRate
  });
  const judgments = buildReviewJudgments(facts);
  const decision = selectProgramChanges(judgments, currentProgram);
  const log = buildDecisionLogEntry({
    facts,
    judgments,
    current_program: currentProgram,
    date: dateEnd
  });

  return {
    status: decision.status,                  // 'micro_adjust' | 'continue'
    data_quality: facts.data_quality,
    facts: facts.facts,
    comparisons: facts.comparisons,
    judgments: judgments.judgments.map((item) => ({
      code: item.code,
      result: item.result,
      inference: item.inference,
      uncertainty: item.uncertainty,
      decision: item.decision,
      validation: item.validation
    })),
    changes: decision.changes,                // 每项带 expected_effect / rollback_condition / review_date
    kept: decision.kept,
    program_version: decision.program_version,
    decision_log: log,                        // 直接落 DECISION_LOG.md 的结构
    read_next: ['references/review-adjustment.md', 'references/training-dna.md']
  };
}

// ---------------------------------------------------------------------------
// 落盘：不覆盖已有文件（沿用 skill 的 resolveNonOverwritingPath 语义）
// ---------------------------------------------------------------------------
function writeAssets(directory, result) {
  const { resolveNonOverwritingPath } = ref('output-routing.js');
  fs.mkdirSync(directory, { recursive: true });
  const written = [];
  for (const [key, text] of Object.entries(result.assets || {})) {
    const target = resolveNonOverwritingPath(`${key}.md`, { outputDirectory: directory });
    fs.writeFileSync(target, text, { encoding: 'utf8', flag: 'wx' });
    written.push(target);
  }
  return written;
}

module.exports = {
  safetyGate,
  planProgram,
  todaySession,
  nutritionTargets,
  reviewWeek,
  activityFactorFrom,
  pickExercise,
  equipmentTier,
  describeMissingField,
  MOVEMENT_LIBRARY,
  SKILL_ROOT
};
