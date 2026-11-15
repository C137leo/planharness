import { resetCoachWorkspace } from '../composables/coachWorkspace'
import { lastSavedChatRequest, rememberChatRequest, forgetChatRequest } from './chatRequests'
import type {
  MealLogItem,
  AgentChatResult,
  AgentConversation,
  AgentAdjustment,
  AgentDay,
  AgentMemoryList,
  ProgressInsights,
  AgentMessageItem,
  ApiEnvelope,
  CheckinPayload,
  CurrentPlan,
  AuditEvent,
  CompareResult,
  AuditVerify,
  AuthChallenge,
  CalorieStats,
  FoodOptionPayload,
  TodayAgentResult,
  SystemStatus,
  LlmSwitchState,
  KnowledgeAnswer,
  LoginPayload,
  Me,
  RegisterPayload,
  TokenPayload,
  CheckinRecord,
  PatchPreset,
  PlanDraft,
  Profile,
  ProfileUpdate,
  EvalScenario,
  EvalScenarioResult,
  PatchRequest,
  WorkoutLogItem,
  WorkoutLogPayload,
  PlanPatch,
  PlanPatchSummary,
  RunDetail,
  RunSummary,
  PlanVersionHistory,
  TodayPlanSnapshot,
  McdStore,
  McdMenuPayload,
  McdQuote,
  McdOrder,
  MemoryItem,
  WorkoutSession,
} from './types'

const API_BASE = (import.meta.env.VITE_API_BASE || '/api/v1').replace(/\/$/, '')
const TOKEN_KEY = 'planharness_token'
export const MCD_TOKEN_KEY = 'planharness_mcd_token'

