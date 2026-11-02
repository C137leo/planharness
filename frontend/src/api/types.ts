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
