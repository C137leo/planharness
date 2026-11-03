export interface PlanAction {
  id: string
  day_index: number
  slot: string
  seq: number
  slot_key: string
  action_type: 'exercise' | 'meal'
  action_ref: string
  sets: number | null
  reps: number | null
  duration_min: number | null
  intensity: string | null
  portion_g: number | null
}

export interface CurrentPlan {
  plan_id: string
  status: string
  version: number
  calorie_target: number
  protein_target_g: number
  step_target: number
  summary: string
  actions: PlanAction[]
}

export interface PatchOperation {
  op: 'replace_action' | 'remove_action'
  slot_key: string
  before_action_ref?: string
  after_action?: {
    action_ref: string
    sets?: number | null
    reps?: number | null
    duration_min?: number | null
    meta?: Record<string, unknown>
  }
  reason: string
}

export interface PlanPatch {
  id: string
  plan_id: string
  base_version: number
  reason_code: string
  reason_text: string
  status: string
  operations: PatchOperation[]
  validation: {
    passed?: boolean
    proposed_actions?: number
    safety?: string[]
    explanation?: string
  }
  diff: {
    total_slots: number
    changed_slots: number
    churn: number
    required_changes: number
    unnecessary_churn: number
    unchanged_ratio: number
    available_days?: number
  }
  created_at: string
}

export interface CheckinPayload {
  date?: string
  weight_kg?: number | null
  sleep_hours?: number | null
  hunger_level?: number | null
  soreness_level?: number | null
  adherence_pct?: number | null
  note?: string
}

export interface ApiEnvelope<T> {
  code: number
  message: string
  data: T
  trace_id: string
}

// ---------------- Run 观测（Console） ----------------

export interface TraceEntry {
  node: string
  status: string
  ts: string
  detail?: Record<string, unknown> | null
}

export interface RunSummary {
  id: string
  kind: string
  mode: string
  status: string
  stop_reason: string | null
  patch_id: string | null
  created_at: string
  finished_at: string | null
  latency_ms: number
  error_code: string | null
}

export interface VerificationCheck {
  name: string
  passed: boolean
  detail: string
}

export interface RunVerification {
  passed: boolean
  checks: VerificationCheck[]
}

export interface RunPatch {
  id: string
  status: string
  base_version: number
  reason_code: string
  reason_text: string
  diff: Record<string, number>
  operations: PatchOperation[]
}

export interface RunContextSection {
  name: string
  tokens: number
  priority?: number
  truncated?: boolean
}

export interface RunContext {
  token_count: number
  token_budget: number
  dropped: Array<{ name?: string; reason?: string; tokens?: number }>
  compressed: boolean
  compression_method: string | null
  sections: RunContextSection[]
}

export interface RunDetail extends RunSummary {
  trace: TraceEntry[]
  input: Record<string, unknown>
  output: Record<string, unknown>
  patch: RunPatch | null
  context: RunContext | null
  verification: RunVerification | null
}

export interface PlanPatchSummary {
  id: string
  plan_id: string
  base_version: number
  status: string
  reason_code: string
  reason_text: string
  created_at: string | null
  diff: Record<string, number>
}

export interface EvalScenario {
  id: string
  title: string
  description: string
}

export interface EvalScenarioResult {
  id: string
  title: string
  passed: boolean
  error: string | null
  latency_ms: number
  checks: Array<{ name: string; ok: boolean; detail: string }>
  metrics: {
    tokens_in: number
    tokens_out: number
    cost_cny: number | null
    llm_fallback: boolean | null
    churn: number | null
    safety_violations: number
  }
}
