/**
 * 动作缩略图：按身体部位映射到一张插画。
 *
 * 我们用 6 张部位图覆盖全部 40 个动作（腿部/臀部/胸部/背部/核心/拉伸），
 * 比给每个动作配一张图便宜得多，也比 emoji 更统一。
 * 映射顺序：先看 meta.movement_mode，再看 meta.primary_muscle，最后按 ref 兜底。
 */

export type ThumbKey = 'legs' | 'hip' | 'chest' | 'back' | 'core' | 'stretch'

export const THUMBS: Record<ThumbKey, string> = {
  legs: '/static/ex/legs.jpg',
  hip: '/static/ex/hip.jpg',
  chest: '/static/ex/chest.jpg',
  back: '/static/ex/back.jpg',
  core: '/static/ex/core.jpg',
  stretch: '/static/ex/stretch.jpg',
}

const MODE_TO_THUMB: Record<string, ThumbKey> = {
  knee_dominant: 'legs',
  hip_dominant: 'hip',
  horizontal_push: 'chest',
  vertical_push: 'chest',
  horizontal_pull: 'back',
  vertical_pull: 'back',
  core: 'core',
  cardio: 'legs',
  mobility: 'stretch',
}

const MUSCLE_TO_THUMB: Record<string, ThumbKey> = {
  quads: 'legs',
  glutes: 'hip',
  hamstrings: 'hip',
  'lower back': 'hip',
  chest: 'chest',
  shoulders: 'chest',
  back: 'back',
  lats: 'back',
  core: 'core',
  cardio: 'legs',
  mobility: 'stretch',
}

// 模板计划（没有 meta）用 ref 兜底
const REF_TO_THUMB: Record<string, ThumbKey> = {
  squat: 'legs',
  goblet_squat: 'legs',
  bodyweight_squat: 'legs',
  squat_template: 'legs',
  pushup: 'chest',
  push_up: 'chest',
  bench_press: 'chest',
  row: 'back',
  backpack_row: 'back',
  glute_bridge: 'hip',
  hip_thrust: 'hip',
  plank: 'core',
  crunch: 'core',
  stretch: 'stretch',
  brisk_walk: 'legs',
  easy_walk: 'legs',
}

export function thumbOf(action: {
  action_ref?: string
  meta?: Record<string, unknown>
}): string {
  const meta = action.meta || {}
  const mode = String(meta.movement_mode || '').toLowerCase()
  if (mode && MODE_TO_THUMB[mode]) return THUMBS[MODE_TO_THUMB[mode]]

  const muscle = String(meta.primary_muscle || '').toLowerCase()
  if (muscle && MUSCLE_TO_THUMB[muscle]) return THUMBS[MUSCLE_TO_THUMB[muscle]]

  const ref = String(action.action_ref || '').replace(/-/g, '_').toLowerCase()
  const key = REF_TO_THUMB[ref]
  if (key) return THUMBS[key]
  return THUMBS.legs
}

/** 训练/餐食统一的缩略图：餐食没有实拍图，继续用 emoji 更直观。 */
export function isMeal(action: { action_type?: string }): boolean {
  return action.action_type === 'meal'
}
