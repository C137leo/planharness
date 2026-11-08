/**
 * 动作缩略图：优先用具体动作图，其次退回身体部位插画。
 *
 * 当前有 6 张部位图 + 7 张动作图（引体/死虫/哑铃卧推/硬拉/行走弓步/腿举/坐姿划船）。
 * 具体动作图只配给动作模式真正一致的动作：例如 `seated-row` 用坐姿划船图，
 * `row`（哑铃划船）仍走背部部位图，避免把器械图配错。
 * 映射顺序：先按 action_ref，再看 meta.movement_mode，最后看 meta.primary_muscle。
 */

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

// 模板计划（没有 meta）用 ref 兜底。
// 这里放的是"动作图能对得上"的 ref；对不上的仍走部位图，别硬配。
const REF_TO_THUMB: Record<string, ThumbKey> = {
  'pull-up': 'pullUp',
  pull_up: 'pullUp',
  pullup: 'pullUp',
  'dead-bug': 'deadBug',
  dead_bug: 'deadBug',
  'dumbbell-bench-press': 'dumbbellBenchPress',
  dumbbell_bench_press: 'dumbbellBenchPress',
  deadlift: 'deadlift',
  'walking-lunge': 'walkingLunge',
  walking_lunge: 'walkingLunge',
  lunge: 'walkingLunge',
  'forward-lunge': 'walkingLunge',
  'barbell-lunge': 'walkingLunge',
  'dumbbell-lunges': 'walkingLunge',
  'leg-press': 'legPress',
  leg_press: 'legPress',
  'seated-row': 'seatedRow',
  seated_row: 'seatedRow',
  'seated-cable-row': 'seatedRow',
  'cable-row': 'seatedRow',
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
  // 先按动作 ref 查：动作是什么，图就该是什么。
  // meta 有可能是补丁残留（例如休息补丁只改了 duration/meta，没换 action_ref），
  // 用 meta 优先会让"高脚杯深蹲"显示成拉伸图。
  const rawRef = String(action.action_ref || '')
  const refKey = REF_TO_THUMB[rawRef] || REF_TO_THUMB[rawRef.replace(/-/g, '_').toLowerCase()]
  if (refKey) return THUMBS[refKey]

  const meta = action.meta || {}
  const mode = String(meta.movement_mode || '').toLowerCase()
  if (mode && MODE_TO_THUMB[mode]) return THUMBS[MODE_TO_THUMB[mode]]

  const muscle = String(meta.primary_muscle || '').toLowerCase()
  if (muscle && MUSCLE_TO_THUMB[muscle]) return THUMBS[MUSCLE_TO_THUMB[muscle]]

  return THUMBS.legs
}

/** 训练/餐食统一的缩略图：餐食没有实拍图，继续用 emoji 更直观。 */
export function isMeal(action: { action_type?: string }): boolean {
  return action.action_type === 'meal'
}
