/**
 * 动作 → free-exercise-db 搜索词。
 *
 * 站点不支持 URL 预填（实测 ?q= 会被忽略），所以统一做两件事：
 * 先把英文名复制到剪贴板，再打开站点；用户在新页面搜索框粘贴。
 */
export const EXERCISE_DB_URL = 'https://yuhonas.github.io/free-exercise-db/'

// 顺序不重要：匹配时取文本里最早出现的动作，同位置取更长的词。
// 这样「高脚杯深蹲」不会被「深蹲」抢先，「如果深蹲…改成臀桥」会匹配深蹲。
export const EXERCISE_DB_TERMS: Array<[string, string]> = [
  ['高脚杯深蹲', 'Goblet Squat'],
  ['罗马尼亚硬拉', 'Romanian Deadlift'],
  ['单臂哑铃划船', 'One-Arm Dumbbell Row'],
  ['哑铃俯身划船', 'Dumbbell Bent-Over Row'],
  ['坐姿绳索划船', 'Seated Cable Row'],
  ['侧平板支撑', 'Side Bridge'],
  ['平板支撑', 'Plank'],
  ['俯卧撑', 'Push-up'],
  ['臀桥', 'Glute Bridge'],
  ['臀推', 'Hip Thrust'],
  ['箭步蹲', 'Lunge'],
  ['弓步', 'Lunge'],
  ['深蹲', 'Squat'],
  ['硬拉', 'Deadlift'],
  ['划船', 'Row'],
  ['卧推', 'Bench Press'],
  ['肩推', 'Shoulder Press'],
  ['推举', 'Shoulder Press'],
  ['引体向上', 'Pull-Up'],
  ['死虫', 'Dead Bug'],
  ['腿举', 'Leg Press'],
  ['腿弯举', 'Leg Curl'],
  ['提踵', 'Calf Raise'],
  ['弯举', 'Curl'],
  ['臂屈伸', 'Triceps Extension'],
  ['卷腹', 'Crunch'],
  ['鸟狗', 'Bird Dog'],
  // 知识库里的「练臀/练腿/练背/练胸/核心」是清单类条目，
  // 用它作为 fallback，避免只能靠 sheet 的问句猜动作。
  ['练臀', 'Glute Bridge'],
  ['练腿', 'Squat'],
  ['练背', 'Row'],
  ['练胸', 'Push-up'],
  ['练肩', 'Shoulder Press'],
]

export function exerciseSearchTerm(text: string): string {
  let best: { index: number; length: number; en: string } | null = null
  for (const [zh, en] of EXERCISE_DB_TERMS) {
    const index = (text || '').indexOf(zh)
    if (index < 0) continue
    if (!best || index < best.index || (index === best.index && zh.length > best.length)) {
      best = { index, length: zh.length, en }
    }
  }
  return best ? best.en : ''
}

export function copyExerciseTerm(term: string): boolean {
  // 必须在 window.open 之前同步复制：新标签一打开，原页面失去焦点，
  // Clipboard API 和 execCommand 都可能被浏览器拒绝。
  if (typeof document !== 'undefined') {
    try {
      const area = document.createElement('textarea')
      area.value = term
      area.setAttribute('readonly', '')
      area.style.position = 'fixed'
      area.style.top = '-9999px'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      area.setSelectionRange(0, term.length)
      const copied = document.execCommand('copy')
      document.body.removeChild(area)
      if (copied) return true
    } catch {
      // 继续走 uni 的兼容实现
    }
  }
  if (typeof uni !== 'undefined' && typeof uni.setClipboardData === 'function') {
    uni.setClipboardData({ data: term })
    return true
  }
  return false
}

export function openExerciseDbTerm(term: string): void {
  if (!term) return
  const copied = copyExerciseTerm(term)
  const globalOpen = (globalThis as unknown as { open?: (u: string, t?: string) => unknown }).open
  if (typeof globalOpen === 'function') {
    globalOpen(EXERCISE_DB_URL, '_blank')
  } else if (typeof location !== 'undefined') {
    location.href = EXERCISE_DB_URL
  }
  uni.showToast({
    title: copied ? `已复制 ${term}` : `搜索：${term}`,
    icon: 'none',
    duration: 2500,
  })
}
