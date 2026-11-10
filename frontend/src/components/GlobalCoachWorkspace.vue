<script setup lang="ts">
/**
 * GlobalCoachWorkspace：全站唯一的教练工作区。
 *
 * 今天 / 计划 / 进展 / 我的 / 训练执行共用同一个组件、同一个 `/agent/chat`
 * 和同一份对话历史（模块级共享状态 + 服务端 AgentMessage）。
 * 页面只传入业务上下文，不创建第二个 Agent。
 *
 * 桌面端：左侧可选对话 + 中间对话框；移动端单列。
 * 所有对话线程仍共用同一个 Agent、长期记忆和页面上下文。
 */
import { computed, nextTick, ref, watch } from 'vue'
import {
  api,
  ApiError,
  getStoredMcdToken,
  setStoredMcdToken,
  type AgentActionChip,
  type AgentChatResult,
  type AgentConversation,
  type AgentGuidance,
  type AgentMessageItem,
  type McdOrder,
  type PatchRequest,
  type PlanDraft,
  type TodayAgentResult,
  type WorkoutSession,
} from '../api'
import { actionLabel, describeOperation, slotLabel } from '../styles/copy'
import { exerciseSearchTerm, openExerciseDbTerm } from '../styles/exerciseDb'
import { todayStr } from '../styles/dates'
import KnowledgeSheet from './KnowledgeSheet.vue'
import McdOrderSheet from './McdOrderSheet.vue'
import {
  coachWorkspaceConversations,
  coachWorkspaceHistoryThread,
  coachWorkspaceLastContext,
  coachWorkspaceLastMcdOrder,
  coachWorkspaceLastPage,
  coachWorkspaceMessages,
  isMcdOrderExpired,
  coachWorkspaceThreadId,
  pushCoachMessage,
  setLastMcdOrder,
} from '../composables/coachWorkspace'

const props = defineProps<{
  visible: boolean
  page?: 'today' | 'plan' | 'profile' | 'progress' | 'workout'
  date?: string
  sessionId?: string
  initialText?: string
  autoSend?: boolean
  /** 页面附带的只读业务上下文：今日动作、长期计划、进展数据、档案摘要 */
  context?: Record<string, unknown>
  /** modal：其他页面弹出；inline：今天页直接嵌入的大对话区 */
  mode?: 'modal' | 'inline'
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'changed'): void
  (e: 'openWorkout', payload: { sessionId?: string; date?: string }): void
  (e: 'openTodayBrief', output: TodayAgentResult): void
  (e: 'openMemory'): void
  (e: 'openProgress'): void
}>()

const isOpen = computed(() => props.mode === 'inline' || props.visible)
const messages = coachWorkspaceMessages
const input = ref('')
const busy = ref(false)
const error = ref('')
const result = ref<AgentChatResult | null>(null)
const richAnchorMessageId = ref('')
const richCollapsed = ref(false)
const activeSession = ref<WorkoutSession | null>(null)
const lastAutoText = ref('')
const confirming = ref(false)
const knowledgeOpen = ref(false)
const knowledgeQuestion = ref('')

const PAGE_LABEL: Record<string, string> = {
  today: '今天',
  plan: '长期计划',
  profile: '我的档案',
  progress: '训练进展',
  workout: '训练中',
}

const pageKey = computed(() => props.page || 'today')
const greeting = computed(() => {
  switch (pageKey.value) {
    case 'workout':
      return '我在看你的当前训练。器械被占用、动作不会、时间不够，直接说，我会改当前和剩余动作，已完成的部分不会丢。'
    case 'plan':
      return '这一周哪里不合适？直接说，我会判断是只改今天、改长期偏好，还是换整份计划。'
    case 'profile':
      return '这里是我们共同的健身档案。你可以问我记住了什么，也可以直接说“以后都按这个来”。'
    case 'progress':
      return '我在看你的真实训练记录。完成率、疲劳、间隔，哪里需要调整，直接说一句。'
    default:
      return '今天想怎么练？时间、地点、器械、身体状态，直接说一句就行。'
  }
})

const draft = computed<PlanDraft | null>(() => result.value?.plan_draft || null)
const patchRequest = computed<PatchRequest | null>(() => result.value?.patch_request || null)
const guidance = computed<AgentGuidance[]>(() => result.value?.guidance || [])
const feedback = computed(() => result.value?.feedback || null)
const day = computed(() => result.value?.day || null)
const todayBrief = computed(() => result.value?.today_brief || null)
const briefSchedule = computed(() =>
  (todayBrief.value?.schedule || []).filter(
    (item) => item.activity && item.activity !== '按当前计划执行',
  ),
)
const mcdBlock = computed(() => todayBrief.value?.mcdonalds || null)
const mcdBudget = computed(() => mcdBlock.value?.budget || null)
const mcdCombo = computed(() => mcdBlock.value?.combo || null)
const mcdSuggestions = computed(() =>
  (mcdBlock.value?.options || []).map((item) => item.name),
)
const selectedMeals = ref<Record<string, string>>({})
const applyingMeals = ref(false)
const mcdOrderOpen = ref(false)
const mcdInstruction = ref('')
const cancellingMcdOrder = ref(false)
const MCD_ORDER_MESSAGES_KEY = 'planharness_mcd_order_messages'
const mcdRequestMessageId = ref('')
function readMcdOrdersByMessage(): Record<string, McdOrder> {
  try {
    const stored = uni.getStorageSync(MCD_ORDER_MESSAGES_KEY)
    return stored && typeof stored === 'object' ? stored as Record<string, McdOrder> : {}
  } catch {
    return {}
  }
}
const mcdOrdersByMessage = ref<Record<string, McdOrder>>(readMcdOrdersByMessage())
for (const [messageId, order] of Object.entries(mcdOrdersByMessage.value)) {
  if (isMcdOrderExpired(order)) delete mcdOrdersByMessage.value[messageId]
}
function persistMcdOrdersByMessage() {
  try {
    uni.setStorageSync(MCD_ORDER_MESSAGES_KEY, mcdOrdersByMessage.value)
  } catch {
    // 本地缓存失败不影响当前会话
  }
}
const pendingDelete = ref<AgentConversation | null>(null)
const deletingConversation = ref(false)
const session = computed(() => result.value?.session || activeSession.value)
const scrollTop = ref(0)
const workspaceDomId = computed(() => `coach-ws-${pageKey.value}-${props.mode || 'modal'}`)
const hasRichResult = computed(() =>
  Boolean(
    draft.value ||
      patchRequest.value ||
      day.value ||
      session.value ||
      guidance.value.length ||
      feedback.value ||
      todayBrief.value ||
      result.value?.memories?.length ||
      result.value?.progress_insights ||
      result.value?.changes?.length ||
      result.value?.warnings?.length ||
      result.value?.actions?.length ||
      result.value?.mcd_order ||
      mcdOrderOpen.value ||
      messages.value.some((message) => Boolean(mcdOrdersByMessage.value[message.id])) ||
      (pageKey.value === 'today' && coachWorkspaceLastMcdOrder.value),
  ),
)
// 历史消息里点“查看”时，卡片要回到那条消息下面，而不是跑到整个对话最底部。
const visibleOrderMessageId = computed(() => {
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    const message = messages.value[index]
    if (mcdOrdersByMessage.value[message.id]) return message.id
  }
  return ''
})
const richMessageId = computed(() => {
  if (!hasRichResult.value) return ''
  if (richAnchorMessageId.value) return richAnchorMessageId.value
  if (!result.value && visibleOrderMessageId.value) return visibleOrderMessageId.value
  return messages.value[messages.value.length - 1]?.id || ''
})
const chatActions = computed(() =>
  (result.value?.actions || []).filter((action) => action.type !== 'quick_reply'),
)
const activeMcdOrder = computed(() => {
  const order = mcdOrdersByMessage.value[richMessageId.value]
  return order && !isMcdOrderExpired(order) ? order : null
})
function orderForMessage(messageId: string): McdOrder | null {
  const order = mcdOrdersByMessage.value[messageId]
  return order && !isMcdOrderExpired(order) ? order : null
}

let scrollTick = 0
let bottomScrollToken = 0

function chatScroller(): HTMLElement | null {
  if (typeof document === 'undefined') return null
  const root = document.getElementById(workspaceDomId.value)
  const scrollers = root?.querySelectorAll('.workspace-scroll .uni-scroll-view')
  return scrollers && scrollers.length
    ? scrollers[scrollers.length - 1] as HTMLElement
    : null
}

function applyBottomScroll() {
  // scroll-view 的 scroll-into-view 在动态插入附件卡片时偶尔停在中间；
  // 用一个不断增大的 scroll-top 让它稳定夹到最底部。
  scrollTick += 1
  scrollTop.value = 1_000_000 + scrollTick
  // H5 下 uni scroll-view 的 scroll-top 有时在内容高度变化前就应用了，
  // 直接同步一次 DOM 滚动位置，保证进入页面时看到的是最新消息。
  const scroller = chatScroller()
  if (scroller) scroller.scrollTop = scroller.scrollHeight
}

async function scrollChatToBottom() {
  const token = ++bottomScrollToken
  await nextTick()
  if (token !== bottomScrollToken) return
  applyBottomScroll()
  // 图片、附件卡片和 rich result 会在首帧后继续撑高内容，再补两次。
  for (const delay of [80, 240]) {
    setTimeout(() => {
      if (token === bottomScrollToken) applyBottomScroll()
    }, delay)
  }
}

async function scrollChatToLatestCoach() {
  bottomScrollToken += 1
  await nextTick()
  await nextTick()
  // 等 today_brief 附件卡片完成布局后再取位置
  await new Promise((resolve) => setTimeout(resolve, 80))
  const scroller = chatScroller()
  const rows = scroller?.querySelectorAll?.('.chat-row.agent')
  const row = rows && rows.length ? rows[rows.length - 1] as HTMLElement : null
  if (scroller && row) {
    const top = row.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop
    const target = Math.max(0, top - 10)
    scroller.scrollTop = target
    // 同步绑定的 prop，避免后续重渲染把 scroll-view 又拉回旧的底部值
    scrollTop.value = target
    return
  }
  void scrollChatToBottom()
}

async function scrollToRichCard(messageId: string) {
  if (!messageId || typeof document === 'undefined') return
  bottomScrollToken += 1
  await nextTick()
  await new Promise((resolve) => setTimeout(resolve, 60))
  const card = document.getElementById(`coach-rich-${messageId}`)
  const scroller = chatScroller()
  if (!card || !scroller) return
  const top = card.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop
  const target = Math.max(0, top - 8)
  scroller.scrollTop = target
  scrollTop.value = target
}

