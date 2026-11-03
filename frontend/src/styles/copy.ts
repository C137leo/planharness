/** 文案与人话映射：页面和组件共用，避免同一个动作名三处不一致。 */

export const SLOT_LABELS: Record<string, string> = {
  warmup: '热身',
  strength: '力量',
  cardio: '有氧',
  mobility: '放松',
  meal_breakfast: '早餐',
  meal_lunch: '午餐',
  meal_dinner: '晚餐',
}

export const ACTION_NAMES: Record<string, string> = {
  dynamic_warmup: '动态热身',
  goblet_squat: '高脚杯深蹲',
  squat: '深蹲',
  bodyweight_squat: '自重深蹲',
  pushup: '俯卧撑',
  row: '哑铃划船',
  backpack_row: '背包划船',
  brisk_walk: '快走',
  easy_walk: '低冲击快走',
  glute_bridge: '臀桥',
  jump_squat: '跳跃深蹲',
  lunge: '弓步',
  run: '跑步',
  kettlebell_swing: '壶铃摆',
  burpee: '波比',
  plank: '平板支撑',
  full_body_stretch: '全身拉伸',
  stretch: '拉伸',
  chicken_rice_bowl: '鸡胸糙米碗',
  oats_eggs: '燕麦鸡蛋',
  beef_salad: '牛肉沙拉',
  fish_veggies: '鱼排蔬菜',
  tofu_veggies: '豆腐时蔬',
  rice_veggies: '米饭时蔬',
}

export const KIND_LABELS: Record<string, string> = {
  rest: '休息',
  injury_stop: '受伤停练',
  injury_swap: '受伤换动作',
  increase: '加量',
  diet: '饮食调整',
}

/** 兜底预设：接口拿不到时用它，保证抽屉不会空着。 */
export const QUICK_ACTIONS: Array<{ key: string; label: string; text: string; hint?: string }> = [
  { key: 'rest', label: '今天想休息', text: '今天想休息，把今天的训练挪到明天' },
  { key: 'injury_stop', label: '受伤了，先停练', text: '膝盖受伤了，先停止所有腿部训练' },
  { key: 'injury_swap', label: '换部位练', text: '膝盖疼，换成上肢训练' },
  { key: 'increase', label: '加强度', text: '这周感觉轻松，想加一点强度' },
  { key: 'diet', label: '调饮食', text: '晚餐想吃清淡一点，少油' },
  { key: 'time', label: '这周没时间', text: '这周只能练两天，每次 30 分钟' },
]

/** 动作名：查表失败就原样显示 ref，绝不显示空字符串。 */
export function actionName(ref: string): string {
  return ACTION_NAMES[ref] || ref
}

/** 优先用计划里带的 meta.name_zh（skill 生成的动作名都在那里）。 */
export function actionLabel(action: { action_ref: string; meta?: Record<string, unknown> }): string {
  const named = (action.meta || {}).name_zh
  if (typeof named === 'string' && named.trim()) return named
  return actionName(action.action_ref)
}

export function slotLabel(slot: string): string {
  return SLOT_LABELS[slot] || slot
}

/** 一条补丁操作用人话讲清楚：把什么换成什么 / 删掉哪天什么。 */
export function describeOperation(op: {
  op: string
  slot_key: string
  before_action_ref?: string
  after_action?: { action_ref: string } | null
  reason?: string
}): string {
  const parts = op.slot_key.split(':')
  const day = Number(parts[0])
  if (op.op === 'remove_action') {
    return `Day ${day} 删掉「${actionName(op.before_action_ref || '')}」`
  }
  const to = op.after_action ? actionName(op.after_action.action_ref) : '新动作'
  return `Day ${day} 把「${actionName(op.before_action_ref || '')}」换成「${to}」`
}