export class ApiError extends Error {
  constructor(
    message: string,
    public statusCode = 0,
    public detail: unknown = null,
    public traceId = '',
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export function getStoredToken(): string {
  return (uni.getStorageSync(TOKEN_KEY) as string) || ''
}

export function setStoredToken(token: string): void {
  uni.setStorageSync(TOKEN_KEY, token)
}

/** 麦当劳 MCP token 按登录用户分别存在本机；是否映射服务端 token 由后端配置决定。 */
function accountStorageId(auth = getStoredToken()): string {
  if (!auth) return ''
  try {
    const raw = auth.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')
    const padded = raw.padEnd(Math.ceil(raw.length / 4) * 4, '=')
    const payload = JSON.parse(globalThis.atob(padded)) as { sub?: string }
    return typeof payload.sub === 'string' ? payload.sub : ''
  } catch {
    return ''
  }
}

function mcdStorageKey(): string {
  return `${MCD_TOKEN_KEY}:${accountStorageId() || 'anon'}`
}

export function getStoredMcdToken(): string {
  const key = mcdStorageKey()
  const current = (uni.getStorageSync(key) as string) || ''
  if (current) return current
  // 兼容旧版本：以前存在全局 key 上，第一次读取时迁移到当前用户名下。
  const legacy = (uni.getStorageSync(MCD_TOKEN_KEY) as string) || ''
  if (legacy) {
    uni.setStorageSync(key, legacy)
    uni.removeStorageSync(MCD_TOKEN_KEY)
    return legacy
  }
  return ''
}

export function setStoredMcdToken(token: string): void {
  const key = mcdStorageKey()
  if (token) uni.setStorageSync(key, token)
  else uni.removeStorageSync(key)
}

function mcdHeaders(token = ''): Record<string, string> {
  return token ? { 'X-MCD-Token': token } : {}
}

function parseError(data: unknown, statusCode: number): ApiError {
  const body = data as { detail?: unknown; message?: string; trace_id?: string } | null
  const detail = body?.detail
  const traceId = body?.trace_id || ''
  let message = body?.message || `请求失败（${statusCode}）`
  if (typeof detail === 'string') message = detail
  if (Array.isArray(detail)) message = detail.join('；')
  // 5xx 把 trace_id 带到界面上，用户报错时能直接在 journal 里定位这一次请求
  if (statusCode >= 500 && traceId) message = `${message}（trace: ${traceId}）`
  return new ApiError(message, statusCode, detail, traceId)
}

async function rawRequest<T>(
  method: 'GET' | 'POST' | 'PUT' | 'DELETE',
  path: string,
  data?: unknown,
  token = '',
  extraHeaders: Record<string, string> = {},
): Promise<T> {
  return new Promise((resolve, reject) => {
    uni.request({
      url: `${API_BASE}${path}`,
      method,
      timeout: path === '/agent/chat' ? 15000 : 60000,
      data: data as object,
      header: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...extraHeaders,
      },
      success: (response) => {
        if (response.statusCode >= 200 && response.statusCode < 300) {
          const body = response.data as ApiEnvelope<T>
          if (body && body.code === 0) {
            resolve(body.data)
            return
          }
          reject(parseError(body, response.statusCode))
          return
        }
        reject(parseError(response.data, response.statusCode))
      },
      fail: (error) => {
        reject(new ApiError(error.errMsg || '网络请求失败'))
      },
    })
  })
}

/** 只读已保存的 token；没有就抛 401，由页面引导去登录页。
    不再自动用 demo 账号登录——那会让"注册/登录"形同虚设。 */
export async function ensureToken(): Promise<string> {
  const existing = getStoredToken()
  if (existing) return existing
  throw new ApiError('请先登录', 401)
}

/** 清掉本地登录态（退出登录 / token 失效时调用）。 */
export function clearToken(): void {
  uni.removeStorageSync(TOKEN_KEY)
  resetCoachWorkspace()
}

/** 登录态失效时的统一处理：清 token → 回登录页（已经在登录页就不跳，避免循环）。 */
export function handleUnauthorized(): void {
  clearToken()
  const pages = typeof getCurrentPages === 'function' ? getCurrentPages() : []
  const current = pages.length ? pages[pages.length - 1].route || '' : ''
  if (current.includes('pages/login')) return
  uni.reLaunch({ url: '/pages/login/index' })
}

/** 挑战接口不需要登录态，所以直接走 rawRequest（不带 Authorization）。 */
async function rawGetChallenge(purpose: 'register' | 'login') {
  return rawRequest<AuthChallenge>('GET', `/auth/challenge?purpose=${purpose}`)
}

async function request<T>(
  method: 'GET' | 'POST' | 'PUT' | 'DELETE',
  path: string,
  data?: unknown,
  extraHeaders: Record<string, string> = {},
): Promise<T> {
  let token: string
  try {
    token = await ensureToken()
  } catch (err) {
    if (err instanceof ApiError && err.statusCode === 401 && !getStoredToken()) handleUnauthorized()
    throw err
  }
  try {
    const result = await rawRequest<T>(method, path, data, token, extraHeaders)
    if (getStoredToken() !== token) throw new ApiError('登录账户已变化，已丢弃旧请求结果。', 409)
    return result
  } catch (error) {
    if (error instanceof ApiError && error.statusCode === 401 && getStoredToken() === token) {
      // 旧账号的延迟响应不能清除用户刚切换的新登录态。
      handleUnauthorized()
    }
    throw error
  }
}

async function chatWithRecovery(payload: Record<string, unknown>, headers: Record<string, string>): Promise<AgentChatResult> {
  const auth = getStoredToken()
  const owner = accountStorageId(auth)
  const requestId = String(payload.request_id)
  rememberChatRequest(owner, requestId, String(payload.thread || 'coach'))
  const sameAccount = () => {
    if (getStoredToken() !== auth) throw new ApiError('登录账户已变化，已停止读取旧请求。', 409)
  }
  try {
    const result = await request<AgentChatResult>('POST', '/agent/chat', payload, headers)
    sameAccount()
    forgetChatRequest(owner, requestId)
    return result
  } catch (err) {
    sameAccount()
    if (!(err instanceof ApiError) || (err.statusCode !== 0 && err.statusCode < 500)) {
      forgetChatRequest(owner, requestId)
      throw err
    }
    const deadline = Date.now() + 180000
    while (Date.now() < deadline) {
      sameAccount()
      await new Promise((resolve) => setTimeout(resolve, 1500))
      let saved: { status: string; result: AgentChatResult | null }
      try {
        saved = await request('GET', `/agent/requests/${encodeURIComponent(String(payload.request_id))}`)
      } catch (pollError) {
        if (pollError instanceof ApiError && pollError.statusCode === 401) throw pollError
        continue
      }
      sameAccount()
      if (saved.status === 'succeeded' && saved.result) {
        forgetChatRequest(owner, requestId)
        return saved.result
      }
      if (saved.status !== 'running') throw new ApiError('该请求需要核对执行记录，未重复提交。请查看聊天历史后继续。', 0, { request_pending: true, request_id: payload.request_id })
    }
    throw new ApiError('请求结果仍待核实，未重复提交。请稍后重新打开聊天查看最终结果。', 0, { request_pending: true, request_id: payload.request_id })
  }
}

async function recoverSavedChat(thread: string): Promise<{ request_id: string; status: string; result: AgentChatResult | null } | null> {
  const auth = getStoredToken(), owner = accountStorageId(auth)
  const saved = lastSavedChatRequest(owner, thread)
  if (!saved) return null
  const result = await request<{request_id: string; status: string; thread: string; result: AgentChatResult | null}>(
    'GET', `/agent/requests/${encodeURIComponent(saved.request_id)}`)
  if (auth !== getStoredToken()) throw new ApiError('登录账户已变化，已停止读取旧请求。', 409)
  if (result.thread !== thread) throw new ApiError('上次请求所属会话不一致，请核对聊天历史。', 409)
  if (result.status === 'succeeded' && result.result) forgetChatRequest(owner, saved.request_id)
  return result
}

/** 后端版本可能比前端旧：把可能缺失的数组字段补成空数组。
    这一步是白屏的最后一道防线——组件里任何一个 `.length` 都可能直接炸掉整页。 */
function normalizeDraft(draft: PlanDraft): PlanDraft {
  return {
    ...draft,
    days: draft.days ?? [],
    meals: draft.meals ?? [],
    revisions: draft.revisions ?? [],
    cautions: draft.cautions ?? [],
    highlights: draft.highlights ?? [],
    warnings: draft.warnings ?? [],
    feedback: draft.feedback ?? [],
    trace: draft.trace ?? [],
  }
}

function normalizePlan(plan: CurrentPlan): CurrentPlan {
  return {
    ...plan,
    actions: plan.actions ?? [],
    meals: plan.meals ?? [],
    nutrition: plan.nutrition ?? {},
    program: plan.program ?? {},
  }
}

export const api = {
  // ---- 账号 ----
  authChallenge: (purpose: 'register' | 'login' = 'register') =>
    rawGetChallenge(purpose),
  register: (payload: RegisterPayload) =>
    rawRequest<TokenPayload>('POST', '/auth/register', payload),
  login: (payload: LoginPayload) =>
    rawRequest<TokenPayload>('POST', '/auth/login', payload),
  demoLogin: () => rawRequest<TokenPayload>('POST', '/auth/demo', {}),
  me: () => request<Me>('GET', '/auth/me'),
  // 公开状态：登录页也能读，用来显示维护提示
  getSystemStatus: () => rawRequest<SystemStatus>('GET', '/system/status'),
  getLlmSwitch: () => request<LlmSwitchState>('GET', '/admin/llm-switch'),
  setLlmSwitch: (paused: boolean) =>
    request<LlmSwitchState>('PUT', '/admin/llm-switch', { paused }),
  logout: () => {
    clearToken()
  },

  getCurrentPlan: async () =>
    normalizePlan(await request<CurrentPlan>('GET', '/plans/current')),
  generatePlan: () => request<CurrentPlan>('POST', '/plans/generate'),
  createKneePatch: (planId: string, availableDays = 2) =>
    request<PlanPatch>('POST', `/plans/${planId}/patches`, {
      reason_code: 'injury',
      available_days: availableDays,
    }),
  getPatch: (patchId: string) => request<PlanPatch>('GET', `/patches/${patchId}`),
  listPatches: (limit = 20, status = '') =>
    request<PlanPatchSummary[]>(
      'GET',
      `/patches?limit=${limit}${status ? `&status=${status}` : ''}`,
    ),
  approvePatch: (patchId: string, comment = '') =>
    request<{ patch_id: string; committed_version: number; plan: CurrentPlan }>(
      'POST',
      `/patches/${patchId}/approve`,
      { comment },
    ),
  rejectPatch: (patchId: string, comment = '') =>
    request<PlanPatch>('POST', `/patches/${patchId}/reject`, { comment }),
  saveCheckin: (payload: CheckinPayload) =>
    request<{ id: string; date: string }>('POST', '/checkins', payload),
  listMealLogs: (day = '') =>
    request<MealLogItem[]>('GET', `/meal-logs${day ? `?day=${day}` : ''}`),
  createMealLog: (payload: {
    date?: string
    slot: 'breakfast' | 'lunch' | 'dinner' | 'snack'
    title: string
    kcal: number
    protein_g?: number
    note?: string
  }) => request<MealLogItem>('POST', '/meal-logs', payload),
  deleteMealLog: (id: string) =>
    request<{ deleted: string }>('DELETE', `/meal-logs/${id}`),
  getAudit: (streamId: string) => request<AuditEvent[]>('GET', `/audit/${streamId}`),
  verifyAudit: (streamId: string) => request<AuditVerify>('GET', `/audit/${streamId}/verify`),
  compareKneeModes: () => request<CompareResult>('POST', '/compare/knee-patch'),
  listEvalScenarios: () => request<EvalScenario[]>('GET', '/evals/scenarios'),
  runEvalScenario: (id: string) =>
    request<EvalScenarioResult>('POST', `/evals/scenarios/${id}/run`),
  createPatchRequest: (planId: string, text: string, quickAction = '', baseVersion?: number) =>
    request<PatchRequest>('POST', `/plans/${planId}/patch-requests`, {
      text,
      quick_action: quickAction,
      base_version: baseVersion ?? null,
    }),
  getPatchRequest: (requestId: string) => request<PatchRequest>('GET', `/patch-requests/${requestId}`),
  feedbackPatchRequest: (requestId: string, text: string) =>
    request<PatchRequest>('POST', `/patch-requests/${requestId}/feedback`, { text }),
  approvePatchRequest: (requestId: string, comment = '') =>
    request<{ request_id: string; committed_version: number; plan: CurrentPlan }>(
      'POST',
      `/patch-requests/${requestId}/approve`,
      { comment },
    ),
  rejectPatchRequest: (requestId: string, comment = '') =>
    request<PatchRequest>('POST', `/patch-requests/${requestId}/reject`, { comment }),
  planVersions: (planId: string, limit = 20) =>
    request<PlanVersionHistory>('GET', `/plans/${planId}/versions?limit=${limit}`),
  rollbackPlan: (planId: string, targetVersion: number) =>
    request<{ new_version: number }>('POST', `/plans/${planId}/rollback`, {
      target_version: targetVersion,
    }),
  retargetPlan: (planId: string) =>
    request<{ new_version: number }>('POST', `/plans/${planId}/retarget`, {}),
  undoPlan: (planId: string) =>
    request<{ new_version: number; plan: CurrentPlan }>('POST', `/plans/${planId}/undo`),
  logWorkout: (payload: WorkoutLogPayload) =>
    request<WorkoutLogItem>('POST', '/workout-logs', payload),
  listWorkoutLogs: (days = 14) =>
    request<{ items: WorkoutLogItem[]; days: number; since: string }>(
      'GET',
      `/workout-logs?days=${days}`,
    ),
  deleteWorkoutLog: (logId: string) =>
    request<{ deleted: string }>('DELETE', `/workout-logs/${logId}`),
  getCalorieStats: (days = 7) => request<CalorieStats>('GET', `/stats/calories?days=${days}`),
  listCheckins: () => request<CheckinRecord[]>('GET', '/checkins'),
  getProfile: () => request<Profile>('GET', '/me/profile'),
  updateProfile: (payload: ProfileUpdate) => request<Profile>('PUT', '/me/profile', payload),
  getTodayPlan: (date = '') =>
    request<TodayPlanSnapshot | null>(
      'GET',
      `/agent/today-plan${date ? `?date=${date}` : ''}`,
    ),

  // ---- 麦当劳点餐：找门店 → 菜单 → 算价 → 建单（付款留给用户）----
  mcdStatus: (mcdToken = '') =>
    request<{ available: boolean; allow_order: boolean }>(
      'GET', '/mcd/status', undefined, mcdHeaders(mcdToken),
    ),
  mcdStores: (city: string, keyword: string, mcdToken = '') =>
    request<{ stores: McdStore[] }>(
      'GET',
      `/mcd/stores?city=${encodeURIComponent(city)}&keyword=${encodeURIComponent(keyword)}`,
      undefined,
      mcdHeaders(mcdToken),
    ),
  mcdMenu: (storeCode: string, names: string[] = [], mcdToken = '') =>
    request<McdMenuPayload>(
      'GET',
      `/mcd/menu?store_code=${encodeURIComponent(storeCode)}${names.length ? `&names=${encodeURIComponent(names.join('|'))}` : ''}`,
      undefined,
      mcdHeaders(mcdToken),
    ),
  mcdQuote: (storeCode: string, items: Array<{ product_code: string; quantity: number }>, mcdToken = '') =>
    request<McdQuote>('POST', '/mcd/quote', { store_code: storeCode, items }, mcdHeaders(mcdToken)),
  mcdCreateOrder: (
    storeCode: string,
    items: Array<{ product_code: string; quantity: number }>,
    takeWayCode = '',
    mcdToken = '',
  ) =>
    request<McdOrder>('POST', '/mcd/orders', {
      store_code: storeCode,
      items,
      take_way_code: takeWayCode,
    }, mcdHeaders(mcdToken)),
  mcdGetOrder: (orderId: string, mcdToken = '') =>
    request<McdOrder>('GET', `/mcd/orders/${orderId}`, undefined, mcdHeaders(mcdToken)),
  mcdCancelOrder: (orderId: string, mcdToken = '') =>
    request<{ order_id: string; cancelled: boolean }>(
      'POST', `/mcd/orders/${orderId}/cancel`, {}, mcdHeaders(mcdToken),
    ),

  foodOptions: (planId: string, date: string, mealSlot: string, refresh = false) =>
    request<FoodOptionPayload>(
      'GET',
      `/food/options?plan_id=${planId}&date=${date}&meal_slot=${mealSlot}${refresh ? '&refresh=true' : ''}`,
    ),
  startTodayPlan: (text: string, date?: string, mcdToken = '') =>
    request<{ run_id: string; status: string }>(
      'POST', '/agent/today-plan', { text, date, mcd_token: mcdToken || undefined },
    ),
  feedbackTodayPlan: (runId: string, text: string, mcdToken = '') =>
    request<{ run_id: string; status: string }>(
      'POST',
      `/agent/today-plan/${runId}/feedback`,
      { text, mcd_token: mcdToken || undefined },
    ),
  applyAgentMeals: (runId: string, selections: unknown[], date?: string) =>
    request<{ new_version: number }>('POST', `/agent/today-plan/${runId}/apply`, {
      selections,
      date,
    }),
  askKnowledge: (question: string, bodyPart?: string) =>
    request<KnowledgeAnswer>('POST', '/knowledge/ask', {
      question,
      body_part: bodyPart || null,
    }),
  createPlanDraft: async (instruction: string) =>
    normalizeDraft(await request<PlanDraft>('POST', '/plans/drafts', { instruction })),
  getPlanDraft: async (draftId: string) =>
    normalizeDraft(await request<PlanDraft>('GET', `/plans/drafts/${draftId}`)),
  feedbackPlanDraft: async (draftId: string, text: string) =>
    normalizeDraft(await request<PlanDraft>('POST', `/plans/drafts/${draftId}/feedback`, { text })),
  confirmPlanDraft: (draftId: string, comment = '') =>
    request<{ draft_id: string; committed_version: number; plan: CurrentPlan }>(
      'POST',
      `/plans/drafts/${draftId}/confirm`,
      { comment },
    ),
  rejectPlanDraft: (draftId: string, comment = '') =>
    request<PlanDraft>('POST', `/plans/drafts/${draftId}/reject`, { comment }),
  listPatchPresets: () => request<PatchPreset[]>('GET', '/patch-presets'),
  runtimeConversations: () => request<any[]>('GET', '/agent/runtime/conversations'),
  runtimeCreate: () => request<any>('POST', '/agent/runtime/conversations', {}),
  runtimeMessages: (thread: string, before = '') => request<any>('GET', `/agent/runtime/conversations/${encodeURIComponent(thread)}?before=${encodeURIComponent(before)}`),
  runtimeTurn: (id: string) => request<any>('GET', `/agent/runtime/turns/${encodeURIComponent(id)}`),
  listRuns: (limit = 20, status = '') =>
    request<RunSummary[]>('GET', `/runs?limit=${limit}${status ? `&status=${status}` : ''}`),
  getRun: (runId: string) => request<RunDetail>('GET', `/runs/${runId}`),

  // ---- 统一教练 Agent：所有页面共用同一个入口 ----
  recoverSavedChat,
  recoverableChatThread: () => lastSavedChatRequest(accountStorageId())?.thread || '',
  commerceStatus: () => request<{ status: 'ready' | 'token_required'; guide_url: string; allow_order: boolean }>('GET', '/agent/commerce/status', undefined, mcdHeaders(getStoredMcdToken())),
  commerceOrders: (group: import('./types').CommerceOrderGroup = 'active', cursor = '') => request<import('./types').CommerceOrderPage>('GET', `/agent/commerce/orders?group=${group}&limit=20&cursor=${encodeURIComponent(cursor)}`, undefined, mcdHeaders(getStoredMcdToken())),
  commerceOrderDetail: (id: string) => request<import('./types').CommerceHistoryOrder>('GET', `/agent/commerce/orders/${encodeURIComponent(id)}`, undefined, mcdHeaders(getStoredMcdToken())),
  refreshCommerceOrder: (id: string) => request<import('./types').CommerceHistoryOrder>('POST', `/agent/commerce/orders/${encodeURIComponent(id)}/refresh`, {}, mcdHeaders(getStoredMcdToken())),
  syncCommerceOrders: () => request<{checked:number;failed:number;remaining:number;synced_at:string}>('POST', '/agent/commerce/orders/sync', {}, mcdHeaders(getStoredMcdToken())),
  commercePreferences: () => request<{city: string; keyword: string}>('GET', '/agent/commerce/preferences'),
  saveCommercePreferences: (city: string, keyword: string) => request<{city: string; keyword: string}>('PUT', '/agent/commerce/preferences', {city,keyword}),
  agentChat: (
    text: string,
    page = 'today',
    date = '',
    sessionId = '',
    context: Record<string, unknown> = {},
    thread = '',
    requestId = `coach-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    confirmation?: { action: 'confirm' | 'cancel'; pending_id: string; version: number },
    checkoutAction?: import('./types').CheckoutAction,
    commerceAction?: import('./types').CommerceAction,
  ) =>
    chatWithRecovery({
      request_id: requestId,
      confirmation,
      checkout_action: checkoutAction,
      commerce_action: commerceAction,
      text,
      page,
      date: date || undefined,
      session_id: sessionId || undefined,
      context,
      thread: thread || undefined,
    }, mcdHeaders(getStoredMcdToken())),
  agentMessages: (limit = 20, thread = 'coach') =>
    request<AgentMessageItem[]>(
      'GET',
      `/agent/messages?limit=${limit}&thread=${encodeURIComponent(thread)}`,
    ),
  listAgentConversations: () =>
    request<AgentConversation[]>('GET', '/agent/conversations'),
  deleteAgentConversation: (thread: string) =>
    request<{ thread: string; deleted: number }>(
      'DELETE',
      `/agent/conversations/${encodeURIComponent(thread)}`,
    ),
  getDayRevision: (id: string) => request<import('./types').DayRevision>('GET', `/agent/day-revisions/${encodeURIComponent(id)}`),
  getWorkoutReplan: (id: string) => request<import('./types').WorkoutReplanState>('GET', `/agent/workout-replans/${encodeURIComponent(id)}`),
  getAgentDay: (date = '', fallbackNext = true) =>
    request<AgentDay>('GET', `/agent/day?date=${encodeURIComponent(date)}&fallback_next=${fallbackNext}`),
  resetAgentDay: (date = '') =>
    request<AgentDay>('POST', '/agent/day/reset', { date: date || undefined }),
  listAdjustments: (start = '', end = '', limit = 60) =>
    request<{ items: AgentAdjustment[] }>(
      'GET',
      `/agent/adjustments?start=${start}&end=${end}&limit=${limit}`,
    ),
  restoreAdjustment: (adjustmentId: string) =>
    request<{ adjustment: AgentAdjustment; day: AgentDay }>(
      'POST', `/agent/adjustments/${adjustmentId}/restore`, {},
    ),
  getProgressInsights: (days = 14) =>
    request<ProgressInsights>('GET', `/agent/progress-insights?days=${days}`),
  getMemoryStatus: () =>
    request<{
      short_term_retention_days: number
      temporary_context_retention_days: number
      cleanup_interval_seconds: number
      last_run: Record<string, unknown> | null
      last_run_at: string
    }>('GET', '/agent/memory/status'),
  listMemories: (kind = '') =>
    request<AgentMemoryList>('GET', `/agent/memories${kind ? `?kind=${kind}` : ''}`),
  updateMemory: (memoryId: string, payload: { value?: string; structured?: Record<string, unknown> }) =>
    request<MemoryItem>('PUT', `/agent/memories/${memoryId}`, payload),
  deleteMemory: (memoryId: string) =>
    request<{ deleted: string; value: string }>('DELETE', `/agent/memories/${memoryId}`),

  // ---- 训练执行 ----
  startWorkout: (date = '') =>
    request<{ session: WorkoutSession; day: AgentDay }>(
      'POST', '/workout-sessions', { date: date || undefined },
    ),
  getActiveWorkout: (date = '') =>
    request<{ session: WorkoutSession | null; day: AgentDay }>(
      'GET', `/workout-sessions/active${date ? `?date=${date}` : ''}`,
    ),
  getWorkout: (sessionId: string) =>
    request<WorkoutSession>('GET', `/workout-sessions/${sessionId}`),
  completeWorkoutSet: (sessionId: string, payload?: {
    reps?: number | null
    weight_kg?: number | null
    rpe?: number | null
  }) => request<{ session: WorkoutSession }>(
    'POST', `/workout-sessions/${sessionId}/sets`, payload || {},
  ),
  skipWorkoutAction: (sessionId: string, reason = '') =>
    request<{ session: WorkoutSession }>(
      'POST', `/workout-sessions/${sessionId}/skip`, { reason },
    ),
  replaceWorkoutAction: (
    sessionId: string,
    payload: {
      reason?: string
      action_ref?: string
      equipment_busy?: boolean
      injured_regions?: string[]
      easier?: boolean
      minutes_remaining?: number
    },
  ) =>
    request<{ session: WorkoutSession; changes: string[] }>(
      'POST', `/workout-sessions/${sessionId}/replace`, payload,
    ),
  pauseWorkout: (sessionId: string) =>
    request<{ session: WorkoutSession }>(
      'POST', `/workout-sessions/${sessionId}/pause`, {},
    ),
  finishWorkout: (
    sessionId: string,
    payload: {
      completed: boolean
      completion_basis?: 'recorded_only' | 'confirm_current_plan'
      expected_revision?: string
      completion_pct?: number
      difficulty: number
      fatigue: number
      actual_minutes?: number
      note?: string
    },
  ) =>
    request<{
      session: WorkoutSession
      summary: Record<string, unknown>
      training_state: Record<string, unknown>
      logs: Array<Record<string, unknown>>
      day: AgentDay
    }>('POST', `/workout-sessions/${sessionId}/finish`, payload),
}