function showMessageTime(index: number): boolean {
  const current = messages.value[index]?.at
  if (!current) return false
  const currentTime = Date.parse(current)
  if (Number.isNaN(currentTime)) return false
  const previous = messages.value[index - 1]?.at
  if (!previous) return true
  const previousTime = Date.parse(previous)
  return Number.isNaN(previousTime) || currentTime - previousTime >= 5 * 60 * 1000
}

function messageTime(at?: string): string {
  if (!at) return ''
  const time = new Date(at)
  if (Number.isNaN(time.getTime())) return ''
  return time.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

function nowId(): string {
  return `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`
}

function pushMessage(role: 'user' | 'agent', text: string, attachment?: CoachAttachment): string {
  if (!text.trim()) return ''
  const id = nowId()
  pushCoachMessage({ id, role, text, at: new Date().toISOString(), attachment })
  return id
}

function attachmentFor(next: AgentChatResult): CoachAttachment | undefined {
  if (next.mcd_order) {
    return {
      kind: '点餐',
      title: next.mcd_order.instruction || '麦当劳点餐',
      detail: [next.mcd_order.city, next.mcd_order.store].filter(Boolean).join(' · ') || '可查看门店、菜单和订单',
      result: next,
    }
  }
  if (next.plan_draft) {
    return {
      kind: '计划草案',
      title: next.plan_draft.summary || next.plan_draft.question || '新的训练计划',
      detail: next.plan_draft.status === 'ready' ? '确认后才会生成新版本' : '需要补充信息',
      result: next,
    }
  }
  if (next.today_brief) {
    return {
      kind: '今日安排',
      title: next.today_brief.summary || '今天的训练与餐食',
      detail: `${(next.today_brief.meals || []).length} 餐 · 可查看替换和点餐`,
      result: next,
    }
  }
  if (next.patch_request) {
    return {
      kind: '计划改动',
      title: next.patch_request.draft?.summary || next.patch_request.question || '待确认的改动',
      detail: '确认后才会生效',
      result: next,
    }
  }
  if (next.day) {
    return { kind: '今日调整', title: next.day.summary || '今天的临时安排', result: next }
  }
  if (next.feedback) {
    return { kind: '训练记录', title: `本次完成 ${next.feedback.completion_pct.toFixed(0)}%`, result: next }
  }
  if (next.guidance?.length) {
    return { kind: '动作指导', title: next.guidance[0].name, result: next }
  }
  if (next.progress_insights) {
    return { kind: '训练进展', title: '最近训练数据与发现', result: next }
  }
  if (next.session) {
    return { kind: '当前训练', title: next.session.current_item?.name || '训练进行中', result: next }
  }
  return undefined
}

function showAttachment(attachment: CoachAttachment, messageId = '') {
  if (!attachment.result) return
  const saved = attachment.result as AgentChatResult
  result.value = saved
  richCollapsed.value = false
  richAnchorMessageId.value = messageId
  selectedMeals.value = {}
  if (saved.mcd_order) {
    mcdInstruction.value = saved.mcd_order.instruction || ''
    mcdOrderOpen.value = true
    mcdRequestMessageId.value = messageId
  } else {
    mcdOrderOpen.value = false
  }
  void nextTick(() => scrollToRichCard(messageId))
}

function newThreadId(): string {
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
}

function conversationTime(at?: string): string {
  if (!at) return ''
  const time = new Date(at)
  if (Number.isNaN(time.getTime())) return ''
  const now = new Date()
  if (time.toDateString() === now.toDateString()) {
    return time.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  }
  return `${time.getMonth() + 1}月${time.getDate()}日`
}

async function loadConversations() {
  try {
    coachWorkspaceConversations.value = await api.listAgentConversations()
  } catch {
    // 列表拿不到不影响当前对话
  }
}

async function loadHistory(thread = coachWorkspaceThreadId.value, force = false) {
  if (!thread) return
  if (!force && coachWorkspaceHistoryThread.value === thread) return
  coachWorkspaceHistoryThread.value = thread
  try {
    const rows = await api.agentMessages(50, thread)
    if (rows.length) {
      messages.value = rows.map((item: AgentMessageItem) => {
        const saved = (item.result || null) as AgentChatResult | null
        const attachment = saved && item.role === 'agent'
          ? attachmentFor(saved)
          : undefined
        return {
          id: item.id,
          role: item.role,
          text: item.text,
          at: item.created_at,
          attachment,
        }
      })
      const lastOrder = coachWorkspaceLastMcdOrder.value
      const alreadyMapped = lastOrder
        ? Object.values(mcdOrdersByMessage.value).some(
          (order) => order.order_id === lastOrder.order_id,
        )
        : false
      if (lastOrder && !alreadyMapped) {
        const target = [...messages.value].reverse().find((message) => {
          const saved = message.attachment?.result as AgentChatResult | undefined
          return Boolean(saved?.mcd_order)
        })
        if (target) {
          mcdOrdersByMessage.value = {
            ...mcdOrdersByMessage.value,
            [target.id]: lastOrder,
          }
          persistMcdOrdersByMessage()
        }
      }
      return
    }
  } catch {
    // 历史拿不到不影响新对话
  }
  if (!messages.value.length) pushMessage('agent', greeting.value)
}

async function ensureConversation() {
  await loadConversations()
  if (!coachWorkspaceThreadId.value) {
    coachWorkspaceThreadId.value = coachWorkspaceConversations.value[0]?.thread || newThreadId()
  }
  await loadHistory(coachWorkspaceThreadId.value)
}

function newConversation() {
  const thread = newThreadId()
  coachWorkspaceThreadId.value = thread
  coachWorkspaceHistoryThread.value = thread
  messages.value = []
  result.value = null
  richAnchorMessageId.value = ''
  richCollapsed.value = false
  mcdRequestMessageId.value = ''
  activeSession.value = null
  selectedMeals.value = {}
  mcdOrderOpen.value = false
  mcdInstruction.value = ''
  error.value = ''
  input.value = ''
  pushMessage('agent', greeting.value)
  coachWorkspaceConversations.value = [
    {
      thread,
      title: '新对话',
      preview: '开始聊点别的',
      message_count: 0,
      updated_at: new Date().toISOString(),
    },
    ...coachWorkspaceConversations.value.filter(
      (item) => item.thread !== thread && item.message_count > 0,
    ),
  ]
  void scrollChatToBottom()
}

function deleteConversation(item: AgentConversation) {
  pendingDelete.value = item
}

async function confirmDeleteConversation() {
  const item = pendingDelete.value
  if (!item || deletingConversation.value) return
  deletingConversation.value = true
  try {
    await api.deleteAgentConversation(item.thread)
    coachWorkspaceConversations.value = coachWorkspaceConversations.value.filter(
      (row) => row.thread !== item.thread,
    )
    if (coachWorkspaceThreadId.value === item.thread) {
      const next = coachWorkspaceConversations.value[0]
      if (next) await selectConversation(next)
      else newConversation()
    }
    pendingDelete.value = null
  } catch (err) {
    uni.showToast({ title: describeError(err), icon: 'none' })
  } finally {
    deletingConversation.value = false
  }
}

async function selectConversation(item: AgentConversation) {
  if (item.thread === coachWorkspaceThreadId.value) {
    // 再点当前会话也回到最新，而不是停在用户刚才翻到的位置
    await scrollChatToBottom()
    return
  }
  coachWorkspaceThreadId.value = item.thread
  coachWorkspaceHistoryThread.value = ''
  messages.value = []
  result.value = null
  richAnchorMessageId.value = ''
  richCollapsed.value = false
  mcdRequestMessageId.value = ''
  selectedMeals.value = {}
  mcdOrderOpen.value = false
  mcdInstruction.value = ''
  error.value = ''
  input.value = ''
  await loadHistory(item.thread, true)
  await scrollChatToBottom()
}

watch(
  isOpen,
  async (open) => {
    if (!open) {
      lastAutoText.value = ''
      return
    }
    error.value = ''
    confirming.value = false
    coachWorkspaceLastPage.value = pageKey.value
    coachWorkspaceLastContext.value = props.context || {}
    input.value = props.initialText || ''
    await ensureConversation()
    if (props.sessionId) {
      try {
        activeSession.value = await api.getWorkout(props.sessionId)
      } catch {
        activeSession.value = null
      }
    }
    if (props.initialText && props.autoSend) {
      lastAutoText.value = props.initialText
      await nextTick()
      await send(props.initialText)
      return
    }
    // 进入页面时直接停在最新一条，而不是从这轮对话的开头开始看
    await scrollChatToBottom()
  },
  { immediate: true },
)

watch(
  [() => messages.value.length, () => busy.value, () => result.value, () => isOpen.value],
  () => {
    if (!isOpen.value) return
    if (richAnchorMessageId.value
      && richAnchorMessageId.value !== messages.value[messages.value.length - 1]?.id) {
      void scrollToRichCard(richAnchorMessageId.value)
      return
    }
    if (!busy.value && result.value && todayBrief.value) {
      // 今日安排附件很长：定位到最新回复开头，避免只看到最后一份晚餐
      void scrollChatToLatestCoach()
      return
    }
    void scrollChatToBottom()
  },
)

async function send(text?: string) {
  const raw = (text ?? input.value).trim()
  if (!raw || busy.value) return
  const value = redactMcdToken(raw)
  input.value = ''
  // 发新消息时先收起上一次“查看”的旧卡片，再滚到新消息；旧结果本身仍有附件摘要
  result.value = null
  richAnchorMessageId.value = ''
  richCollapsed.value = false
  mcdRequestMessageId.value = ''
  selectedMeals.value = {}
  mcdOrderOpen.value = false
  mcdInstruction.value = ''
  error.value = ''
  pushMessage('user', value)
  busy.value = true
  error.value = ''
  try {
    const next = await api.agentChat(
      value,
      pageKey.value,
      props.date || todayStr(),
      props.sessionId || '',
      props.context || {},
      coachWorkspaceThreadId.value,
    )
    result.value = next
    selectedMeals.value = {}
    if (next.mcd_order) {
      mcdInstruction.value = next.mcd_order.instruction || value
      mcdOrderOpen.value = true
    }
    const agentMessageId = pushMessage(
      'agent', next.reply || '收到。', attachmentFor(next),
    )
    richAnchorMessageId.value = agentMessageId
    if (next.mcd_order) mcdRequestMessageId.value = agentMessageId
    void loadConversations()
    if (next.warnings?.length) {
      // 警告只展示，不改写回复；用户需要看到"长期计划没被改"这类边界
    }
    emit('changed')
  } catch (err) {
    error.value = describeError(err)
    pushMessage('agent', `这次没成功：${error.value}`)
  } finally {
    busy.value = false
  }
}

const MEAL_LABELS: Record<string, string> = {
  breakfast: '早餐',
  lunch: '午餐',
  dinner: '晚餐',
  snack: '加餐',
}

function mealLabel(slot: string): string {
  return MEAL_LABELS[slot] || slot
}

function mealLinks(links: Record<string, string>): Array<{ label: string; url: string }> {
  return [
    { label: '京东', url: links.jd || '' },
    { label: '淘宝', url: links.taobao || '' },
    { label: '美团', url: links.meituan || '' },
  ].filter((item) => item.url)
}

function toggleMeal(slot: string, title: string) {
  selectedMeals.value = {
    ...selectedMeals.value,
    [slot]: selectedMeals.value[slot] === title ? '' : title,
  }
}

async function applyMeals() {
  const brief = todayBrief.value
  const runId = result.value?.today_brief_run_id || ''
  if (!brief || !runId || applyingMeals.value) return
  const selections = (brief.meals || [])
    .map((meal) => {
      const title = selectedMeals.value[meal.slot]
      const option = (meal.options || []).find((item) => item.title === title)
      return option ? { slot: meal.slot, ...option } : null
    })
    .filter(Boolean)
  if (!selections.length) {
    uni.showToast({ title: '先选每餐想要的选项', icon: 'none' })
    return
  }
  applyingMeals.value = true
  try {
    const applied = await api.applyAgentMeals(
      runId, selections, props.date || todayStr(),
    )
    uni.showToast({ title: `已替换今日餐食（v${applied.new_version}）`, icon: 'none' })
    emit('changed')
  } catch (err) {
    uni.showToast({ title: describeError(err), icon: 'none' })
  } finally {
    applyingMeals.value = false
  }
}

function openMcdOrder() {
  mcdInstruction.value = ''
  mcdOrderOpen.value = true
  void scrollChatToBottom()
}

function openMcdLink() {
  const url = mcdBlock.value?.links?.order || 'https://www.mcd.cn/'
  openSource(url)
}

function redactMcdToken(text: string): string {
  const match = text.match(
    /(?:mcp\s*token|mcptoken|token)\s*(?:是|为|[:：=])?\s*([A-Za-z0-9._~-]{8,})/i,
  )
  if (!match) return text
  setStoredMcdToken(match[1])
  return text.replace(match[0], 'MCP Token 已保存')
}

function findMcdRequestMessageId(): string {
  if (mcdRequestMessageId.value
    && messages.value.some((message) => message.id === mcdRequestMessageId.value)) {
    return mcdRequestMessageId.value
  }
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    const saved = messages.value[index].attachment?.result as AgentChatResult | undefined
    if (saved?.mcd_order) return messages.value[index].id
  }
  return messages.value[messages.value.length - 1]?.id || ''
}

