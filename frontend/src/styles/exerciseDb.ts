import guidanceCatalog from '../../../backend/app/domain/exercise_guidance_catalog.json'
import guidance from '../../../backend/app/domain/exercise_guidance.json'
import translations from '../../../backend/app/domain/exercise_guidance_zh.json'

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
  ['全身拉伸', 'Full Body Stretch'],
  ['动态拉伸', 'Dynamic Stretch'],
  ['静态拉伸', 'Static Stretch'],
  ['泡沫轴放松', 'Foam Roll'],
  ['猫牛式', 'Cat-Cow Stretch'],
  ['拉伸', 'Stretch'],
  ['高脚杯深蹲', 'Goblet Squat'],
  ['罗马尼亚硬拉', 'Romanian Deadlift'],
  ['单臂哑铃划船', 'One-Arm Dumbbell Row'],
  ['哑铃俯身划船', 'Dumbbell Bent-Over Row'],
  ['坐姿绳索划船', 'Seated Cable Row'],
  ['侧平板支撑', 'Side Bridge'],
  ['平板支撑', 'Plank'],
  ['俯卧撑', 'Pushups'],
  ['臀桥', 'Butt Lift (Bridge)'],
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

const catalogEntries = Object.entries(guidanceCatalog)
function catalogMatch(text: string) {
  const normalized = text.trim().replace(/\s*的?动作要领$/, '').trim().toLowerCase()
  return catalogEntries.find(([ref, item]) => [ref, item.name_zh, item.name_en].some(label => label.toLowerCase() === normalized))?.[1]
}

export function exerciseGuidance(text: string) {
  const normalized = (text || '').toLowerCase()
  const local = guidance.find(item => item.aliases.some(alias => alias.toLowerCase() === normalized.trim().replace(/\s*的?动作要领$/, '').trim()))
  if (local) return local
  const match = catalogMatch(text)
  if (!match || !('instructions' in match)) return undefined
  const ref = catalogEntries.find(([,entry]) => entry === match)?.[0] || ''
  const chinese: Record<string, string[]> = translations
  if (!chinese[ref]) return undefined
  return { title: `${match.name_zh}的动作要领`, content: chinese[ref].join('\n'), source_title: 'free-exercise-db · 中文整理', search_term: match.search_term }
}

export function hasExerciseGuidance(text: string): boolean {
  return !!exerciseGuidance(text) || !!exerciseSearchTerm(text)
}

export function openExerciseGuidance(text: string): void {
  const item = exerciseGuidance(text)
  if (!item) {
    openExerciseDbTerm(exerciseSearchTerm(text))
    return
  }
  const term = item.search_term || text.trim().replace(/\s*的?动作要领$/, '')
  if (copyExerciseTerm(term)) {
    uni.showToast({ title: '已复制动作名称，请粘贴搜索', icon: 'none', duration: 2500 })
  }
  uni.showModal({
    title: item.title,
    content: `${item.content}\n\n依据：${item.source_title}`,
    showCancel: !!item.search_term,
    confirmText: item.search_term ? '查看动作库' : '知道了',
    success: result => {
      if (result.confirm && item.search_term) openExerciseDbTerm(item.search_term)
    },
  })
}

export function exerciseSearchTerm(text: string): string {
  const local = exerciseGuidance(text)
  if (local) return local.search_term
  if (catalogMatch(text)) return ''

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

export function copyExerciseTerm(term: string, completed?: (success: boolean) => void): boolean {
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
      if (copied) { completed?.(true); return true }
    } catch {
      // 继续走 uni 的兼容实现
    }
  }
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(term).then(() => completed?.(true), () => completed?.(false))
    return false
  }
  if (typeof uni !== 'undefined' && typeof uni.setClipboardData === 'function') {
    uni.setClipboardData({
      data: term,
      success: () => { if(completed) completed(true); else uni.showToast({ title: '已经复制动作名称，请在新页面搜索框直接粘贴查看', icon: 'none', duration: 2500 }) },
      fail: () => { if(completed) completed(false); else uni.showToast({ title: `复制失败，请手动搜索：${term}`, icon: 'none', duration: 3000 }) },
    })
    // 异步复制尚未完成，不提前声称成功。
    return false
  }
  completed?.(false)
  return false
}

let navigationTimer: ReturnType<typeof setTimeout> | undefined
let notice: HTMLElement | undefined
let navigationVersion = 0
export function openExerciseDbTerm(term: string): void {
  if (!term) return
  const version=++navigationVersion
  if(navigationTimer) clearTimeout(navigationTimer)
  notice?.remove()
  copyExerciseTerm(term, success => {
    if(version!==navigationVersion) return
    const message = success ? '已经复制动作名称，请在新页面搜索框直接粘贴查看' : `复制失败，请在动作库手动搜索：${term}`
    if (typeof document !== 'undefined') {
      notice=document.createElement('div')
      notice.setAttribute('role','status')
      notice.style.cssText='position:fixed;z-index:2147483647;top:24px;left:50%;transform:translateX(-50%);max-width:90vw;padding:18px 24px;border-radius:14px;background:#3d3028;color:white;box-shadow:0 8px 32px #0004;font-size:15px;line-height:1.7;pointer-events:none;text-align:center'
      notice.textContent=message
      document.body.appendChild(notice)
    } else uni.showToast({title:message,icon:'none',duration:1000})
    navigationTimer=setTimeout(()=>{ notice?.remove();notice=undefined;openExerciseDestination() },1000)
  })
}

function openExerciseDestination(): void {
  const globalOpen = (globalThis as unknown as { open?: (u: string, t?: string) => unknown }).open
  if (typeof globalOpen === 'function') {
    const destination=globalOpen(EXERCISE_DB_URL, '_blank')
    if(destination) return
  }
  if (typeof location !== 'undefined') location.href = EXERCISE_DB_URL
}
