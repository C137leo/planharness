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
  kcal: number | null
  meta: Record<string, unknown>
}

export interface MealItem {
  name: string
  grams: number
  kcal: number
  protein: number
  fat: number
  carb: number
}

export interface MealPlan {
  day_index: number
  slot: string
  title: string
  items: MealItem[]
  kcal: number
  protein_g: number
  fat_g: number
  carb_g: number
}

export interface TodayPlan {
  weekday: number
  date: string
  actions: PlanAction[]
  meals: MealPlan[]
}

export interface NutritionMacro {
  low_g?: number
  high_g?: number
  low?: number
  high?: number
}

export interface NutritionTargets {
  status?: string
  bmr?: number
  tdee?: number
  target_kcal?: number
  delta_kcal?: number
  macros?: Record<string, NutritionMacro>
  scale_check?: string
  cautions?: string[]
  boundaries?: string[]
}

export interface CurrentPlan {
  plan_id: string
  status: string
  version: number
  calorie_target: number
  protein_target_g: number
  step_target: number
  summary: string
  source: 'template' | 'coach'
  nutrition: NutritionTargets
  program: Record<string, unknown>
  actions: PlanAction[]
  meals: MealPlan[]
  today: TodayPlan | null
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
  plan_id?: string | null
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

export interface RunSummaryWithPlan extends RunSummary {
  plan_id?: string | null
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

// ---------------- 审计链 ----------------

export interface AuditEvent {
  seq: number
  action: string
  actor_type: string
  actor_id: string
  payload: Record<string, unknown>
  payload_hash: string
  prev_hash: string
  hash: string
  created_at: string | null
}

export interface AuditVerify {
  stream_id: string
  valid: boolean
  count: number
  broken_at: number | null
  reason: string
  checks: string[]
}

// ---------------- A/B/C 三模式对比 ----------------

export interface CompareMode {
  mode: 'A' | 'B' | 'C'
  label: string
  changed_slots: number
  total_slots: number
  churn: number
  unnecessary_churn: number
  violations: number
  violation_refs: string[]
  approval: boolean
  notes: string
  gate_changes?: string[]
}

export interface CompareResult {
  scenario: string
  constraint: string
  fixture_note: string
  modes: CompareMode[]
}

// ---------------- 一句话改计划（patch-requests） ----------------

export interface PatchIntent {
  kind: string
  body_parts: string[]
  target_body_parts: string[]
  available_days: number | null
  duration_min: number | null
  notes: string
}

export interface PatchDraft {
  patch_id: string
  operations: PatchOperation[]
  summary: string
  explanation: string
  risks: string[]
  diff: Record<string, unknown>
  status: string
}

/** status: ready(可审批) / clarifying(需要追问) / invalid(被安全门拦下) / applied / rejected */
export interface PatchRequest {
  request_id: string
  status: string
  round: number
  intent: PatchIntent | Record<string, never>
  question: string
  raw_text: string
  parent_request_id: string | null
  draft: PatchDraft | null
  created_at: string | null
}

// ---------------- 训练记录与热量 ----------------

export type WorkoutKind = 'strength' | 'cardio' | 'flexibility'

export interface WorkoutLogPayload {
  date?: string
  action_ref: string
  kind: WorkoutKind
  duration_min: number
  sets?: number | null
  reps?: number | null
  intensity?: 'light' | 'normal' | 'hard'
  note?: string
}

export interface WorkoutLogItem {
  log_id: string
  date: string
  action_ref: string
  kind: WorkoutKind
  duration_min: number
  met: number
  met_fallback: boolean
  kcal: number
  weight_source: 'checkin' | 'profile' | 'default'
}

export interface CalorieDay {
  date: string
  kcal: number
  duration_min: number
  sessions: number
}

export interface CalorieStats {
  weight_kg: number
  weight_source: 'checkin' | 'profile' | 'default'
  total_kcal: number
  total_duration_min: number
  sessions: number
  days: CalorieDay[]
  by_kind: Record<string, number>
  formula: string
  scope: 'burn_only'
  note: string
  since: string
  days_window: number
}

export interface Profile {
  user_id: string
  sex: string
  birth_year: number
  height_cm: number
  weight_kg: number
  activity_level: string
  training_experience: string
  equipment: string[]
  weekly_days: number
  session_minutes: number
  diet_pattern: string
  allergies: string[]
  dislikes: string[]
  timezone: string
}

export interface CheckinRecord {
  id: string
  date: string
  weight_kg: number | null
  sleep_hours: number | null
  hunger_level: number | null
  soreness_level: number | null
  adherence_pct: number | null
  note: string
}

// ---------------- 教练计划草案（生成 → 多轮修改 → 确认） ----------------

export interface DraftAction {
  slot: string
  seq: number
  action_type: string
  action_ref: string
  sets: number | null
  reps: number | null
  duration_min: number | null
  meta: Record<string, unknown>
}

export interface DraftDay {
  day_index: number
  session_id?: string
  focus: string
  minimum_version?: string
  actions: DraftAction[]
}

export interface PlanDraft {
  draft_id: string
  status: 'ready' | 'clarifying' | 'invalid' | 'blocked' | 'confirmed' | 'rejected' | 'superseded'
  round: number
  instruction: string
  feedback: string[]
  question: string
  summary: string
  highlights: string[]
  cautions: string[]
  warnings: string[]
  program: Record<string, unknown>
  prescription: Record<string, unknown>
  nutrition: NutritionTargets
  days: DraftDay[]
  meals: MealPlan[]
  meta: {
    skill_available?: boolean
    skill_version?: string
    tools_called?: string[]
    model_fallback?: boolean
    usage?: Record<string, number>
  }
  trace: Array<Record<string, unknown>>
  created_at: string | null
}

export interface PatchPreset {
  key: string
  label: string
  example: string
  hint: string
}

export interface ProfileUpdate {
  sex?: 'male' | 'female'
  birth_year?: number
  height_cm?: number
  weight_kg?: number
  goal?: 'fat_loss' | 'muscle_gain' | 'cardio' | 'health'
  goal_note?: string
  weekly_days?: number
  session_minutes?: number
  activity_level?: 'sedentary' | 'light' | 'moderate' | 'active'
  training_experience?: 'beginner' | 'intermediate' | 'advanced'
  equipment?: string[]
  diet_pattern?: 'omnivore' | 'vegetarian' | 'vegan' | 'pescatarian'
  allergies?: string[]
  diet_preferences?: string[]
  dislikes?: string[]
}