function onMcdOrderCreated(order: McdOrder) {
  const messageId = findMcdRequestMessageId()
  if (messageId) {
    mcdOrdersByMessage.value = { ...mcdOrdersByMessage.value, [messageId]: order }
    persistMcdOrdersByMessage()
    mcdRequestMessageId.value = messageId
    richAnchorMessageId.value = messageId
  }
  setLastMcdOrder(order)
}

function removeMcdOrder(orderId: string) {
  const next: Record<string, McdOrder> = {}
  let removed = false
  for (const [messageId, order] of Object.entries(mcdOrdersByMessage.value)) {
    if (order.order_id === orderId) removed = true
    else next[messageId] = order
  }
  if (removed) {
    mcdOrdersByMessage.value = next
    persistMcdOrdersByMessage()
  }
}

function onMcdOrderCancelled(orderId: string) {
  removeMcdOrder(orderId)
  if (coachWorkspaceLastMcdOrder.value?.order_id === orderId) {
    setLastMcdOrder(null)
  }
}

function openLatestMcdOrder() {
  const messageId = visibleOrderMessageId.value || mcdRequestMessageId.value
  if (messageId && mcdOrdersByMessage.value[messageId]) {
    showMcdOrder(messageId)
    return
  }
  if (coachWorkspaceLastMcdOrder.value) openSavedMcdPay(coachWorkspaceLastMcdOrder.value)
}

function showMcdOrder(messageId: string) {
  if (!mcdOrdersByMessage.value[messageId]) return
  result.value = null
  selectedMeals.value = {}
  mcdOrderOpen.value = false
  richAnchorMessageId.value = messageId
  mcdRequestMessageId.value = messageId
  void nextTick(() => scrollToRichCard(messageId))
}

function openSavedMcdPay(order: McdOrder | null = activeMcdOrder.value) {
  if (order?.pay_url) openSource(order.pay_url)
}

async function cancelSavedMcdOrder(order: McdOrder | null = activeMcdOrder.value) {
  if (!order || cancellingMcdOrder.value) return
  cancellingMcdOrder.value = true
  try {
    await api.mcdCancelOrder(order.order_id, getStoredMcdToken())
    removeMcdOrder(order.order_id)
    if (coachWorkspaceLastMcdOrder.value?.order_id === order.order_id) {
      setLastMcdOrder(null)
    }
    uni.showToast({ title: '订单已取消', icon: 'none' })
  } catch (err) {
    uni.showToast({ title: describeError(err), icon: 'none' })
  } finally {
    cancellingMcdOrder.value = false
  }
}

function tipExerciseTerm(text: string): string {
  return exerciseSearchTerm(text)
}

function openTipExercise(text: string) {
  openExerciseDbTerm(tipExerciseTerm(text))
}

function guidanceExerciseTerm(item: AgentGuidance): string {
  const sourceTitle = item.sources?.[0]?.title || ''
  const english = sourceTitle.match(/[（(]([^）)]+)[）)]/)
  if (english && /[A-Za-z]/.test(english[1])) return english[1].trim()
  return exerciseSearchTerm(item.name) || exerciseSearchTerm(sourceTitle)
}

function openGuidanceExercise(item: AgentGuidance) {
  openExerciseDbTerm(guidanceExerciseTerm(item))
}

function isSpecificSource(url: string): boolean {
  if (!url) return false
  try {
    const path = new URL(url).pathname.replace(/\/+$/, '')
    return path.length > 0 && path !== '/live-well/exercise' 
  } catch {
    return false
  }
}

function citationFor(id: string) {
  return (todayBrief.value?.citations || []).find((item) => item.id === id)
}

function markDraftAttachments(
  draftId: string, status: 'confirmed' | 'rejected', version = 0,
) {
  coachWorkspaceMessages.value = coachWorkspaceMessages.value.map((message) => {
    const attachment = message.attachment
    const saved = attachment?.result as AgentChatResult | undefined
    if (!attachment || !saved?.plan_draft || saved.plan_draft.draft_id !== draftId) {
      return message
    }
    const nextResult: AgentChatResult = { ...saved, plan_draft: null, actions: [] }
    return {
      ...message,
      attachment: {
        ...attachment,
        title: status === 'confirmed'
          ? `计划已生效 v${version}`
          : '计划草案已放弃',
        detail: status === 'confirmed'
          ? '新版本已生成，可去计划页查看'
          : '现有计划没有改动',
        result: nextResult,
      },
    }
  })
}

function statusLabel(status?: string): string {
  const labels: Record<string, string> = {
    ready: '待确认',
    needs_input: '需要补充',
    pending_approval: '待确认',
    committed: '已生效',
    rejected: '已放弃',
    superseded: '已被替换',
  }
  return status ? labels[status] || status : ''
}

function describeError(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return '请求失败'
}

async function runAction(action: AgentActionChip) {
  if (busy.value) return
  switch (action.type) {
    case 'quick_reply':
      if (action.text) await send(action.text)
      return
    case 'start_workout':
    case 'open_workout':
      emit('openWorkout', {
        sessionId: action.session_id || session.value?.session_id,
        date: action.date || props.date || todayStr(),
      })
      emit('close')
      return
    case 'confirm_plan': {
      const draftId = action.draft_id || draft.value?.draft_id
      if (!draftId) return
      confirming.value = true
      busy.value = true
      try {
        const confirmed = await api.confirmPlanDraft(draftId, 'Agent 对话确认')
        uni.showToast({ title: `计划已生效 v${confirmed.committed_version}`, icon: 'success' })
        markDraftAttachments(draftId, 'confirmed', confirmed.committed_version)
        result.value = result.value ? { ...result.value, plan_draft: null, actions: [] } : result.value
        pushMessage('agent', `计划已生效 v${confirmed.committed_version}，正在打开新计划页。`)
        emit('changed')
        if (pageKey.value === 'plan') {
          if (props.mode === 'modal') emit('close')
        } else {
          setTimeout(() => {
            uni.switchTab({ url: '/pages/plan/index' })
          }, 350)
        }
      } catch (err) {
        error.value = describeError(err)
      } finally {
        confirming.value = false
        busy.value = false
      }
      return
    }
    case 'reject_plan': {
      const draftId = action.draft_id || draft.value?.draft_id
      if (!draftId) return
      try {
        await api.rejectPlanDraft(draftId, '用户在对话里放弃')
        markDraftAttachments(draftId, 'rejected')
        result.value = result.value ? { ...result.value, plan_draft: null, actions: [] } : result.value
        pushMessage('agent', '这份草案已丢弃，现有计划不受影响。')
      } catch (err) {
        error.value = describeError(err)
      }
      return
    }
    case 'reset_day': {
      try {
        const next = await api.resetAgentDay(action.date || props.date || todayStr())
        result.value = result.value ? { ...result.value, day: next } : result.value
        uni.showToast({ title: '已恢复原计划', icon: 'none' })
        emit('changed')
      } catch (err) {
        error.value = describeError(err)
      }
      return
    }
    case 'open_memory':
      emit('openMemory')
      emit('close')
      return
    case 'open_progress':
      emit('openProgress')
      emit('close')
      return
    case 'view_today_brief':
      if (result.value?.today_brief) emit('openTodayBrief', result.value.today_brief)
      return
    case 'open_knowledge':
      knowledgeQuestion.value = action.question || '训练安全建议'
      knowledgeOpen.value = true
      return
    case 'approve_patch': {
      const requestId = action.request_id || patchRequest.value?.request_id
      if (!requestId) return
      busy.value = true
      try {
        const confirmed = await api.approvePatchRequest(requestId, 'Agent 对话确认')
        uni.showToast({ title: `已应用，v${confirmed.committed_version}`, icon: 'success' })
        result.value = result.value ? { ...result.value, patch_request: null, actions: [] } : result.value
        pushMessage('agent', `改动已生效，当前是 v${confirmed.committed_version}。`)
        emit('changed')
      } catch (err) {
        error.value = describeError(err)
      } finally {
        busy.value = false
      }
      return
    }
    case 'reject_patch': {
      const requestId = action.request_id || patchRequest.value?.request_id
      if (!requestId) return
      try {
        await api.rejectPatchRequest(requestId, '用户在对话里放弃')
        result.value = result.value ? { ...result.value, patch_request: null, actions: [] } : result.value
        pushMessage('agent', '这条改动已丢弃，原计划没变。')
      } catch (err) {
        error.value = describeError(err)
      }
      return
    }
    default:
      return
  }
}

