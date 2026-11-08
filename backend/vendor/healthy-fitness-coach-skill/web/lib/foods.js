'use strict';

/**
 * 本地食物库（估算用）。
 * 数值为常见食物每 100g 的近似营养值，仅用于估算配餐，不是食物成分数据库的权威数据。
 * allergens 采用通用类别键，供"过敏原 → 安全门 → 食谱排除"链路使用。
 */

const FOODS = [
  // ---- 蛋白源 ----
  { name: '鸡胸肉', group: 'protein', kcal: 165, protein: 31, fat: 3.6, carb: 0, diet: ['meat'] },
  { name: '鸡蛋', group: 'protein', kcal: 143, protein: 13, fat: 9.5, carb: 1.1, diet: ['egg'] },
  { name: '三文鱼', group: 'protein', kcal: 208, protein: 20, fat: 13, carb: 0, diet: ['fish', 'seafood'] },
  { name: '北豆腐', group: 'protein', kcal: 76, protein: 8, fat: 4.8, carb: 1.9, diet: ['soy'] },
  { name: '希腊酸奶', group: 'protein', kcal: 59, protein: 10, fat: 0.4, carb: 3.6, diet: ['dairy'] },
  { name: '牛奶', group: 'protein', kcal: 61, protein: 3.2, fat: 3.3, carb: 4.8, diet: ['dairy'] },
  { name: '瘦牛肉', group: 'protein', kcal: 250, protein: 26, fat: 15, carb: 0, diet: ['meat', 'beef'] },
  { name: '虾', group: 'protein', kcal: 99, protein: 24, fat: 0.3, carb: 0, diet: ['seafood'] },
  { name: '乳清蛋白粉', group: 'protein', kcal: 400, protein: 80, fat: 6, carb: 8, diet: ['dairy'] },
  { name: '鹰嘴豆', group: 'protein', kcal: 364, protein: 19, fat: 6, carb: 61, diet: ['legume'] },

  // ---- 主食 ----
  { name: '米饭', group: 'carb', kcal: 130, protein: 2.7, fat: 0.3, carb: 28, diet: [] },
  { name: '燕麦', group: 'carb', kcal: 389, protein: 16.9, fat: 6.9, carb: 66, diet: ['gluten'] },
  { name: '红薯', group: 'carb', kcal: 86, protein: 1.6, fat: 0.1, carb: 20, diet: [] },
  { name: '全麦面包', group: 'carb', kcal: 247, protein: 13, fat: 3.4, carb: 41, diet: ['gluten'] },
  { name: '意面', group: 'carb', kcal: 158, protein: 5.8, fat: 0.9, carb: 31, diet: ['gluten'] },
  { name: '土豆', group: 'carb', kcal: 77, protein: 2, fat: 0.1, carb: 17, diet: [] },
  { name: '玉米', group: 'carb', kcal: 96, protein: 3.4, fat: 1.5, carb: 21, diet: [] },

  // ---- 蔬菜 ----
  { name: '西兰花', group: 'veg', kcal: 34, protein: 2.8, fat: 0.4, carb: 7, diet: [] },
  { name: '菠菜', group: 'veg', kcal: 23, protein: 2.9, fat: 0.4, carb: 3.6, diet: [] },
  { name: '番茄', group: 'veg', kcal: 18, protein: 0.9, fat: 0.2, carb: 3.9, diet: [] },
  { name: '生菜', group: 'veg', kcal: 15, protein: 1.4, fat: 0.2, carb: 2.9, diet: [] },
  { name: '青椒', group: 'veg', kcal: 20, protein: 0.9, fat: 0.2, carb: 4.6, diet: [] },

  // ---- 水果 ----
  { name: '香蕉', group: 'fruit', kcal: 89, protein: 1.1, fat: 0.3, carb: 23, diet: [] },
  { name: '蓝莓', group: 'fruit', kcal: 57, protein: 0.7, fat: 0.3, carb: 14, diet: [] },
  { name: '苹果', group: 'fruit', kcal: 52, protein: 0.3, fat: 0.2, carb: 14, diet: [] },

  // ---- 脂肪 ----
  { name: '橄榄油', group: 'fat', kcal: 884, protein: 0, fat: 100, carb: 0, diet: [] },
  { name: '牛油果', group: 'fat', kcal: 160, protein: 2, fat: 15, carb: 9, diet: [] },
  { name: '混合坚果', group: 'fat', kcal: 607, protein: 20, fat: 54, carb: 21, diet: ['nuts'] },
  { name: '花生酱', group: 'fat', kcal: 588, protein: 25, fat: 50, carb: 20, diet: ['peanut'] }
];

