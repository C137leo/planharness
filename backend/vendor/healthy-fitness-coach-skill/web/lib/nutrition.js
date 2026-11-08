'use strict';

/**
 * 营养与配餐层。
 * - 热量/蛋白区间复用 agent-integration/coach.js 的实现（单一真源，参数出处 references/nutrition.md）
 * - 食谱由本地食物库按目标宏量做确定性分配；过敏原与饮食偏好直接参与排除
 * - 所有输出标注为「估算」，不做医疗或治疗性饮食建议
 */

const coach = require('../../agent-integration/coach.js');
const { resolveDietConstraints, normalizeList } = require('./foods.js');

const MEAL_PLAN = [
  { key: 'breakfast', title: '早餐', share: 0.25, veg: 0, prefer: ['鸡蛋', '牛奶', '燕麦', '希腊酸奶', '香蕉'] },
  { key: 'lunch', title: '午餐', share: 0.35, veg: 150, prefer: ['鸡胸肉', '米饭', '西兰花', '瘦牛肉'] },
  { key: 'dinner', title: '晚餐', share: 0.30, veg: 150, prefer: ['北豆腐', '红薯', '菠菜', '虾'] },
  { key: 'snack', title: '加餐', share: 0.10, veg: 0, prefer: ['希腊酸奶', '乳清蛋白粉', '苹果', '混合坚果'] }
];

const CARB_PREFERENCE = ['米饭', '红薯', '燕麦', '土豆', '全麦面包', '意面', '玉米'];

function round5(value) {
  return Math.max(0, Math.round(value / 5) * 5);
}

function macrosOf(food, grams) {
  const ratio = grams / 100;
  return {
    kcal: Math.round(food.kcal * ratio),
    protein: Number((food.protein * ratio).toFixed(1)),
    fat: Number((food.fat * ratio).toFixed(1)),
    carb: Number((food.carb * ratio).toFixed(1))
  };
}

function pickFrom(allowed, preferred, group) {
  for (const name of preferred) {
    const food = allowed.find((item) => item.name === name);
    if (food) return food;
  }
  return allowed.find((item) => item.group === group) || null;
}

function buildMeals({ targets, constraints, goal, mode = 'simplified' }) {
  const allowed = constraints.allowed_foods;
  const proteinTargetTotal = /减脂|减重|cut/iu.test(String(goal))
    ? targets.macros.protein_g.high_g
    : Math.round((targets.macros.protein_g.low_g + targets.macros.protein_g.high_g) / 2);
  const kcalTarget = targets.target_kcal || targets.tdee;

  if (mode === 'habit') {
    return {
      mode,
      meals: MEAL_PLAN.map((meal) => ({
        key: meal.key,
        title: meal.title,
        portions: [
          '一掌心蛋白质（肉/鱼/蛋/豆制品）',
          meal.veg ? '两捧蔬菜' : '一份水果或奶类',
          '一拳主食'
        ],
        items: [],
        totals: null
      })),
      totals: null,
      target: null,
      gaps: [],
      note: '习惯模式不计算热量：本轮只挑一个最容易改的行为（如减少含糖饮料）。'
    };
  }

  const meals = [];
  const gaps = [];
  let usedKcal = 0;
  let usedProtein = 0;

  for (const plan of MEAL_PLAN) {
    const mealKcal = kcalTarget * plan.share;
    const mealProtein = proteinTargetTotal * plan.share;
    const items = [];

    const proteinFood = pickFrom(allowed, plan.prefer, 'protein');
    if (!proteinFood) {
      gaps.push(`${plan.title}：可用蛋白源不足（饮食限制过严），建议补充蛋白来源或咨询营养专业人士。`);
      continue;
    }
    const proteinGrams = Math.min(300, Math.max(40, round5((mealProtein / proteinFood.protein) * 100)));
    items.push({ name: proteinFood.name, grams: proteinGrams, ...macrosOf(proteinFood, proteinGrams) });

    let vegKcal = 0;
    if (plan.veg > 0) {
      const veg = pickFrom(allowed, [], 'veg');
      if (veg) {
        items.push({ name: veg.name, grams: plan.veg, ...macrosOf(veg, plan.veg) });
        vegKcal = macrosOf(veg, plan.veg).kcal;
      }
    }

    const proteinKcal = macrosOf(proteinFood, proteinGrams).kcal;
    const remain = mealKcal - proteinKcal - vegKcal;
    if (remain > 60) {
      const carbFood = pickFrom(allowed, CARB_PREFERENCE, 'carb');
      if (carbFood) {
        const carbGrams = Math.min(300, round5((remain / carbFood.kcal) * 100));
        if (carbGrams > 0) items.push({ name: carbFood.name, grams: carbGrams, ...macrosOf(carbFood, carbGrams) });
      }
    } else if (remain < -40) {
      gaps.push(`${plan.title}：蛋白目标已超该餐热量预算，主食已省略。`);
    }

    const totals = items.reduce((acc, item) => ({
      kcal: acc.kcal + item.kcal,
      protein: Number((acc.protein + item.protein).toFixed(1)),
      fat: Number((acc.fat + item.fat).toFixed(1)),
      carb: Number((acc.carb + item.carb).toFixed(1))
    }), { kcal: 0, protein: 0, fat: 0, carb: 0 });

    usedKcal += totals.kcal;
    usedProtein += totals.protein;
    meals.push({ key: plan.key, title: plan.title, items, totals });
  }

  if (usedProtein < proteinTargetTotal * 0.85) {
    gaps.push(`蛋白质估算 ${Math.round(usedProtein)}g，低于目标 ${proteinTargetTotal}g；可加一份蛋白粉或无糖酸奶。`);
  }

  return {
    mode,
    meals,
    totals: {
      kcal: Math.round(usedKcal),
      protein_g: Math.round(usedProtein),
      fat_g: Math.round(meals.reduce((sum, meal) => sum + meal.totals.fat, 0)),
      carb_g: Math.round(meals.reduce((sum, meal) => sum + meal.totals.carb, 0))
    },
    target: { kcal: Math.round(kcalTarget), protein_g: proteinTargetTotal },
    delta: { kcal: Math.round(usedKcal - kcalTarget), protein_g: Math.round(usedProtein - proteinTargetTotal) },
    gaps,
    note: '食材克数为估算值，按包装营养成分表与自身体重趋势校准；调味料、烹调油未计入。'
  };
}