function openSource(url: string) {
  if (!url) return
  const globalOpen = (globalThis as unknown as { open?: (u: string, t?: string) => unknown }).open
  if (typeof globalOpen === 'function') globalOpen(url, '_blank')
  else uni.setClipboardData({ data: url })
}

function applyGuidance(item: { name: string; cues: string[] }) {
  knowledgeQuestion.value = `${item.name} 的动作要领`
  knowledgeOpen.value = true
}

function close() {
  emit('close')
}

function focusInput() {
  // uni-app 没有跨端统一的 focus() 方法，滚动到工作区即可
  try {
    uni.pageScrollTo({ scrollTop: 0, duration: 150 })
  } catch {
    // ignore
  }
}

function showTodayBrief(output: TodayAgentResult, runId = '') {
  const synthetic = {
    intent: 'today_brief',
    intent_label: '今日安排',
    scope: 'today',
    reason: '查看已保存的今日安排',
    reply: output.summary || '这是今天已保存的安排。',
    changes: [],
    actions: [],
    day: null,
    session: null,
    plan_draft: null,
    patch_request: null,
    today_brief: output,
    today_brief_run_id: runId,
    memories: null,
    memory_saved: [],
    guidance: [],
    warnings: [],
    blocked: false,
  } as AgentChatResult
  result.value = synthetic
  selectedMeals.value = {}
  mcdOrderOpen.value = false
  richCollapsed.value = false
  // 优先回到最近一条“今日安排”消息，而不是在底部再造一条。
  const latestBrief = [...messages.value].reverse().find((message) => {
    const saved = message.attachment?.result as AgentChatResult | undefined
    return Boolean(saved?.today_brief)
  })
  if (latestBrief) {
    messages.value = messages.value.map((message) => {
      if (message.id !== latestBrief.id || !message.attachment) return message
      return {
        ...message,
        attachment: {
          ...message.attachment,
          title: output.summary || '今天已保存的训练与餐食',
          detail: `${(output.meals || []).length} 餐 · 可查看替换和点餐`,
          result: synthetic,
        },
      }
    })
    richAnchorMessageId.value = latestBrief.id
    void nextTick(() => scrollToRichCard(latestBrief.id))
    return
  }
  richAnchorMessageId.value = pushMessage('agent', synthetic.reply, {
    kind: '今日安排',
    title: output.summary || '今天已保存的训练与餐食',
    detail: `${(output.meals || []).length} 餐 · 可查看替换和点餐`,
    result: synthetic,
  })
  void scrollChatToBottom()
}

defineExpose({ sendText: send, focusInput, showTodayBrief, scrollToLatest: scrollChatToBottom })
</script>

