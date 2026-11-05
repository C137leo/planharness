import type {
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
  McdStore,
  McdMenuPayload,
  McdQuote,
  McdOrder,
} from './types'

const API_BASE = (import.meta.env.VITE_API_BASE || '/api/v1').replace(/\/$/, '')
const TOKEN_KEY = 'planharness_token'

export class ApiError extends Error {
  constructor(
    message: string,
    public statusCode = 0,
    public detail: unknown = null,
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

function parseError(data: unknown, statusCode: number): ApiError {
  const body = data as { detail?: unknown; message?: string } | null
  const detail = body?.detail
  let message = body?.message || `请求失败（${statusCode}）`
  if (typeof detail === 'string') message = detail
  if (Array.isArray(detail)) message = detail.join('；')
  return new ApiError(message, statusCode, detail)
}

async function rawRequest<T>(
  method: 'GET' | 'POST' | 'PUT' | 'DELETE',
  path: string,
  data?: unknown,
  token = '',
): Promise<T> {
  return new Promise((resolve, reject) => {
    uni.request({
      url: `${API_BASE}${path}`,
      method,
      data: data as object,
      header: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
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

async function request<T>(method: 'GET' | 'POST' | 'PUT' | 'DELETE', path: string, data?: unknown): Promise<T> {
  let token: string
  try {
    token = await ensureToken()
  } catch (err) {
    if (err instanceof ApiError && err.statusCode === 401) handleUnauthorized()
    throw err
  }
  try {
    return await rawRequest<T>(method, path, data, token)
  } catch (error) {
    if (error instanceof ApiError && error.statusCode === 401) {
      // 不再悄悄换一个身份重试：把用户送回登录页，由他决定登哪个账号
      handleUnauthorized()
    }
    throw error
  }
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
  // ---- 麦当劳点餐：找门店 → 菜单 → 算价 → 建单（付款留给用户）----
  mcdStatus: () => request<{ available: boolean; allow_order: boolean }>('GET', '/mcd/status'),
  mcdStores: (city: string, keyword: string) =>
    request<{ stores: McdStore[] }>(
      'GET',
      `/mcd/stores?city=${encodeURIComponent(city)}&keyword=${encodeURIComponent(keyword)}`,
    ),
  mcdMenu: (storeCode: string, names: string[] = []) =>
    request<McdMenuPayload>(
      'GET',
      `/mcd/menu?store_code=${encodeURIComponent(storeCode)}${names.length ? `&names=${encodeURIComponent(names.join('|'))}` : ''}`,
    ),
  mcdQuote: (storeCode: string, items: Array<{ product_code: string; quantity: number }>) =>
    request<McdQuote>('POST', '/mcd/quote', { store_code: storeCode, items }),
  mcdCreateOrder: (
    storeCode: string,
    items: Array<{ product_code: string; quantity: number }>,
    takeWayCode = '',
  ) =>
    request<McdOrder>('POST', '/mcd/orders', {
      store_code: storeCode,
      items,
      take_way_code: takeWayCode,
    }),
  mcdGetOrder: (orderId: string) => request<McdOrder>('GET', `/mcd/orders/${orderId}`),
  mcdCancelOrder: (orderId: string) =>
    request<{ order_id: string; cancelled: boolean }>('POST', `/mcd/orders/${orderId}/cancel`, {}),

  foodOptions: (planId: string, date: string, mealSlot: string, refresh = false) =>
    request<FoodOptionPayload>(
      'GET',
      `/food/options?plan_id=${planId}&date=${date}&meal_slot=${mealSlot}${refresh ? '&refresh=true' : ''}`,
    ),
  startTodayPlan: (text: string, date?: string) =>
    request<{ run_id: string; status: string }>('POST', '/agent/today-plan', { text, date }),
  feedbackTodayPlan: (runId: string, text: string) =>
    request<{ run_id: string; status: string }>(
      'POST',
      `/agent/today-plan/${runId}/feedback`,
      { text },
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
  listRuns: (limit = 20, status = '') =>
    request<RunSummary[]>('GET', `/runs?limit=${limit}${status ? `&status=${status}` : ''}`),
  getRun: (runId: string) => request<RunDetail>('GET', `/runs/${runId}`),
}
