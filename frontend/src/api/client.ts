import type {
  ApiEnvelope,
  CheckinPayload,
  CurrentPlan,
  EvalScenario,
  EvalScenarioResult,
  PlanPatch,
  PlanPatchSummary,
  RunDetail,
  RunSummary,
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
  method: 'GET' | 'POST' | 'PUT',
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

export async function ensureToken(): Promise<string> {
  const existing = getStoredToken()
  if (existing) return existing
  const data = await rawRequest<{ access_token: string }>('POST', '/auth/demo', {})
  setStoredToken(data.access_token)
  return data.access_token
}

async function request<T>(method: 'GET' | 'POST' | 'PUT', path: string, data?: unknown): Promise<T> {
  let token = await ensureToken()
  try {
    return await rawRequest<T>(method, path, data, token)
  } catch (error) {
    if (error instanceof ApiError && error.statusCode === 401) {
      uni.removeStorageSync(TOKEN_KEY)
      token = await ensureToken()
      return await rawRequest<T>(method, path, data, token)
    }
    throw error
  }
}

export const api = {
  getCurrentPlan: () => request<CurrentPlan>('GET', '/plans/current'),
  generatePlan: () => request<CurrentPlan>('POST', '/plans/generate'),
  createKneePatch: (planId: string, availableDays = 2) =>
    request<PlanPatch>('POST', `/plans/${planId}/patches`, {
      reason_code: 'injury',
      available_days: availableDays,
    }),
  getPatch: (patchId: string) => request<PlanPatch>('GET', `/patches/${patchId}`),
  listPatches: (limit = 20) => request<PlanPatchSummary[]>('GET', `/patches?limit=${limit}`),
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
  listEvalScenarios: () => request<EvalScenario[]>('GET', '/evals/scenarios'),
  runEvalScenario: (id: string) =>
    request<EvalScenarioResult>('POST', `/evals/scenarios/${id}/run`),
  listRuns: (limit = 20, status = '') =>
    request<RunSummary[]>('GET', `/runs?limit=${limit}${status ? `&status=${status}` : ''}`),
  getRun: (runId: string) => request<RunDetail>('GET', `/runs/${runId}`),
}