<template>
  <view v-if="isOpen">
    <view v-if="mode !== 'inline'" class="workspace-mask" @click="close" />
    <view :id="workspaceDomId" class="workspace" :class="{ 'workspace-inline': mode === 'inline' }">
      <!-- 顶栏 -->
      <view class="workspace-head">
        <view class="workspace-brand">
          <view class="workspace-avatar">教</view>
          <view class="workspace-brand-text">
            <text class="workspace-title">你的健身教练</text>
            <text class="workspace-sub">同一个教练 · 共享记忆</text>
          </view>
        </view>
        <view class="workspace-head-right">
          <view class="workspace-online"><text class="workspace-online-dot" />在线</view>
          <text v-if="mode !== 'inline'" class="workspace-close" @click="close" aria-label="关闭">×</text>
        </view>
      </view>

      <view v-if="pendingDelete" class="conversation-confirm-mask" @click="pendingDelete = null">
        <view class="conversation-confirm-card" @click.stop>
          <text class="conversation-confirm-title">删除这个对话？</text>
          <text class="conversation-confirm-copy">「{{ pendingDelete.title }}」的消息会被删除，长期记忆和计划不受影响。</text>
          <view class="conversation-confirm-actions">
            <view class="btn-sm primary" @click="confirmDeleteConversation">
              {{ deletingConversation ? '删除中…' : '删除' }}
            </view>
            <view class="btn-sm" @click="pendingDelete = null">取消</view>
          </view>
        </view>
      </view>

      <view class="workspace-body">
        <!-- 左侧：可选对话列表 -->
        <view class="workspace-left">
          <view class="conversation-new" @click="newConversation">
            <text class="conversation-new-plus">＋</text>
            <text>新对话</text>
          </view>
          <text class="conversation-head">最近对话</text>
          <scroll-view class="conversation-list" scroll-y>
            <view
              v-for="item in coachWorkspaceConversations"
              :key="item.thread"
              class="conversation-item"
              :class="{ active: item.thread === coachWorkspaceThreadId }"
              @click="selectConversation(item)"
            >
              <view class="conversation-item-head">
                <text class="conversation-title">{{ item.title }}</text>
                <text class="conversation-time">{{ conversationTime(item.updated_at) }}</text>
                <text class="conversation-delete" @click.stop="deleteConversation(item)">×</text>
              </view>
              <text class="conversation-preview">{{ item.preview || '还没有消息' }}</text>
            </view>
            <view v-if="!coachWorkspaceConversations.length" class="conversation-empty">
              还没有历史对话
            </view>
          </scroll-view>
        </view>

        <!-- 中间：对话 -->
        <view class="workspace-chat">
          <scroll-view
            class="workspace-scroll"
            scroll-y
            :scroll-top="scrollTop"
          >
            <view class="chat-stream">
              <view class="chat-date"><text>今天</text></view>

            <template v-for="(message, index) in messages" :key="message.id">
              <view v-if="showMessageTime(index)" class="chat-time">{{ messageTime(message.at) }}</view>
              <view class="chat-row" :class="message.role">
                <view v-if="message.role === 'agent'" class="chat-avatar coach">教</view>
                <view class="chat-bubble" :class="message.role">
                  <text class="chat-text">{{ message.text }}</text>
                </view>
                <view v-if="message.role === 'user'" class="chat-avatar user">我</view>
              </view>
              <view
                v-if="message.attachment && (message.id !== messages[messages.length - 1]?.id || !hasRichResult)"
                class="chat-attachment"
                @click="showAttachment(message.attachment, message.id)"
              >
                <view class="chat-attachment-main">
                  <text class="chat-attachment-kind">{{ message.attachment.kind }}</text>
                  <text class="chat-attachment-title">{{ message.attachment.title }}</text>
                  <text v-if="message.attachment.detail" class="chat-attachment-detail">
                    {{ message.attachment.detail }}
                  </text>
                </view>
                <text class="chat-attachment-open">查看 →</text>
              </view>
              <view
                v-if="orderForMessage(message.id) && message.id !== richMessageId"
                class="chat-attachment order-attachment"
                @click="showMcdOrder(message.id)"
              >
                <view class="chat-attachment-main">
                  <text class="chat-attachment-kind">麦当劳订单</text>
                  <text class="chat-attachment-title">{{ orderForMessage(message.id)?.store_name }}</text>
                  <text class="chat-attachment-detail">
                    ¥{{ orderForMessage(message.id)?.total.toFixed(2) }} ·
                    {{ orderForMessage(message.id)?.status }} ·
                    {{ orderForMessage(message.id)?.items.length }} 件
                  </text>
                </view>
                <text class="chat-attachment-open">查看 →</text>
              </view>
            <view v-if="hasRichResult && message.id === richMessageId" :id="'coach-rich-' + message.id" class="chat-rich-row">
              <view v-show="!richCollapsed" class="chat-rich">
                <!-- 计划草案 -->
                <view v-if="draft" class="result-card">
              <view class="result-head">
                <view>
                  <text class="result-kicker">计划草案</text>
                  <text class="result-title">{{ draft.summary || draft.question }}</text>
                </view>
                <text class="result-pill">{{ statusLabel(draft.status) }}</text>
              </view>
              <view v-if="draft.status === 'ready'" class="result-stats">
                <view class="result-stat">
                  <text class="result-stat-value">{{ draft.nutrition?.target_kcal || '—' }}</text>
                  <text class="result-stat-label">kcal / 天</text>
                </view>
                <view class="result-stat">
                  <text class="result-stat-value">{{ (draft.days || []).length }}</text>
                  <text class="result-stat-label">训练日 / 周</text>
                </view>
                <view class="result-stat">
                  <text class="result-stat-value">{{ (draft.days || []).reduce((sum, d) => sum + (d.actions?.length || 0), 0) }}</text>
                  <text class="result-stat-label">动作 / 周</text>
                </view>
              </view>
              <view v-if="draft.adaptive?.load_reduced" class="result-warn">
                最近连续 {{ draft.adaptive.consecutive_incomplete }} 次没完成，这一版已主动降低负担。
              </view>
              <view class="draft-week">
                <view v-for="weekDay in draft.days || []" :key="weekDay.day_index" class="draft-day">
                  <view class="draft-day-head">
                    <text class="draft-day-title">第 {{ weekDay.day_index }} 天 · {{ weekDay.focus || '训练' }}</text>
                    <text class="draft-day-count">{{ (weekDay.actions || []).length }} 个动作</text>
                  </view>
                  <text class="draft-day-actions">{{ (weekDay.actions || []).map((a) => actionLabel(a)).join('、') }}</text>
                </view>
              </view>
            </view>

            <!-- 计划改动草案 -->
            <view v-if="patchRequest" class="result-card">
              <view class="result-head">
                <view>
                  <text class="result-kicker">计划改动预览</text>
                  <text class="result-title">{{ patchRequest.draft?.summary || patchRequest.question || '等待确认' }}</text>
                </view>
                <text class="result-pill">{{ statusLabel(patchRequest.status) }}</text>
              </view>
              <view v-for="(op, index) in patchRequest.draft?.operations || []" :key="index" class="result-line">
                · {{ describeOperation(op) }}
                <text v-if="op.reason" class="result-sub">（{{ op.reason }}）</text>
              </view>
              <view v-for="(risk, index) in patchRequest.draft?.risks || []" :key="`risk-${index}`" class="result-line warn">
                ⚠ {{ risk }}
              </view>
              <text class="result-sub">确认后才会生成新版本；不确认的话长期计划不会变。</text>
            </view>

            <!-- 今日调整 -->
            <view v-if="day" class="result-card">
              <view class="result-head">
                <view>
                  <text class="result-kicker">今日有效安排</text>
                  <text class="result-title">{{ day.summary || '按当前计划执行' }}</text>
                </view>
                <text class="result-pill">{{ day.source === 'agent' ? '临时调整' : '原计划' }}</text>
              </view>
              <view v-for="item in day.actions" :key="item.slot_key" class="result-action-row">
                <text class="result-action-name">{{ item.name }}</text>
                <text class="result-action-meta">
                  {{ item.sets ? `${item.sets} 组` : '' }}
                  {{ item.duration_min ? ` · ${item.duration_min} 分钟` : '' }}
                </text>
              </view>
              <text class="result-sub">只改今天；长期 PlanVersion 没有变化。</text>
            </view>

            <!-- 今日安排与餐食：直接作为教练回复的一部分显示，不再弹独立面板 -->
            <view v-if="todayBrief" class="result-card today-brief-card">
              <view class="result-head">
                <view>
                  <text class="result-kicker">今日安排</text>
                  <text class="result-title">训练、餐食和外卖入口</text>
                </view>
                <text class="result-pill">{{ (todayBrief.meals || []).length }} 餐</text>
              </view>

              <view v-if="todayBrief.next_training" class="brief-next-training">
                <text class="result-kicker">下一次训练</text>
                <text class="brief-next-title">{{ todayBrief.next_training.label }}</text>
                <view class="brief-next-actions">
                  <text
                    v-for="action in todayBrief.next_training.actions"
                    :key="action.action_ref"
                    class="brief-next-action"
                  >
                    <text v-if="action.time">{{ action.time }} · </text>{{ action.name }}
                    <text v-if="action.sets"> · {{ action.sets }} 组</text>
                    <text v-else-if="action.duration_min"> · {{ action.duration_min }} 分钟</text>
                  </text>
                </view>
              </view>

              <view v-if="briefSchedule.length && !todayBrief.next_training" class="brief-schedule">
                <view v-for="(item, index) in briefSchedule" :key="index" class="brief-schedule-row">
                  <text class="brief-time">{{ item.time }}</text>
                  <view class="brief-schedule-copy">
                    <text class="brief-activity">{{ item.activity }}</text>
                    <text class="brief-why">{{ item.why }}</text>
                  </view>
                </view>
              </view>

              <view v-for="meal in todayBrief.meals || []" :key="meal.slot" class="brief-meal">
                <view class="brief-meal-head">
                  <text class="brief-meal-title">{{ mealLabel(meal.slot) }}</text>
                  <text class="brief-meal-target">目标 {{ meal.target_kcal }} kcal</text>
                </view>
                <view
                  v-for="option in meal.options || []"
                  :key="option.title"
                  class="brief-option"
                  :class="{ picked: selectedMeals[meal.slot] === option.title }"
                  @click="toggleMeal(meal.slot, option.title)"
                >
                  <view class="brief-option-head">
                    <text class="brief-option-title">
                      {{ option.title }}
                      <text v-if="selectedMeals[meal.slot] === option.title" class="brief-picked">已选</text>
                    </text>
                    <text class="brief-option-kcal">{{ option.kcal }} kcal</text>
                  </view>
                  <text class="brief-option-meta">
                    蛋白 {{ option.protein_g }}g · {{ option.reason }}
                  </text>
                  <text v-if="option.items?.length" class="brief-option-items">
                    {{ option.items.map((item) => `${item.name} ${item.grams}g`).join(' · ') }}
                  </text>
                  <view v-if="mealLinks(option.links).length" class="brief-links">
                    <text
                      v-for="link in mealLinks(option.links)"
                      :key="link.label"
                      class="brief-link"
                      @click.stop="openSource(link.url)"
                    >
                      {{ link.label }}
                    </text>
                  </view>
                </view>
              </view>

              <view v-if="todayBrief.tips?.length" class="brief-tips">
                <text class="result-kicker">动作与注意事项</text>
                <view v-for="(tip, index) in todayBrief.tips" :key="index" class="brief-tip">
                  <text class="brief-tip-text">{{ tip.text }}</text>
                  <view class="brief-tip-links">
                    <text
                      v-if="tipExerciseTerm(tip.text)"
                      class="brief-link"
                      @click.stop="openTipExercise(tip.text)"
                    >
                      动作库 · {{ tipExerciseTerm(tip.text) }} →
                    </text>
                    <template v-else>
                      <template v-for="sourceId in tip.source_ids || []" :key="sourceId">
                        <text
                          v-if="isSpecificSource(citationFor(sourceId)?.source_url || '')"
                          class="brief-link"
                          @click.stop="openSource(citationFor(sourceId)?.source_url || '')"
                        >
                          依据 · {{ citationFor(sourceId)?.title || sourceId }} →
                        </text>
                        <text v-else class="brief-source-static">
                          {{ citationFor(sourceId)?.title || sourceId }}
                        </text>
                      </template>
                    </template>
                  </view>
                </view>
              </view>

              <view class="brief-apply-row">
                <text class="result-sub" style="margin-top: 0">
                  点选每餐的替换选项，确认后会更新今天的餐食。
                </text>
                <view
                  class="btn-sm primary"
                  :class="{ disabled: applyingMeals }"
                  @click="applyMeals"
                >
                  {{ applyingMeals ? '保存中…' : '应用选中的餐食' }}
                </view>
              </view>

              <text v-if="todayBrief.disclaimer" class="result-sub">{{ todayBrief.disclaimer }}</text>
            </view>

            <!-- 麦当劳官方营养库 / MCP 点餐 -->
            <view v-if="mcdBlock" class="result-card mcd-inline-card">
              <view class="mcd-inline-head">
                <view class="mcd-mark">M</view>
                <view class="mcd-inline-titles">
                  <text class="result-title">麦当劳 · 官方营养库</text>
                  <text class="result-sub" style="margin-top: 0">留够额度再点，数字来自官方营养表</text>
                </view>
              </view>

              <view v-if="mcdBlock.status !== 'ok'" class="result-sub">
                这次没取到麦当劳数据（{{ mcdBlock.error || mcdBlock.status }}），其余推荐不受影响。
              </view>
              <template v-else>
                <view v-if="mcdBudget" class="mcd-inline-budget">
                  <view class="result-stat">
                    <text class="result-stat-value">{{ mcdBudget.remaining_kcal }}</text>
                    <text class="result-stat-label">今天还剩 kcal</text>
                  </view>
                  <view class="result-stat">
                    <text class="result-stat-value">{{ mcdBudget.remaining_protein_g }}g</text>
                    <text class="result-stat-label">蛋白缺口</text>
                  </view>
                  <text class="result-sub" style="margin-top: 0">
                    计划 {{ mcdBudget.plan_kcal }} / 目标 {{ mcdBudget.target_kcal }} kcal
                  </text>
                </view>

                <view v-if="mcdCombo" class="mcd-inline-combo">
                  <text class="result-sub" style="margin-top: 0">想点麦当劳可以这么凑</text>
                  <text class="mcd-combo-line">
                    {{ mcdCombo.items.map((item) => `${item.name}（${item.kcal}）`).join(' + ') }}
                    = {{ mcdCombo.kcal }} kcal · 蛋白 {{ mcdCombo.protein_g }} g
                  </text>
                </view>

                <view
                  v-for="item in mcdBlock.options || []"
                  :key="item.name"
                  class="mcd-inline-option"
                  @click="openMcdLink"
                >
                  <view class="mcd-inline-option-main">
                    <text class="mcd-inline-option-name">{{ item.name }}</text>
                    <text class="result-sub" style="margin-top: 0">{{ item.why }}</text>
                  </view>
                  <view class="mcd-inline-option-right">
                    <text class="result-sub" style="margin-top: 0">{{ item.kcal }} kcal · 蛋白 {{ item.protein_g }}g</text>
                    <text class="result-link">去点餐 →</text>
                  </view>
                </view>

                <view class="result-actions">
                  <view class="ws-action-btn primary" @click="openMcdLink">去麦当劳点餐</view>
                  <view class="ws-action-btn" @click="openMcdOrder">让教练帮我点好（到付款前）→</view>
                </view>
                <text class="result-sub">
                  {{ mcdBlock.order_hint || '登录 → 选门店 → 加购物车 → 想付再付' }}。
                  我们只推荐、不代付也不收款。
                </text>
              </template>
            </view>

            <!-- 当前计划里带上已生效的麦当劳订单摘要 -->
            <view
              v-if="todayBrief && coachWorkspaceLastMcdOrder && result?.reason === '查看已保存的今日安排'"
              class="result-card plan-order-note"
            >
              <view class="result-head">
                <view>
                  <text class="result-kicker">当前麦当劳订单</text>
                  <text class="result-title">{{ coachWorkspaceLastMcdOrder.store_name }}</text>
                </view>
                <text class="result-pill">{{ coachWorkspaceLastMcdOrder.status }}</text>
              </view>
              <text class="result-sub">
                ¥{{ Number(coachWorkspaceLastMcdOrder.total || 0).toFixed(2) }} ·
                {{ coachWorkspaceLastMcdOrder.items.map((item) => `${item.name} × ${item.quantity}`).join('、') }}
              </text>
              <text class="result-link" @click="openLatestMcdOrder">查看订单详情 →</text>
            </view>

            <!-- 已生成的麦当劳订单：换消息/看已保存餐食时也保留 -->
            <view
              v-if="activeMcdOrder && !mcdOrderOpen"
              class="result-card saved-mcd-order"
            >
              <view class="result-head">
                <view>
                  <text class="result-kicker">麦当劳订单已生成</text>
                  <text class="result-title">{{ activeMcdOrder.store_name }}</text>
                </view>
                <text class="result-pill">{{ activeMcdOrder.status }}</text>
              </view>
              <text class="result-sub">
                ¥{{ activeMcdOrder.total.toFixed(2) }} · 订单号 {{ activeMcdOrder.order_id }}
                <text v-if="activeMcdOrder.expire_at"> · {{ activeMcdOrder.expire_at }} 前有效</text>
              </text>
              <view v-for="item in activeMcdOrder.items" :key="item.name" class="result-action-row">
                <text class="result-action-name">{{ item.name }} × {{ item.quantity }}</text>
                <text class="result-action-meta">¥{{ item.price.toFixed(1) }}</text>
              </view>
              <view class="result-actions">
                <view class="ws-action-btn primary" @click="openSavedMcdPay(activeMcdOrder)">去麦当劳付款 →</view>
                <view class="ws-action-btn" @click="cancelSavedMcdOrder(activeMcdOrder)">
                  {{ cancellingMcdOrder ? '取消中…' : '取消订单' }}
                </view>
              </view>
              <text class="result-sub">付款在麦当劳自己的页面完成；我们只建单到付款前，不代付。</text>
            </view>

            <!-- 训练会话 -->
            <view v-if="session" class="result-card">
              <view class="result-head">
                <view>
                  <text class="result-kicker">当前训练</text>
                  <text class="result-title">{{ session.current_item?.name || '训练进行中' }}</text>
                </view>
                <text class="result-pill">{{ session.progress.percent }}%</text>
              </view>
              <view class="result-progress">
                <view class="result-progress-fill" :style="{ width: `${session.progress.percent}%` }" />
              </view>
              <text class="result-sub">
                已完成 {{ session.progress.done_sets }}/{{ session.progress.total_sets }} 组
                · 剩余约 {{ session.progress.remaining_minutes }} 分钟
              </text>
            </view>

            <!-- 动作指导 -->
            <view v-if="guidance.length" class="result-card">
              <text class="result-kicker">动作指导</text>
              <view v-for="item in guidance" :key="item.name" class="guidance-item">
                <view class="result-head">
                  <text class="result-title">{{ item.name }}</text>
                  <text class="result-link" @click="applyGuidance(item)">完整要领 →</text>
                </view>
                <view v-for="(cue, index) in item.cues" :key="index" class="result-line">· {{ cue }}</view>
                <view v-if="guidanceExerciseTerm(item)" class="guidance-sources">
                  <text class="guidance-hint">点击复制动作名，去动作库网页粘贴搜索</text>
                  <text class="result-link" @click="openGuidanceExercise(item)">
                    动作库 · {{ guidanceExerciseTerm(item) }} →
                  </text>
                </view>
              </view>
            </view>

            <!-- 完成反馈 -->
            <view v-if="feedback" class="result-card">
              <view class="result-head">
                <view>
                  <text class="result-kicker">训练记录已保存</text>
                  <text class="result-title">完成 {{ feedback.completion_pct.toFixed(0) }}%</text>
                </view>
                <text class="result-pill">{{ feedback.kcal.toFixed(0) }} kcal</text>
              </view>
              <view class="result-stats">
                <view class="result-stat"><text class="result-stat-value">{{ feedback.actual_minutes }}</text><text class="result-stat-label">分钟</text></view>
                <view class="result-stat"><text class="result-stat-value">{{ feedback.difficulty }}/5</text><text class="result-stat-label">难度</text></view>
                <view class="result-stat"><text class="result-stat-value">{{ feedback.done_actions }}/{{ feedback.total_actions }}</text><text class="result-stat-label">动作</text></view>
              </view>
            </view>

            <!-- 记忆 -->
            <view v-if="result?.memories?.length" class="result-card">
              <view class="result-head">
                <text class="result-kicker">教练记忆</text>
                <text class="result-link" @click="emit('openMemory')">管理 →</text>
              </view>
              <view v-for="memory in result.memories.slice(0, 8)" :key="memory.id" class="memory-chip-row">
                <text class="memory-chip-label">{{ memory.category_label }}</text>
                <text class="memory-chip-value">{{ memory.value }}</text>
              </view>
            </view>

            <view v-if="result?.progress_insights" class="result-card">
              <text class="result-kicker">训练数据</text>
              <view class="result-stats">
                <view class="result-stat">
                  <text class="result-stat-value">{{ result.progress_insights.weekly.sessions }}</text>
                  <text class="result-stat-label">训练次数</text>
                </view>
                <view class="result-stat">
                  <text class="result-stat-value">{{ result.progress_insights.weekly.avg_completion_pct.toFixed(0) }}%</text>
                  <text class="result-stat-label">平均完成率</text>
                </view>
                <view class="result-stat">
                  <text class="result-stat-value">{{ result.progress_insights.weekly.total_minutes }}</text>
                  <text class="result-stat-label">分钟</text>
                </view>
              </view>
              <view v-for="finding in result.progress_insights.findings.slice(0, 3)" :key="finding.id" class="result-line">
                · {{ finding.title }}：{{ finding.evidence }}
              </view>
            </view>

            <view v-if="result?.changes?.length" class="result-card changes-card">
              <text class="result-kicker">这次改了什么</text>
              <view v-for="(change, index) in result.changes" :key="index" class="result-line">· {{ change }}</view>
            </view>

            <view v-if="result?.warnings?.length" class="workspace-warnings">
              <text v-for="(warning, index) in result.warnings" :key="index">· {{ warning }}</text>
            </view>

                <view v-if="chatActions.length" class="result-actions">
                  <view
                    v-for="action in chatActions"
                    :key="action.type + action.label"
                    class="ws-action-btn"
                    :class="{ primary: ['start_workout', 'open_workout', 'confirm_plan', 'approve_patch'].includes(action.type) }"
                    @click="runAction(action)"
                  >
                    {{ action.label }}
                  </view>
                </view>

                <McdOrderSheet
                  v-if="mcdOrderOpen"
                  inline
                  :visible="true"
                  :suggestions="mcdSuggestions"
                  :instruction="mcdInstruction"
                  :seed-city="result?.mcd_order?.city || ''"
                  :seed-keyword="result?.mcd_order?.store || ''"
                  :seed-foods="result?.mcd_order?.foods || []"
                  @close="mcdOrderOpen = false"
                  @order-created="onMcdOrderCreated"
                  @order-cancelled="onMcdOrderCancelled"
                />
              </view>
              <view v-show="!richCollapsed || hasRichResult" class="chat-rich-bar">
                <text class="chat-rich-bar-label">{{ richCollapsed ? '结果已收起' : '教练结果' }}</text>
                <text class="chat-rich-toggle" @click="richCollapsed = !richCollapsed">
                  {{ richCollapsed ? '展开' : '收起' }}
                </text>
              </view>
            </view>
            </template>

            <view v-if="busy" class="chat-row agent">
              <view class="chat-avatar coach">教</view>
              <view class="chat-bubble agent typing">
                <view class="typing-dots"><text /><text /><text /></view>
                <text class="typing-label">正在输入</text>
              </view>
            </view>

            <view v-if="error" class="chat-system error">{{ error }}</view>


            </view>
          </scroll-view>

          <!-- 输入区固定 -->
          <view class="workspace-composer">
            <view class="composer-input-row">
              <textarea
                v-model="input"
                class="composer-input"
                auto-height
                maxlength="800"
                :placeholder="pageKey === 'workout' ? '器械、动作、时间、身体状态…直接说' : '发消息给教练…'"
                confirm-type="send"
                @confirm="send()"
                @keydown.enter.exact.prevent="send()"
              />
              <view class="composer-send" :class="{ disabled: !input.trim() || busy }" @click="send()">
                {{ busy ? '…' : '发送' }}
              </view>
            </view>
            <view class="composer-foot">
              <text class="composer-hint">Enter 发送 · Shift + Enter 换行</text>
              <text class="composer-hint">同一个 Agent</text>
            </view>
          </view>
        </view>

      </view>
    </view>

    <KnowledgeSheet
      :visible="knowledgeOpen"
      :question="knowledgeQuestion"
      @close="knowledgeOpen = false"
    />
  </view>