/** 用户口语 → 过敏原类别键 */
const ALLERGEN_ALIASES = [
  { key: 'dairy', pattern: /牛奶|乳制品|乳糖|奶类|乳清|奶酪|芝士|dairy|milk|lactose/iu, label: '乳制品' },
  { key: 'egg', pattern: /鸡蛋|蛋类|蛋白过敏|egg/iu, label: '蛋类' },
  { key: 'peanut', pattern: /花生|peanut/iu, label: '花生' },
  { key: 'nuts', pattern: /坚果|核桃|腰果|杏仁|榛子|nut/iu, label: '坚果' },
  { key: 'seafood', pattern: /海鲜|虾|蟹|贝|甲壳|shellfish|seafood/iu, label: '甲壳/海鲜' },
  { key: 'fish', pattern: /鱼|三文鱼|金枪鱼|鳕鱼|fish/iu, label: '鱼类' },
  { key: 'soy', pattern: /大豆|豆制品|豆腐|豆浆|soy/iu, label: '大豆' },
  { key: 'gluten', pattern: /小麦|麸质|面食|面包|面条|gluten|wheat/iu, label: '小麦/麸质' },
  { key: 'beef', pattern: /牛肉|beef/iu, label: '牛肉' },
  { key: 'legume', pattern: /鹰嘴豆|豆类|legume/iu, label: '豆类' }
];

/** 饮食偏好 → 需要排除的类别 */
const DIET_ALIASES = [
  { pattern: /纯素|全素|vegan/iu, exclude: ['meat', 'fish', 'seafood', 'egg', 'dairy'], label: '纯素' },
  { pattern: /蛋奶素|素食|vegetarian/iu, exclude: ['meat', 'fish', 'seafood'], label: '素食（蛋奶可用）' },
  { pattern: /不吃牛肉|忌牛肉/iu, exclude: ['beef'], label: '不吃牛肉' },
  { pattern: /乳糖不耐/iu, exclude: ['dairy'], label: '乳糖不耐' },
  { pattern: /不吃海鲜|忌海鲜/iu, exclude: ['seafood', 'fish'], label: '不吃海鲜' }
];

const SEVERE_ALLERGY_PATTERN = /严重|休克|anaphyla|喉头水肿|呼吸困难|抢救|肾上腺素/iu;

function normalizeList(value) {
  if (value === undefined || value === null) return [];
  if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean);
  return String(value)
    .split(/[,，、;；/\n]+/u)
    .map((item) => item.trim())
    .filter(Boolean);
}

/**
 * 把用户填的过敏/饮食信息转成结构化约束。
 * 这既是食谱的排除表，也是喂给安全门的素材。
 */
function resolveDietConstraints({ allergies = [], dietPreferences = [] } = {}) {
  const allergyItems = normalizeList(allergies);
  const dietItems = normalizeList(dietPreferences);
  const allergenKeys = new Set();
  const dietKeys = new Set();
  const labels = [];
  const unmatched = [];

  for (const item of allergyItems) {
    const hit = ALLERGEN_ALIASES.find((alias) => alias.pattern.test(item));
    if (hit) { allergenKeys.add(hit.key); labels.push(`过敏：${hit.label}`); }
    else unmatched.push(item);
  }
  for (const item of dietItems) {
    const hit = DIET_ALIASES.find((alias) => alias.pattern.test(item));
    if (hit) { hit.exclude.forEach((key) => dietKeys.add(key)); labels.push(`饮食：${hit.label}`); }
    else unmatched.push(item);
  }

  const excludedDietKeys = new Set([...allergenKeys, ...dietKeys]);
  const foods = FOODS.filter((food) => !food.diet.some((key) => excludedDietKeys.has(key)));

  return {
    allergens: [...allergenKeys],
    diet_exclusions: [...dietKeys],
    excluded_keys: [...excludedDietKeys],
    labels,
    unmatched,
    severe: SEVERE_ALLERGY_PATTERN.test(allergyItems.join(' ')),
    allowed_foods: foods,
    excluded_food_names: FOODS.filter((food) => !foods.includes(food)).map((food) => food.name)
  };
}

module.exports = { FOODS, normalizeList, resolveDietConstraints, ALLERGEN_ALIASES, DIET_ALIASES };