/**
 * 安全门：红旗症状 + 过敏 + 目标与 BMI 的合理性。
 * 返回的 must_avoid 同时喂给食谱排除与前端提示。
 */
function screenProfile({ profile = {}, currentState = {}, instruction = '' } = {}) {
  const diet = resolveDietConstraints({
    allergies: profile.allergies,
    dietPreferences: profile.diet_preferences
  });

  const combined = [
    instruction,
    profile.injury_or_medical_constraints,
    normalizeList(profile.allergies).join(' ')
  ].filter(Boolean).join(' ');

  const redFlag = coach.safetyGate({ ...profile, ...currentState, instruction: combined });

  const reasons = [];
  let level = 'clear';

  if (redFlag.status === 'stop') {
    level = 'stop';
    reasons.push({ code: 'red_flag', text: redFlag.required_action });
  } else if (redFlag.status === 'clarify') {
    level = 'caution';
    reasons.push({ code: 'confirm_current_symptoms', text: redFlag.required_action });
  }

  if (diet.severe) {
    if (level === 'clear') level = 'caution';
    reasons.push({
      code: 'severe_allergy',
      text: '记录到严重过敏史：饮食建议不替代医生与过敏专科方案，外食需确认交叉污染风险。'
    });
  }

  const height = Number(profile.height_cm);
  const weight = Number(profile.weight_kg);
  const bmi = height > 0 && weight > 0 ? Number((weight / Math.pow(height / 100, 2)).toFixed(1)) : null;
  const cutting = /减脂|减重|cut/iu.test(String(profile.goal || ''));

  if (bmi !== null && cutting && bmi < 18.5) {
    level = level === 'stop' ? level : 'caution';
    reasons.push({ code: 'underweight_cutting', text: `BMI ${bmi}：不建议在偏低体重下制造能量缺口，优先力量训练与饮食质量。` });
  }
  if (bmi !== null && bmi >= 35) {
    if (level === 'clear') level = 'caution';
    reasons.push({ code: 'bmi_high_impact', text: `BMI ${bmi}：开始高冲击训练前建议先做医疗评估，可从低冲击有氧与基础力量起步。` });
  }
  if (diet.unmatched.length) {
    reasons.push({ code: 'unmatched_diet_item', text: `未能识别的过敏/饮食项：${diet.unmatched.join('、')}（已按未知处理，请在备注中确认）。` });
  }

  return {
    level,
    red_flag: redFlag,
    bmi,
    diet: {
      allergens: diet.allergens,
      diet_exclusions: diet.diet_exclusions,
      labels: diet.labels,
      excluded_food_names: diet.excluded_food_names,
      unmatched: diet.unmatched,
      allowed_food_count: diet.allowed_foods.length
    },
    reasons,
    must_avoid: [...new Set([...diet.allergens, ...diet.diet_exclusions])],
    boundaries: [
      '不诊断疾病，不做治疗性饮食或疾病专项康复处方。',
      '不提供补剂处方、药物或 PED 指导。',
      '过敏信息仅用于排除食材，不构成医疗建议。'
    ]
  };
}

function nutritionPlan({ profile, mode = 'simplified' }) {
  const targets = coach.nutritionTargets({
    profile,
    body: {
      sex: profile.sex,
      age: Number(profile.age),
      height_cm: Number(profile.height_cm),
      weight_kg: Number(profile.weight_kg)
    },
    mode,
    instruction: profile.injury_or_medical_constraints
  });
  if (targets.status !== 'ready') return targets;

  const constraints = resolveDietConstraints({
    allergies: profile.allergies,
    dietPreferences: profile.diet_preferences
  });
  const meals = buildMeals({ targets, constraints, goal: profile.goal, mode });
  return { status: 'ready', targets, meals, constraints };
}

module.exports = { nutritionPlan, buildMeals, screenProfile, macrosOf };