</template>


<style>
.workspace-mask {
  background: rgba(43, 26, 18, 0.38);
  bottom: 0;
  left: 0;
  position: fixed;
  right: 0;
  top: 0;
  z-index: 1200;
}

.workspace {
  background: var(--canvas, #fdf2e9);
  border-radius: 24px;
  bottom: 0;
  box-shadow: 0 24px 60px rgba(80, 35, 15, 0.24);
  display: flex;
  flex-direction: column;
  height: 86vh;
  left: 50%;
  max-height: 92vh;
  max-width: 1120px;
  min-height: 620px;
  overflow: hidden;
  position: fixed;
  top: 50%;
  transform: translate(-50%, -50%);
  width: calc(100% - 40px);
  z-index: 1201;
}

.workspace-head {
  align-items: center;
  background: var(--surface, #fff);
  border-bottom: 1px solid var(--line, #f0ded2);
  display: flex;
  justify-content: space-between;
  padding: 14px 20px;
}

.workspace-brand,
.workspace-brand-text,
.workspace-head-right {
  align-items: center;
  display: flex;
}

.workspace-brand {
  gap: 10px;
}

.workspace-avatar {
  align-items: center;
  background: linear-gradient(135deg, #f08a5d, var(--primary, #e2552b));
  border-radius: 14px;
  color: #fff;
  display: flex;
  font-size: 17px;
  font-weight: 800;
  height: 42px;
  justify-content: center;
  width: 42px;
}

.workspace-brand-text {
  align-items: flex-start;
  flex-direction: column;
  gap: 2px;
}

.workspace-title {
  color: var(--ink, #2b1a12);
  font-size: 16px;
  font-weight: 800;
}

.workspace-sub {
  color: var(--muted, #8a7466);
  font-size: 11px;
}

.workspace-head-right {
  align-items: center;
  display: flex;
  gap: 12px;
}

.workspace-online {
  align-items: center;
  background: #edf7ef;
  border-radius: 999px;
  color: #3f7a4f;
  display: flex;
  font-size: 10px;
  font-weight: 700;
  gap: 5px;
  padding: 4px 9px;
}

.workspace-online-dot {
  background: #48a463;
  border-radius: 50%;
  box-shadow: 0 0 0 3px rgba(72, 164, 99, 0.12);
  height: 6px;
  width: 6px;
}

.workspace-close {
  align-items: center;
  border-radius: 50%;
  color: var(--ink-soft, #5a4438);
  cursor: pointer;
  display: flex;
  font-size: 24px;
  height: 32px;
  justify-content: center;
  line-height: 1;
  width: 32px;
}

.workspace-close:hover {
  background: var(--line-soft, #f7ebe2);
}

.conversation-confirm-mask {
  align-items: center;
  background: rgba(43, 26, 18, 0.42);
  border-radius: inherit;
  bottom: 0;
  display: flex;
  justify-content: center;
  left: 0;
  position: absolute;
  right: 0;
  top: 0;
  z-index: 1500;
}

.conversation-confirm-card {
  background: var(--surface, #fff);
  border-radius: 16px;
  box-shadow: 0 20px 50px rgba(60, 30, 15, 0.24);
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: 320px;
  padding: 18px;
  width: calc(100% - 48px);
}

.conversation-confirm-title {
  color: var(--ink, #2b1a12);
  font-size: 15px;
  font-weight: 800;
}

.conversation-confirm-copy {
  color: var(--muted, #8a7466);
  font-size: 11px;
  line-height: 1.55;
}

.conversation-confirm-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  margin-top: 6px;
}

.workspace-body {
  display: grid;
  flex: 1;
  grid-template-columns: 200px minmax(0, 1fr);
  min-height: 0;
}

.workspace-left {
  background: rgba(255, 255, 255, 0.7);
  border-right: 1px solid var(--line, #f0ded2);
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
  padding: 14px 10px;
}

.conversation-new {
  align-items: center;
  background: var(--surface, #fff);
  border: 1px solid var(--line, #f0ded2);
  border-radius: 11px;
  color: var(--ink, #2b1a12);
  cursor: pointer;
  display: flex;
  flex: 0 0 auto;
  font-size: 13px;
  font-weight: 700;
  gap: 7px;
  padding: 10px 12px;
}

.conversation-new:hover {
  border-color: var(--primary-soft, #fceae1);
  background: #fffaf6;
}

.conversation-new-plus {
  color: var(--primary, #e2552b);
  font-size: 16px;
  line-height: 1;
}

.conversation-head {
  color: var(--muted, #8a7466);
  flex: 0 0 auto;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.08em;
  padding: 4px 4px 0;
}

.conversation-list {
  flex: 1 1 0;
  height: 0;
  min-height: 0;
}

.conversation-item {
  border-radius: 10px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin-bottom: 3px;
  padding: 9px 9px;
}

.conversation-item:hover {
  background: rgba(252, 234, 225, 0.55);
}

.conversation-item.active {
  background: var(--primary-soft, #fceae1);
}

.conversation-item-head {
  align-items: baseline;
  display: flex;
  gap: 6px;
}

.conversation-title {
  color: var(--ink, #2b1a12);
  flex: 1;
  font-size: 12px;
  font-weight: 700;
  line-height: 1.4;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conversation-time {
  color: #b09a8b;
  flex: 0 0 auto;
  font-size: 9px;
}

.conversation-delete {
  color: #b09a8b;
  flex: 0 0 auto;
  font-size: 14px;
  line-height: 1;
  opacity: 0.55;
  padding: 0 2px;
}

.conversation-item:hover .conversation-delete {
  color: var(--primary-deep, #c2410c);
  opacity: 1;
}

.conversation-preview {
  color: var(--muted, #8a7466);
  display: block;
  font-size: 10px;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conversation-empty {
  color: var(--muted, #8a7466);
  font-size: 10px;
  line-height: 1.5;
  padding: 10px 6px;
}

.workspace-chat {
  -webkit-user-select: text;
  user-select: text;
  background-color: #fbf7f2;
  background-image: radial-gradient(circle at 1px 1px, rgba(180, 130, 100, 0.13) 1px, transparent 0);
  background-size: 18px 18px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  position: relative;
}

.workspace-scroll {
  box-sizing: border-box;
  flex: 1 1 0;
  height: 0;
  min-height: 0;
  padding: 20px 22px 8px;
  width: 100%;
}

.chat-stream {
  margin: 0 auto;
  max-width: 1120px;
}

.chat-date {
  display: flex;
  justify-content: center;
  margin-bottom: 16px;
}

.chat-date text {
  background: rgba(255, 255, 255, 0.72);
  border-radius: 999px;
  color: var(--muted, #8a7466);
  font-size: 10px;
  padding: 4px 10px;
}

.chat-time {
  color: #ad998b;
  font-size: 10px;
  margin: 16px 0 8px;
  text-align: center;
}

.chat-row {
  align-items: flex-start;
  display: flex;
  gap: 8px;
  margin-bottom: 14px;
  max-width: 100%;
}

.chat-row.user {
  justify-content: flex-end;
}

.chat-avatar {
  align-items: center;
  border-radius: 10px;
  display: flex;
  flex: 0 0 auto;
  font-size: 13px;
  font-weight: 800;
  height: 32px;
  justify-content: center;
  width: 32px;
}

.chat-avatar.coach {
  background: linear-gradient(135deg, #f08a5d, var(--primary, #e2552b));
  box-shadow: 0 3px 8px rgba(226, 85, 43, 0.2);
  color: #fff;
}

.chat-avatar.user {
  background: #ead9cc;
  color: #6b4a39;
}

.chat-bubble {
  -webkit-user-select: text;
  user-select: text;
  border-radius: 4px 16px 16px 16px;
  max-width: min(72%, 640px);
  box-shadow: 0 1px 2px rgba(90, 55, 35, 0.06);
  font-size: 14px;
  line-height: 1.75;
  padding: 10px 14px;
  position: relative;
  word-break: break-word;
}

.chat-bubble.agent {
  background: #fff;
  border: 1px solid #f1e2d6;
  color: var(--ink, #2b1a12);
}

.chat-bubble.user {
  background: linear-gradient(135deg, #ef6a3f, var(--primary, #e2552b));
  border-radius: 16px 4px 16px 16px;
  box-shadow: 0 3px 10px rgba(226, 85, 43, 0.18);
  color: #fff;
}

.chat-text {
  -webkit-user-select: text;
  display: block;
  font-size: inherit;
  line-height: inherit;
  user-select: text;
  white-space: pre-wrap;
}

.chat-bubble.typing {
  align-items: center;
  display: flex;
  gap: 9px;
  padding: 12px 14px;
}

.typing-dots {
  display: flex;
  gap: 3px;
}

.typing-dots text {
  animation: typing-bounce 1.1s infinite ease-in-out;
  background: #b99e8c;
  border-radius: 50%;
  height: 5px;
  width: 5px;
}

.typing-dots text:nth-child(2) {
  animation-delay: 0.15s;
}

.typing-dots text:nth-child(3) {
  animation-delay: 0.3s;
}

.typing-label {
  color: var(--muted, #8a7466);
  font-size: 11px;
}

@keyframes typing-bounce {
  0%,
  60%,
  100% {
    opacity: 0.35;
    transform: translateY(0);
  }
  30% {
    opacity: 1;
    transform: translateY(-3px);
  }
}

.chat-attachment {
  -webkit-user-select: none;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid var(--line, #f0ded2);
  border-radius: 12px;
  cursor: pointer;
  display: flex;
  gap: 10px;
  justify-content: space-between;
  margin: -4px 0 14px 40px;
  max-width: 72%;
  padding: 9px 11px;
  user-select: none;
}

.chat-attachment:hover {
  background: #fffdfb;
}

.order-attachment {
  background: #fffaf6;
  border-color: rgba(226, 85, 43, 0.22);
}

.plan-order-note {
  background: #fffaf6;
  border-color: rgba(226, 85, 43, 0.22);
}

.chat-attachment-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.chat-attachment-kind {
  color: var(--primary-deep, #c2410c);
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.04em;
}

.chat-attachment-title {
  color: var(--ink, #2b1a12);
  font-size: 12px;
  font-weight: 700;
  line-height: 1.45;
}

.chat-attachment-detail {
  color: var(--muted, #8a7466);
  font-size: 10px;
  line-height: 1.45;
}

.chat-attachment-open {
  color: var(--primary, #e2552b);
  flex: 0 0 auto;
  font-size: 11px;
  font-weight: 700;
}

.chat-system {
  color: var(--muted, #8a7466);
  font-size: 11px;
  line-height: 1.6;
  margin: 8px auto 12px;
  max-width: 80%;
  text-align: center;
}

.chat-system.error {
  background: #fff4ee;
  border-radius: 10px;
  color: #a34b1f;
  padding: 8px 12px;
}

.chat-rich-row {
  margin-bottom: 4px;
  padding-left: 40px;
}

.chat-rich-bar {
  align-items: center;
  backdrop-filter: blur(6px);
  background: rgba(251, 247, 242, 0.96);
  border-top: 1px solid var(--line-soft, #f7ebe2);
  bottom: 0;
  display: flex;
  justify-content: space-between;
  margin-top: 6px;
  padding: 6px 4px 6px 12px;
  position: sticky;
  z-index: 3;
}

.chat-rich-bar-label {
  color: #ad998b;
  font-size: 10px;
  letter-spacing: 0.04em;
}

.chat-rich-toggle {
  color: var(--primary-deep, #c2410c);
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
}

.chat-rich {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: min(86%, 760px);
  min-width: 0;
}

.chat-rich .result-card {
  margin: 0;
}

.result-card {
  -webkit-user-select: text;
  user-select: text;
  background: #fffaf6;
  border: 1px solid var(--line, #f0ded2);
  border-radius: 14px;
  margin: 12px 0;
  padding: 14px;
}

.result-head {
  align-items: flex-start;
  display: flex;
  gap: 10px;
  justify-content: space-between;
}

.result-kicker {
  color: var(--muted, #8a7466);
  display: block;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.result-title {
  color: var(--ink, #2b1a12);
  display: block;
  font-size: 16px;
  font-weight: 700;
  line-height: 1.4;
  margin-top: 3px;
}

.result-pill {
  background: var(--primary-soft, #fceae1);
  border-radius: 999px;
  color: var(--primary-deep, #c2410c);
  flex: 0 0 auto;
  font-size: 10px;
  font-weight: 700;
  padding: 4px 9px;
}

.today-brief-card {
  background: #fffdfb;
}

.brief-next-training {
  background: #fff5f1;
  border-radius: 10px;
  margin-top: 10px;
  padding: 9px 11px;
}

.brief-next-title {
  color: var(--ink, #2b1a12);
  display: block;
  font-size: 12px;
  font-weight: 700;
  margin-top: 2px;
}

.brief-next-actions {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 4px;
}

.brief-next-action {
  color: var(--ink-soft, #5a4438);
  font-size: 11px;
}

.brief-schedule {
  background: #fff7ef;
  border-radius: 10px;
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin-top: 10px;
  padding: 9px 11px;
}

.brief-schedule-row {
  align-items: flex-start;
  display: flex;
  gap: 10px;
}

.brief-time {
  color: var(--primary-deep, #c2410c);
  flex: 0 0 42px;
  font-size: 10px;
  font-weight: 700;
}

.brief-schedule-copy {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.brief-activity {
  color: var(--ink, #2b1a12);
  font-size: 12px;
  font-weight: 600;
}

.brief-why {
  color: var(--muted, #8a7466);
  font-size: 10px;
  line-height: 1.45;
}

.brief-meal {
  border-top: 1px dashed var(--line, #f0ded2);
  margin-top: 10px;
  padding-top: 9px;
}

.brief-meal-head,
.brief-option-head {
  align-items: baseline;
  display: flex;
  gap: 8px;
  justify-content: space-between;
}

.brief-meal-title {
  color: var(--ink, #2b1a12);
  font-size: 13px;
  font-weight: 800;
}

.brief-meal-target,
.brief-option-kcal {
  color: var(--muted, #8a7466);
  flex: 0 0 auto;
  font-size: 10px;
}

.brief-option {
  border: 1px solid transparent;
  border-radius: 10px;
  cursor: pointer;
  margin-top: 8px;
  padding: 7px 8px;
}

.brief-option:hover {
  background: #fff8f2;
}

.brief-option.picked {
  background: #fff4ec;
  border-color: var(--primary-soft, #fceae1);
}

.brief-picked {
  background: var(--primary, #e2552b);
  border-radius: 999px;
  color: #fff;
  font-size: 9px;
  font-weight: 700;
  margin-left: 5px;
  padding: 1px 6px;
}

.brief-tips {
  border-top: 1px dashed var(--line, #f0ded2);
  margin-top: 10px;
  padding-top: 9px;
}

.brief-tip {
  align-items: flex-start;
  border-top: 1px solid var(--line-soft, #f7ebe2);
  display: flex;
  gap: 12px;
  justify-content: space-between;
  padding: 8px 0;
}

.brief-tip:first-of-type {
  border-top: none;
}

.brief-tip-text {
  color: var(--ink-soft, #5a4438);
  display: block;
  flex: 1 1 auto;
  font-size: 11px;
  line-height: 1.6;
  min-width: 0;
}

.brief-tip-links {
  align-items: flex-end;
  display: flex;
  flex: 0 0 auto;
  flex-direction: column;
  gap: 5px;
  justify-content: flex-end;
  margin-top: 0;
}

.brief-source-static {
  color: #ad998b;
  font-size: 10px;
  white-space: nowrap;
}

.brief-apply-row {
  align-items: center;
  border-top: 1px dashed var(--line, #f0ded2);
  display: flex;
  gap: 12px;
  justify-content: space-between;
  margin-top: 12px;
  padding-top: 10px;
}

.mcd-inline-card,
.saved-mcd-order {
  background: #fffdfb;
}

.saved-mcd-order {
  border-color: rgba(214, 35, 0, 0.18);
}

.mcd-inline-head {
  align-items: center;
  display: flex;
  gap: 10px;
}

.mcd-inline-card .mcd-mark {
  align-items: center;
  background: #d62300;
  border-radius: 9px;
  color: #fff;
  display: flex;
  flex: 0 0 auto;
  font-size: 16px;
  font-weight: 800;
  height: 32px;
  justify-content: center;
  width: 32px;
}

.mcd-combo-line {
  color: var(--ink-soft, #5a4438);
  display: block;
  font-size: 11px;
  line-height: 1.55;
  margin-top: 3px;
}

.mcd-inline-titles {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.mcd-inline-budget {
  align-items: baseline;
  background: #fff5f1;
  border-radius: 10px;
  display: flex;
  gap: 24px;
  margin-top: 10px;
  padding: 9px 11px;
}

.mcd-inline-combo {
  background: #fff7ef;
  border-radius: 10px;
  margin-top: 9px;
  padding: 9px 11px;
}

.mcd-inline-option {
  align-items: flex-start;
  border-top: 1px solid var(--line-soft, #f7ebe2);
  cursor: pointer;
  display: flex;
  gap: 12px;
  justify-content: space-between;
  padding: 9px 0;
}

.mcd-inline-option-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.mcd-inline-option-name {
  color: var(--ink, #2b1a12);
  font-size: 12px;
  font-weight: 700;
}

.mcd-inline-option-right {
  align-items: flex-end;
  display: flex;
  flex: 0 0 auto;
  flex-direction: column;
  gap: 2px;
}

.brief-option-title {
  color: var(--ink-soft, #5a4438);
  font-size: 12px;
  font-weight: 700;
}

.brief-option-meta,
.brief-option-items {
  color: var(--muted, #8a7466);
  display: block;
  font-size: 10px;
  line-height: 1.5;
  margin-top: 2px;
}

.brief-links {
  display: flex;
  gap: 6px;
  margin-top: 5px;
}

.brief-link {
  background: var(--line-soft, #f7ebe2);
  border-radius: 999px;
  color: var(--primary-deep, #c2410c);
  font-size: 10px;
  padding: 3px 8px;
}

.result-stats {
  display: flex;
  gap: 22px;
  margin-top: 12px;
}

.result-stat {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.result-stat-value {
  color: var(--ink, #2b1a12);
  font-size: 22px;
  font-weight: 800;
}

.result-stat-label {
  color: var(--muted, #8a7466);
  font-size: 10px;
}

.result-warn {
  background: #fff4e8;
  border-radius: 9px;
  color: #8a4b20;
  font-size: 12px;
  line-height: 1.5;
  margin-top: 10px;
  padding: 8px 10px;
}

.result-line {
  color: var(--ink-soft, #5a4438);
  font-size: 12px;
  line-height: 1.6;
  margin-top: 4px;
}

.result-line.warn {
  color: #a34b1f;
}

.result-sub {
  color: var(--muted, #8a7466);
  display: block;
  font-size: 11px;
  line-height: 1.55;
  margin-top: 5px;
}

.result-link {
  color: var(--primary, #e2552b);
  font-size: 12px;
  font-weight: 600;
}

.result-action-row {
  align-items: center;
  border-top: 1px solid var(--line-soft, #f7ebe2);
  display: flex;
  justify-content: space-between;
  padding: 7px 0;
}

.result-action-row:first-of-type {
  border-top: none;
}

.result-action-name {
  color: var(--ink-soft, #5a4438);
  font-size: 13px;
}

.result-action-meta {
  color: var(--muted, #8a7466);
  font-size: 11px;
}

.result-progress {
  background: var(--line-soft, #f7ebe2);
  border-radius: 999px;
  height: 8px;
  margin-top: 10px;
  overflow: hidden;
}

.result-progress-fill {
  background: var(--primary, #e2552b);
  border-radius: 999px;
  height: 100%;
}

.guidance-item {
  margin-top: 10px;
}

.guidance-sources {
  align-items: center;
  display: flex;
  gap: 10px;
  justify-content: flex-end;
  margin-top: 6px;
}

.guidance-hint {
  color: #ad998b;
  flex: 1 1 auto;
  font-size: 10px;
  line-height: 1.4;
  text-align: right;
}

.memory-chip-row {
  align-items: baseline;
  border-top: 1px solid var(--line-soft, #f7ebe2);
  display: flex;
  gap: 8px;
  padding: 7px 0;
}

.memory-chip-row:first-of-type {
  border-top: none;
}

.memory-chip-label {
  background: var(--primary-soft, #fceae1);
  border-radius: 999px;
  color: var(--primary-deep, #c2410c);
  flex: 0 0 auto;
  font-size: 9px;
  padding: 2px 7px;
}

.memory-chip-value {
  color: var(--ink-soft, #5a4438);
  font-size: 12px;
  line-height: 1.5;
}

.workspace-warnings {
  background: #fff8ee;
  border: 1px solid var(--line, #f0ded2);
  border-radius: 10px;
  color: #7a5a20;
  display: flex;
  flex-direction: column;
  font-size: 11px;
  gap: 2px;
  line-height: 1.55;
  margin-top: 10px;
  padding: 8px 10px;
}

.result-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 12px 0;
}

.ws-action-btn {
  background: var(--surface, #fff);
  border: 1px solid var(--line, #f0ded2);
  border-radius: 999px;
  color: var(--ink, #2b1a12);
  font-size: 12px;
  font-weight: 600;
  padding: 9px 14px;
}

.ws-action-btn.primary {
  background: var(--primary, #e2552b);
  border-color: var(--primary, #e2552b);
  color: #fff;
}

.draft-week {
  border-top: 1px dashed var(--line, #f0ded2);
  margin-top: 10px;
  padding-top: 8px;
}

.draft-day {
  border-top: 1px solid var(--line-soft, #f7ebe2);
  padding: 7px 0;
}

.draft-day:first-of-type {
  border-top: none;
}

.draft-day-head {
  align-items: baseline;
  display: flex;
  justify-content: space-between;
}

.draft-day-title {
  font-size: 12px;
  font-weight: 700;
}

.draft-day-count,
.draft-day-actions {
  color: var(--muted, #8a7466);
  font-size: 10px;
}

.composer-input-row {
  align-items: flex-end;
  display: flex;
  gap: 8px;
  margin: 0 auto;
  max-width: 1100px;
}

.composer-input {
  background: #f7f3ee;
  border: 1px solid #efe3d9;
  border-radius: 20px;
  box-sizing: border-box;
  color: var(--ink, #2b1a12);
  flex: 1;
  font-size: 14px;
  line-height: 1.5;
  max-height: 120px;
  min-height: 44px;
  padding: 11px 15px;
  width: auto;
}

.composer-input:focus {
  background: #fffdfb;
  border-color: rgba(226, 85, 43, 0.45);
}

.composer-send {
  align-items: center;
  background: var(--primary, #e2552b);
  border-radius: 50%;
  box-shadow: 0 4px 12px rgba(226, 85, 43, 0.22);
  color: #fff;
  display: flex;
  flex: 0 0 auto;
  font-size: 12px;
  font-weight: 700;
  height: 44px;
  justify-content: center;
  width: 44px;
}

.composer-send.disabled {
  box-shadow: none;
  opacity: 0.45;
}

.composer-foot {
  display: flex;
  gap: 10px;
  justify-content: space-between;
  margin: 7px auto 0;
  max-width: 1100px;
}

.composer-hint {
  color: #ad998b;
  font-size: 10px;
  line-height: 1.5;
}

.workspace-composer {
  background: #fff;
  border-top: 1px solid var(--line, #f0ded2);
  padding: 12px 20px 12px;
}

.workspace-error {
  margin-top: 10px;
}

.workspace-inline {
  border: 1px solid var(--line, #f0ded2);
  box-shadow: var(--shadow-card, 0 6px 18px rgba(180, 120, 90, 0.08));
  height: 600px;
  left: auto;
  margin: 0;
  max-height: none;
  max-width: none;
  min-height: 0;
  position: relative;
  top: auto;
  transform: none;
  width: 100%;
}

@media (max-width: 980px) {
  .workspace {
    border-radius: 0;
    height: 100vh;
    max-height: none;
    max-width: none;
    width: 100%;
  }

  .workspace-body {
    grid-template-columns: 1fr;
  }

  .workspace-left {
    display: none;
  }

  .workspace-inline {
    height: 600px;
  }

  .workspace-head {
    padding: 12px 14px;
  }

  .chat-bubble {
    max-width: 78%;
  }

  .chat-rich {
    max-width: 90%;
  }

  .workspace-scroll {
    padding: 14px 14px 6px;
  }

  .workspace-composer {
    padding: 10px 14px;
  }

  .composer-hint:first-child {
    display: none;
  }

  .brief-tip {
    flex-direction: column;
    gap: 4px;
  }

  .brief-tip-links {
    align-items: flex-start;
  }

  .guidance-sources {
    align-items: flex-start;
    justify-content: flex-start;
  }

  .guidance-hint {
    text-align: left;
  }
}
</style>
