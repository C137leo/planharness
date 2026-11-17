/**
 * 动作缩略图：优先用具体动作图，其次退回身体部位插画。
 *
 * exerciseImages.json 只收录已存在的逐动作生成图，优先按动作 ID 精确匹配。
 * 旧模板别名先转为目录 ID；未知动作不再用身体部位图冒充专属动作图。
 */

import exerciseImages from './exerciseImages.json'

export type ThumbKey =
  | 'legs'
  | 'hip'
  | 'chest'
  | 'back'
  | 'core'
  | 'stretch'
  | 'pullUp'
  | 'deadBug'
  | 'dumbbellBenchPress'
  | 'deadlift'
  | 'walkingLunge'
  | 'legPress'
  | 'seatedRow'

export const THUMBS: Record<ThumbKey, string> = {
  legs: '/static/ex/legs.jpg',
  hip: '/static/ex/hip.jpg',
  chest: '/static/ex/chest.jpg',
  back: '/static/ex/back.jpg',
  core: '/static/ex/core.jpg',
  stretch: '/static/ex/stretch.jpg',
  pullUp: '/static/ex/pull-up.jpg',
  deadBug: '/static/ex/dead-bug.jpg',
  dumbbellBenchPress: '/static/ex/dumbbell-bench-press.jpg',
  deadlift: '/static/ex/deadlift.jpg',
  walkingLunge: '/static/ex/walking-lunge.jpg',
  legPress: '/static/ex/leg-press.jpg',
  seatedRow: '/static/ex/seated-row.jpg',
}


export function thumbOf(action: {
  action_ref?: string
  meta?: Record<string, unknown>
}): string {
  // 先按动作 ref 查：动作是什么，图就该是什么。
  // meta 有可能是补丁残留（例如休息补丁只改了 duration/meta，没换 action_ref），
  // 用 meta 优先会让"高脚杯深蹲"显示成拉伸图。
  const rawRef = String(action.action_ref || '')
  const aliases: Record<string, string> = {pushup:'push-up',push_up:'push-up',bodyweight_squat:'bodyweight-squat',goblet_squat:'goblet-squat',glute_bridge:'glute-bridge',hip_thrust:'hip-thrust',pullup:'pull-up', 'dynamic-warmup':'dynamic_warmup', 'full-body-stretch':'full_body_stretch',stretch:'full_body_stretch',easy_walk:'walking',brisk_walk:'walking'}
  const exactImages: Record<string, string> = exerciseImages
  const exact = exactImages[aliases[rawRef] || rawRef] || exactImages[rawRef.replace(/_/g, '-').toLowerCase()]
  if (exact) return exact
  return ''
}

/** 判断餐食动作。 */
export function isMeal(action: { action_type?: string }): boolean {
  return action.action_type === 'meal'
}

/** 餐次分类插画，仅作示意，不代表具体菜品或实际份量。 */
export const MEAL_THUMBS = {
  breakfast: '/static/meals/breakfast-v2.jpg',
  lunch: '/static/meals/lunch-v2.jpg',
  dinner: '/static/meals/dinner-v2.jpg',
} as const

/** 加餐及未知餐次复用早餐分类图，始终返回有效静态资源。 */
export function mealThumb(slot?: string | null): string {
  switch (String(slot || '').trim().toLowerCase().replace(/^meal_/, '')) {
    case 'lunch': return MEAL_THUMBS.lunch
    case 'dinner': return MEAL_THUMBS.dinner
    default: return MEAL_THUMBS.breakfast
  }
}
