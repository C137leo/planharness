/**
 * 全局教练工作区的共享状态。
 *
 * 组件在每个页面各挂一份，但对话消息、历史是否加载过、最近一次页面上下文
 * 都来自这里的模块级单例——页面切换不会开启一个新的 Agent。
 */
import { ref } from 'vue'
import type { AgentConversation, McdOrder } from '../api'

export type CoachAttachment = {
  kind: string
  title: string
  detail?: string
  /** 生成这条附件时的完整 Agent 结果，用于在对话里重新展开 */
  result?: unknown
}

export type CoachChatMessage = {
  id: string
  role: 'user' | 'agent'
  text: string
  /** 服务端消息时间；本地新消息用当前时间，便于像聊天软件一样分组显示 */
  at?: string
  attachment?: CoachAttachment
}

export const coachWorkspaceMessages = ref<CoachChatMessage[]>([])
export const coachWorkspaceThreadId = ref('')
export const coachWorkspaceHistoryThread = ref('')
export const coachWorkspaceConversations = ref<AgentConversation[]>([])
export const coachWorkspaceLastPage = ref('today')
export const coachWorkspaceLastContext = ref<Record<string, unknown>>({})

const LAST_MCD_ORDER_KEY = 'planharness_last_mcd_order'

export function isMcdOrderExpired(order: McdOrder | null | undefined): boolean {
  if (!order?.expire_at) return false
  const time = Date.parse(String(order.expire_at).replace(' ', 'T'))
  return Number.isFinite(time) && time <= Date.now()
}

function readLastMcdOrder(): McdOrder | null {
  try {
    const stored = (uni.getStorageSync(LAST_MCD_ORDER_KEY) as McdOrder) || null
    if (stored && isMcdOrderExpired(stored)) {
      uni.removeStorageSync(LAST_MCD_ORDER_KEY)
      return null
    }
    return stored
  } catch {
    return null
  }
}

export const coachWorkspaceLastMcdOrder = ref<McdOrder | null>(readLastMcdOrder())

export function setLastMcdOrder(order: McdOrder | null): void {
  coachWorkspaceLastMcdOrder.value = order
  try {
    if (order) uni.setStorageSync(LAST_MCD_ORDER_KEY, order)
    else uni.removeStorageSync(LAST_MCD_ORDER_KEY)
  } catch {
    // 本地缓存失败不影响本次会话
  }
}

export function pushCoachMessage(message: CoachChatMessage): void {
  coachWorkspaceMessages.value = [...coachWorkspaceMessages.value, message]
}

export function resetCoachWorkspace(): void {
  coachWorkspaceMessages.value = []
  coachWorkspaceThreadId.value = ''
  coachWorkspaceHistoryThread.value = ''
  coachWorkspaceConversations.value = []
  coachWorkspaceLastPage.value = 'today'
  coachWorkspaceLastContext.value = {}
  setLastMcdOrder(null)
}
