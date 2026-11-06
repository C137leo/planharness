<script setup lang="ts">
import { computed, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import {
  api,
  type AuditEvent,
  type AuditVerify,
  type CompareResult,
  type EvalScenario,
  type EvalScenarioResult,
  type PlanVersionHistory,
  type RunDetail,
  type RunSummary,
} from '../../api'

const runs = ref<RunSummary[]>([])
const selected = ref<RunDetail | null>(null)
const error = ref('')
const loading = ref(false)
const statusFilter = ref('')
const limit = ref(20)
const versions = ref<PlanVersionHistory | null>(null)
const auditEvents = ref<AuditEvent[]>([])
const auditPlanId = ref('')
const auditVerify = ref<AuditVerify | null>(null)
const auditBusy = ref(false)
const auditOpen = ref(false)

/** 审计默认只露 12 条：一屏 120 行没人看得完，展开才看全部 */
const auditShown = computed(() =>
  auditOpen.value ? auditEvents.value : auditEvents.value.slice(0, 12),
)
const auditHidden = computed(() =>
  Math.max(0, auditEvents.value.length - auditShown.value.length),
)
const compare = ref<CompareResult | null>(null)
const comparing = ref(false)
const scenarios = ref<EvalScenario[]>([])
const replay = ref<EvalScenarioResult | null>(null)
const replaying = ref('')

const FILTERS = [
  { value: '', label: '全部' },
  { value: 'succeeded', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'rejected', label: '已驳回' },
  { value: 'running', label: '运行中' },
  { value: 'budget_exceeded', label: '超预算' },
]

const KIND_LABEL: Record<string, string> = {
  today_plan: '今日规划',
  patch_approval: '补丁审批',
  patch_rejection: '补丁驳回',
  rollback: '回滚',
  generate_plan: '生成计划',
  patch: '补丁',
  compare: 'A/B/C 对比',
}

const ACTION_LABEL: Record<string, string> = {
  patch_created: '生成改动草案',
  approval_granted: '审批通过',
  approval_rejected: '审批拒绝',
  patch_committed: '改动已提交（新版本）',
  rollback_committed: '回滚（新版本）',
  plan_draft_created: '计划草案生成',
  plan_draft_revised: '计划草案修订',
  plan_draft_rejected: '计划草案驳回',
  plan_confirmed: '计划确认',
  profile_updated: '资料更新',
  verification_failed: '验证失败',
  mcd_order_created: '麦当劳下单（待支付）',
  mcd_order_cancelled: '取消麦当劳订单',
}

const STATUS_LABEL: Record<string, string> = {
  running: '运行中',
  succeeded: '已完成',
  failed: '失败',
  rejected: '已驳回',
  budget_exceeded: '超预算',
}

const STOP_LABEL: Record<string, string> = {
  completed: '验证通过',
  committed_pending_verification: '待验证',
  verification_failed_rolled_back: '验证失败 → 已回滚',
  rollback_failed: '回滚失败',
  budget_exceeded: '超出预算（未调用模型）',
  approval_rejected: '人工驳回',
  prepare_failed: '准备阶段失败',
  commit_failed: '提交失败',
}

const churn = computed(() => {
  const diff = selected.value?.patch?.diff
  if (!diff || typeof diff.churn !== 'number') return null
  return Math.round(diff.churn * 100)
})

/** 没被碰过的槽位：最小补丁的证据（改了 3 个，其余 9 个原样保留） */
const unchangedSlots = computed(() => {
  const diff = selected.value?.patch?.diff
  if (!diff || typeof diff.total_slots !== 'number') return null
  return diff.total_slots - (diff.changed_slots || 0)
})

const patchOps = computed(() => selected.value?.patch?.operations || [])

/** 安全门：补丁触发的规则 / 红旗阻断都在这里汇总（Console 的 SAFETY 卡） */
const safetyInfo = computed(() => {
  const patch = selected.value?.patch
  if (patch) {
    const hits = patch.validation?.safety || []
    return {
      kind: 'patch' as const,
      passed: hits.length === 0,
      hits,
      approved: patch.validation?.approved === true,
      scope: patch.validation?.intent?.kind || patch.reason_code,
    }
  }
  const output = selected.value?.output as
    | { blocked?: boolean; reason?: string; summary?: string }
    | undefined
  if (output?.blocked) {
    return {
      kind: 'red_flag' as const,
      passed: false,
      hits: [
        output.reason ||
          output.summary ||
          '命中红旗症状关键词，停止规划并建议就医',
      ],
      approved: false,
      scope: 'red_flag',
    }
  }
  return { kind: 'none' as const, passed: true, hits: [] as string[], approved: false, scope: '' }
})

/** 操作清单的一行："替换：深蹲 → 臀桥" */
function opLabel(op: { op: string; before_name?: string; before_action_ref?: string; slot_key: string }) {
  return op.before_name || op.before_action_ref || op.slot_key
}

function opAfter(op: { after_name?: string; after_action?: { action_ref: string } }) {
  return op.after_name || op.after_action?.action_ref || ''
}

const droppedCount = computed(() => selected.value?.context?.dropped?.length ?? 0)
const failedChecks = computed(
  () => selected.value?.verification?.checks.filter((c) => !c.passed) ?? [],
)

function label(map: Record<string, string>, key?: string | null): string {
  if (!key) return '—'
  return map[key] || key
}

function shortId(id?: string | null): string {
  return id ? `${id.slice(0, 8)}…` : '—'
}

function clock(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

async function loadAudit(planId?: string | null) {
  auditEvents.value = []
  auditVerify.value = null
  auditPlanId.value = ''
  try {
    // 没有关联计划（比如今日规划）就退回当前计划的审计流，别显示成"没有审计"
    let target = planId || ''
    if (!target) {
      const plan = await api.getCurrentPlan()
      target = plan.plan_id
    }
    if (!target) return
    auditEvents.value = await api.getAudit(target)
    auditPlanId.value = target
  } catch {
    auditEvents.value = []
  }
}

async function verifyAuditChain() {
  const planId = auditPlanId.value || selected.value?.plan_id
  if (!planId || auditBusy.value) return
  auditBusy.value = true
  try {
    auditVerify.value = await api.verifyAudit(planId)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '验证失败'
  } finally {
    auditBusy.value = false
  }
}

function copyCheckpoint() {
  const verify = auditVerify.value
  if (!verify) return
  const payload = {
    stream_id: verify.stream_id,
    head_seq: verify.head_seq,
    head_hash: verify.head_hash,
    signature: verify.signature,
    algorithm: verify.signature_alg,
  }
  uni.setClipboardData({
    data: JSON.stringify(payload),
    success: () => uni.showToast({ title: '校验点已复制', icon: 'none' }),
  })
}

async function select(id: string) {
  try {
    selected.value = await api.getRun(id)
    await Promise.all([loadAudit(selected.value.plan_id), loadVersions(selected.value.plan_id)])
  } catch (err) {
    error.value = err instanceof Error ? err.message : '读取 run 失败'
  }
}

/** 版本链：没有关联计划就退回"当前计划"的版本历史 */
async function loadVersions(planId: string | null) {
  versions.value = null
  try {
    let target = planId
    if (!target) {
      const plan = await api.getCurrentPlan()
      target = plan.plan_id
    }
    if (!target) return
    versions.value = await api.planVersions(target, 8)
  } catch {
    versions.value = null // 没有计划就不显示这一块
  }
}

function versionChanges(row: PlanVersionHistory['versions'][number]) {
  const changes = row.changes
  if (!changes) return '初始版本'
  const meals = (changes as unknown as { meals_changed?: number }).meals_changed
  return `改 ${changes.changed_slots}/${changes.total_slots} 个槽位${meals ? ` · ${meals} 餐` : ''}`
}

async function loadScenarios() {
  try {
    scenarios.value = await api.listEvalScenarios()
  } catch {
    scenarios.value = []
  }
}

async function runCompare() {
  if (comparing.value) return
  comparing.value = true
  try {
    compare.value = await api.compareKneeModes()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '对比失败'
  } finally {
    comparing.value = false
  }
}

async function runScenario(id: string) {
  if (replaying.value) return
  replaying.value = id
  replay.value = null
  try {
    replay.value = await api.runEvalScenario(id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '回放失败'
  } finally {
    replaying.value = ''
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    // "超预算"不是 run 的生命周期状态，而是 output.budget 上的标记：
    // 先按无状态取，再在前端按字段过滤（否则永远查不到）
    const budgetFilter = statusFilter.value === 'budget_exceeded'
    const fetched = await api.listRuns(limit.value, budgetFilter ? '' : statusFilter.value)
    runs.value = budgetFilter ? fetched.filter((run) => run.budget_exceeded) : fetched
    if (runs.value.length > 0) {
      await select(selected.value?.id && runs.value.some((r) => r.id === selected.value?.id)
        ? selected.value.id
        : runs.value[0].id)
    } else {
      selected.value = null
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function changeFilter(value: string) {
  if (statusFilter.value === value) return
  statusFilter.value = value
  limit.value = 20
  selected.value = null
  await load()
}

async function loadMore() {
  limit.value += 20
  await load()
}

onShow(() => {
  load()
  loadScenarios()
})
</script>

<template>
  <view class="page wide">
    <view class="eyebrow">HARNESS CONSOLE</view>
    <view class="title">每次运行，都留得下证据。</view>
    <view class="subtitle">真实 run · trace 时间线 · diff · 上下文预算 · 独立验证</view>

    <view v-if="error" class="error">{{ error }}</view>

    <view class="console-grid">
      <view class="console-col">
    <view class="card">
      <view class="row">
        <view class="eyebrow">A/B/C COMPARISON</view>
        <view class="chip">{{ comparing ? '计算中…' : '同场景 · 三配置' }}</view>
      </view>
      <view class="muted small" style="margin-top: 6px">
        第 2 周膝痛 + 只能练 2 天：A 裸奔 / B 安全门 / C 补丁，只改 Harness 配置。
      </view>

      <button class="btn" style="margin-top: 12px" :disabled="comparing" @click="runCompare">
        {{ comparing ? '正在跑三种模式…' : compare ? '重新跑一次' : '跑一次对比' }}
      </button>

      <template v-if="compare">
        <view class="matrix">
          <view class="matrix-row header">
            <text>指标</text><text>A 裸奔</text><text>B 安全门</text><text>C 补丁</text>
          </view>
          <view class="matrix-row">
            <text>违规</text>
            <text v-for="m in compare.modes" :key="m.mode" :class="{ warn: m.violations > 0 }">
              {{ m.violations }}
            </text>
          </view>
          <view class="matrix-row">
            <text>churn</text>
            <text v-for="m in compare.modes" :key="m.mode">{{ Math.round(m.churn * 100) }}%</text>
          </view>
          <view class="matrix-row">
            <text>不必要改动</text>
            <text v-for="m in compare.modes" :key="m.mode">{{ Math.round(m.unnecessary_churn * 100) }}%</text>
          </view>
          <view class="matrix-row">
            <text>审批</text>
            <text v-for="m in compare.modes" :key="m.mode">{{ m.approval ? '✓' : '✗' }}</text>
          </view>
          <view class="matrix-row">
            <text>改动槽位</text>
            <text v-for="m in compare.modes" :key="m.mode">{{ m.changed_slots }}/{{ m.total_slots }}</text>
          </view>
        </view>
        <view class="muted small" style="margin-top: 10px">{{ compare.fixture_note }}</view>
        <view v-for="m in compare.modes" :key="`n-${m.mode}`" class="muted small" style="margin-top: 4px">
          <text class="compare-note">{{ m.mode }}</text>{{ m.notes }}
        </view>
      </template>
    </view>

    <view class="card">
      <view class="row">
        <view class="eyebrow">SCENARIO REPLAY</view>
        <view class="chip">{{ replaying ? '执行中…' : `${scenarios.length} 个场景` }}</view>
      </view>
      <view class="muted small" style="margin-top: 6px">
        点一下就在服务端跑一次评测（临时库 + mock 模型，不写你的数据）。
      </view>

      <view class="scenario-grid">
        <view
          v-for="sc in scenarios"
          :key="sc.id"
          class="scenario"
          :class="{ running: replaying === sc.id }"
          @click="runScenario(sc.id)"
        >
          <text class="scenario-id">{{ sc.id }}</text>
          <text class="scenario-title">{{ sc.title }}</text>
        </view>
      </view>

      <view v-if="replay" class="replay-result">
        <view class="row">
          <text class="replay-title">{{ replay.id }} {{ replay.title }}</text>
          <text class="chip" :class="replay.passed ? 'succeeded' : 'failed'">
            {{ replay.passed ? '通过' : '失败' }}
          </text>
        </view>
        <view class="kv" style="margin-top: 8px">
          <view class="kv-item">
            <text class="k">耗时</text>
            <text class="v">{{ replay.latency_ms }} ms</text>
          </view>
          <view class="kv-item">
            <text class="k">成本</text>
            <text class="v">¥{{ Number(replay.metrics.cost_cny ?? 0).toFixed(4) }}</text>
          </view>
          <view class="kv-item">
            <text class="k">tokens</text>
            <text class="v">{{ replay.metrics.tokens_in }}/{{ replay.metrics.tokens_out }}</text>
          </view>
          <view class="kv-item">
            <text class="k">断言</text>
            <text class="v">{{ replay.checks.filter((c) => c.ok).length }}/{{ replay.checks.length }}</text>
          </view>
        </view>
        <view class="checks">
          <view
            v-for="check in replay.checks"
            :key="check.name"
            class="check"
            :class="{ bad: !check.ok }"
          >
            <text class="mark">{{ check.ok ? '✓' : '✗' }}</text>
            <text class="name">{{ check.name }}</text>
            <text class="muted small">{{ check.detail }}</text>
          </view>
        </view>
      </view>
    </view>

    <view class="card">
      <view class="row">
        <view class="eyebrow">RUNS · 执行记录</view>
        <view class="chip">{{ loading ? '加载中' : `${runs.length} 条` }}</view>
      </view>
      <text class="audit-intro">
        每次执行一条（今日规划 / 补丁审批 / 回滚），点开看 trace、diff、成本。
        计划本身的变更证据在下面的 AUDIT CHAIN，两者不是一回事。
      </text>

      <view class="filters">
        <view
          v-for="item in FILTERS"
          :key="item.value"
          class="filter"
          :class="{ active: statusFilter === item.value }"
          @click="changeFilter(item.value)"
        >
          {{ item.label }}
        </view>
      </view>

      <view v-if="runs.length === 0" class="muted">
        {{ statusFilter ? '这个状态下没有运行记录。' : '还没有运行记录。去「审批」页创建一次补丁，这里就会出现。' }}
      </view>

      <view
        v-for="run in runs"
        :key="run.id"
        class="run-item"
        :class="{ active: selected?.id === run.id }"
        @click="select(run.id)"
      >
        <view class="run-main">
          <text class="run-kind">{{ label(KIND_LABEL, run.kind) }}</text>
          <text class="run-id">{{ shortId(run.id) }}</text>
        </view>
        <view class="run-meta">
          <text class="status" :class="run.status">{{ label(STATUS_LABEL, run.status) }}</text>
          <text v-if="run.stop_reason" class="muted small">{{ label(STOP_LABEL, run.stop_reason) }}</text>
        </view>
        <view class="muted small">{{ clock(run.created_at) }}</view>
      </view>

      <button v-if="runs.length > 0 && runs.length >= limit" class="btn secondary" @click="loadMore">
        加载更多
      </button>
    </view>

      </view>

      <view class="console-col">
    <template v-if="selected">
      <view class="card">
        <view class="row">
          <view class="eyebrow">TRACE</view>
          <view class="chip" :class="selected.status">{{ label(STATUS_LABEL, selected.status) }}</view>
        </view>
        <view class="timeline">
          <view v-for="(step, idx) in selected.trace" :key="idx" class="step">
            <view class="dot" />
            <view class="step-main">
              <view class="step-head">
                <text class="node">{{ step.node }}</text>
                <text class="muted small">{{ step.status }}</text>
                <text class="muted small">{{ clock(step.ts) }}</text>
              </view>
              <view
                v-if="step.detail && Object.keys(step.detail).length"
                class="muted small"
              >
                <text>{{ JSON.stringify(step.detail) }}</text>
              </view>
            </view>
          </view>
          <view v-if="selected.trace.length === 0" class="muted">没有 trace 记录。</view>
        </view>
      </view>

      <view class="card">
        <view class="eyebrow">DIFF · 最小补丁</view>
        <view v-if="selected.patch" class="kv">
          <view class="kv-item">
            <text class="k">churn</text>
            <text class="v">{{ churn === null ? '—' : `${churn}%` }}</text>
          </view>
          <view class="kv-item">
            <text class="k">改动槽位</text>
            <text class="v">
              {{ selected.patch.diff.changed_slots ?? '—' }}/{{ selected.patch.diff.total_slots ?? '—' }}
            </text>
          </view>
          <view class="kv-item">
            <text class="k">操作数</text>
            <text class="v">{{ selected.patch.operations.length }}</text>
          </view>
          <view class="kv-item">
            <text class="k">基于版本</text>
            <text class="v">v{{ selected.patch.base_version }}</text>
          </view>
        </view>

        <template v-if="selected.patch">
          <view class="diff-sentence">
            原计划 {{ selected.patch.diff.total_slots ?? '—' }} 个槽位，改了
            {{ selected.patch.diff.changed_slots ?? '—' }} 个，
            其余 {{ unchangedSlots }} 个原样保留；必要改动
            {{ selected.patch.diff.required_changes ?? '—' }} 处，
            多余扰动 {{ Math.round((selected.patch.diff.unnecessary_churn || 0) * 100) }}%。
          </view>

          <view class="ops">
            <view v-for="op in patchOps" :key="op.slot_key" class="op-row">
              <text class="op-kind" :class="{ remove: op.op === 'remove_action' }">
                {{ op.op === 'remove_action' ? '删除' : '替换' }}
              </text>
              <view class="op-body">
                <text class="op-text">
                  {{ opLabel(op) }}<text v-if="op.op === 'replace_action'"> → {{ opAfter(op) }}</text>
                </text>
                <text class="muted small">{{ op.slot_key }}{{ op.reason ? ` · ${op.reason}` : '' }}</text>
              </view>
            </view>
          </view>

          <view class="muted small" style="margin-top: 8px">
            {{ selected.patch.reason_text || selected.patch.reason_code }}
          </view>
        </template>
        <view v-else class="muted">这次运行没有关联补丁。</view>
      </view>

      <view class="card">
        <view class="row">
          <view class="eyebrow">SAFETY · 安全门</view>
          <view class="chip" :class="safetyInfo.passed ? 'succeeded' : 'failed'">
            {{ safetyInfo.kind === 'none' ? '未触发' : safetyInfo.passed ? '通过' : '拦截' }}
          </view>
        </view>

        <template v-if="safetyInfo.kind === 'none'">
          <view class="muted small" style="margin-top: 8px">
            这次运行没有补丁、也没有红旗症状，安全门未介入。
          </view>
        </template>
        <template v-else>
          <view class="safety-scope">
            作用域：{{ safetyInfo.scope }}
            <text v-if="safetyInfo.kind === 'patch'">
              · 审批时重跑安全门：{{ safetyInfo.approved ? '已通过' : '未提交' }}
            </text>
          </view>
          <view v-if="safetyInfo.hits.length" class="checks">
            <view v-for="(hit, index) in safetyInfo.hits" :key="index" class="check bad">
              <text class="mark">✗</text>
              <text class="name">{{ hit }}</text>
            </view>
          </view>
          <view v-else class="muted small" style="margin-top: 8px">
            确定性规则全部通过：没有膝痛禁忌动作、训练日不超过可用天数、忌口与链接白名单无冲突。
          </view>
          <view class="muted small" style="margin-top: 8px">
            判定全部由代码完成（动作目录元数据 + 规则表），模型不参与放行；红旗关键词只用于
            「先就医」这类宁可误伤的判定。
          </view>
        </template>
      </view>

      <view class="card">
        <view class="eyebrow">CONTEXT</view>
        <view v-if="selected.context" class="kv">
          <view class="kv-item">
            <text class="k">token</text>
            <text class="v">{{ selected.context.token_count }}/{{ selected.context.token_budget }}</text>
          </view>
          <view class="kv-item">
            <text class="k">保留段落</text>
            <text class="v">{{ selected.context.sections.length }}</text>
          </view>
          <view class="kv-item">
            <text class="k">丢弃</text>
            <text class="v" :class="{ warn: droppedCount > 0 }">{{ droppedCount }}</text>
          </view>
          <view class="kv-item">
            <text class="k">压缩</text>
            <text class="v">{{ selected.context.compressed ? selected.context.compression_method || '是' : '否' }}</text>
          </view>
        </view>
        <view v-else class="muted">没有上下文快照。</view>
      </view>

      <view class="card">
        <view class="eyebrow">COST</view>
        <view class="kv">
          <view class="kv-item">
            <text class="k">tokens in</text>
            <text class="v">{{ selected.tokens_in ?? 0 }}</text>
          </view>
          <view class="kv-item">
            <text class="k">tokens out</text>
            <text class="v">{{ selected.tokens_out ?? 0 }}</text>
          </view>
          <view class="kv-item">
            <text class="k">成本（元）</text>
            <text class="v">{{ Number(selected.cost_cny ?? 0).toFixed(4) }}</text>
          </view>
          <view class="kv-item">
            <text class="k">预算超限</text>
            <text class="v" :class="{ warn: !!(selected.output?.budget as any)?.exceeded }">
              {{ (selected.output?.budget as any)?.exceeded ? '是' : '否' }}
            </text>
          </view>
        </view>
        <view v-if="(selected.output?.budget as any)?.exceeded" class="notice" style="margin-top: 12px">
          {{ (selected.output?.budget as any).exceeded }}
        </view>
      </view>

      <view class="card">
        <view class="row">
          <view class="eyebrow">VERSIONS · 版本链</view>
          <view v-if="versions" class="chip">共 {{ versions.total }} 版 · 当前 v{{ versions.current_version }}</view>
        </view>
        <view v-if="!versions" class="muted" style="margin-top: 8px">
          这条运行没有关联计划，或计划还没有版本记录。
        </view>
        <view v-else class="version-list">
          <view v-for="row in versions.versions" :key="row.version" class="version-row">
            <view class="version-head">
              <text class="version-no">v{{ row.version }}</text>
              <text v-if="row.is_current" class="chip">当前</text>
              <text v-else class="muted small">{{ row.status === 'superseded' ? '已被取代' : row.status }}</text>
              <text class="muted small">来自 v{{ row.parent_version ?? '—' }}</text>
            </view>
            <text class="version-summary">{{ row.summary }}</text>
            <view class="version-meta">
              <text class="muted small">{{ versionChanges(row) }}</text>
              <text v-if="row.patch?.reason_text" class="muted small">
                · {{ row.patch.reason_text }}
              </text>
            </view>
          </view>
        </view>
      </view>

      <view class="card">
        <view class="row">
          <view class="eyebrow">AUDIT CHAIN</view>
          <view v-if="auditVerify" class="chip" :class="auditVerify.valid ? 'succeeded' : 'failed'">
            {{ auditVerify.valid ? '链完整' : `断裂 @ #${auditVerify.broken_at}` }}
          </view>
          <view v-else class="chip">{{ auditEvents.length }} 条事件</view>
        </view>

        <text class="audit-intro">
          这是「计划」上的变更证据链（谁在什么时候改了/批了/回滚了什么），
          跟上面的 RUNS 不是一回事：RUNS 是每次 Agent 执行，这里是落在计划上的动作。
        </text>

        <view v-if="auditEvents.length === 0" class="muted">这次运行没有关联的审计流。</view>

        <view v-for="ev in auditShown" :key="ev.seq" class="audit-row">
          <text class="audit-seq">#{{ ev.seq }}</text>
          <view class="audit-main">
            <view class="audit-head">
              <text class="audit-action">{{ label(ACTION_LABEL, ev.action) }}</text>
              <text class="muted small">{{ ev.actor_type }}/{{ shortId(ev.actor_id) }}</text>
            </view>
            <text class="muted small audit-hash">
              {{ ev.prev_hash.slice(0, 8) }} → {{ ev.hash.slice(0, 8) }}
            </text>
          </view>
        </view>

        <view v-if="auditHidden > 0" class="btn-row">
          <view class="btn-sm" @click="auditOpen = true">展开全部 {{ auditEvents.length }} 条</view>
        </view>
        <view v-else-if="auditOpen && auditEvents.length > 12" class="btn-row">
          <view class="btn-sm" @click="auditOpen = false">收起</view>
        </view>

        <button
          v-if="auditEvents.length > 0"
          class="btn secondary"
          style="margin-top: 12px"
          :disabled="auditBusy"
          @click="verifyAuditChain"
        >
          {{ auditBusy ? '校验中…' : '验证链完整性' }}
        </button>

        <view v-if="auditVerify" class="notice" style="margin-top: 12px">
          {{ auditVerify.valid
            ? `✓ ${auditVerify.count} 条事件逐条校验通过`
            : `✗ 在 #${auditVerify.broken_at} 断开：${auditVerify.reason}` }}
        </view>

        <view v-if="auditVerify && auditVerify.head_seq" class="checkpoint">
          <view class="checkpoint-line">
            <text class="muted small">链头</text>
            <text class="checkpoint-value">#{{ auditVerify.head_seq }} · {{ auditVerify.head_hash.slice(0, 16) }}…</text>
          </view>
          <view class="checkpoint-line">
            <text class="muted small">签名</text>
            <text class="checkpoint-value">{{ auditVerify.signature.slice(0, 20) }}…</text>
          </view>
          <view class="checkpoint-line">
            <text class="muted small">{{ auditVerify.signature_alg }}</text>
            <view class="btn-sm" @click="copyCheckpoint">复制校验点</view>
          </view>
          <text class="muted small">
            把这三个值抄到数据库外（工单/邮件/另一台机器）。链内校验只能发现"中间被改"；
            整链重写或尾部截断要靠这个签名对不对得上来发现。
          </text>
        </view>
      </view>

      <view class="card">
        <view class="row">
          <view class="eyebrow">VERIFICATION</view>
          <view
            class="chip"
            :class="selected.verification ? (selected.verification.passed ? 'succeeded' : 'failed') : ''"
          >
            {{ selected.verification ? (selected.verification.passed ? '通过' : '未通过') : '未验证' }}
          </view>
        </view>
        <view v-if="selected.verification" class="checks">
          <view
            v-for="check in selected.verification.checks"
            :key="check.name"
            class="check"
            :class="{ bad: !check.passed }"
          >
            <text class="mark">{{ check.passed ? '✓' : '✗' }}</text>
            <text class="name">{{ check.name }}</text>
            <text class="muted small">{{ check.detail }}</text>
          </view>
        </view>
        <view v-else class="muted">
          这次运行没有走到验证环节。只有「补丁审批」会做独立验证（核对补丁真的提交成新版本、
          动作确实变了、目标没漂）；今日规划、回滚、生成计划都不验证。
        </view>
        <view v-if="selected.verification && selected.verification.passed" class="muted small" style="margin-top: 8px">
          验证器不信任审批流程：它直接查库核对提交结果，失败会自动回滚。
        </view>
        <view v-if="failedChecks.length" class="notice" style="margin-top: 12px">
          验证失败会触发自动回滚，计划已退回提交前的版本。
        </view>
      </view>
    </template>
      </view>
    </view>

    <button class="btn secondary" @click="load">刷新</button>
  </view>
</template>

<style>
.console-grid {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.console-col {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

/* 桌面端：左边执行/评测列表，右边详情；手机宽度仍然单列 */
@media (min-width: 900px) {
  .console-grid {
    align-items: start;
    display: grid;
    gap: 20px;
    grid-template-columns: 360px minmax(0, 1fr);
  }
}

.audit-intro {
  color: var(--muted);
  display: block;
  font-size: 11px;
  line-height: 1.55;
  margin: 8px 0 4px;
}

.checkpoint {
  border-top: 1px dashed var(--line-soft);
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 12px;
  padding-top: 10px;
}

.checkpoint-line {
  align-items: center;
  display: flex;
  gap: 8px;
  justify-content: space-between;
}

.checkpoint-value {
  color: var(--ink-soft);
  font-family: ui-monospace, Menlo, monospace;
  font-size: 11px;
}

.run-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.run-kind {
  color: var(--ink);
  font-size: 13px;
  font-weight: 600;
}

.version-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 10px;
}

.version-row {
  border-top: 1px dashed var(--line-soft);
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding-top: 8px;
}

.version-row:first-child {
  border-top: none;
}

.version-head {
  align-items: center;
  display: flex;
  gap: 8px;
}

.version-no {
  color: var(--primary-deep);
  font-size: 13px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.version-summary {
  color: var(--ink-soft);
  font-size: 12px;
  line-height: 1.45;
}

.version-meta {
  display: flex;
  gap: 4px;
}

.safety-scope {
  color: var(--ink-soft);
  font-size: 12px;
  line-height: 1.6;
  margin-top: 8px;
}

.diff-sentence {
  color: var(--ink-soft);
  font-size: 12px;
  line-height: 1.6;
  margin-top: 10px;
}

.ops {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 10px;
}

.op-row {
  align-items: flex-start;
  border-top: 1px dashed var(--line-soft);
  display: flex;
  gap: 8px;
  padding-top: 8px;
}

.op-kind {
  background: var(--primary-soft);
  border-radius: 999px;
  color: var(--primary-deep);
  flex: 0 0 auto;
  font-size: 11px;
  font-weight: 700;
  padding: 2px 8px;
}

.op-kind.remove {
  background: #fde8e8;
  color: #b91c1c;
}

.op-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.op-text {
  color: var(--ink);
  font-size: 13px;
  line-height: 1.4;
}

.matrix {
  margin-top: 12px;
}
.matrix-row {
  border-bottom: 1px solid #edf1f1;
  display: grid;
  font-size: 12px;
  gap: 6px;
  grid-template-columns: 1.4fr 1fr 1fr 1fr;
  padding: 9px 0;
}
.matrix-row.header {
  color: #8a7466;
  font-weight: 600;
}
.matrix-row text:not(:first-child) {
  text-align: right;
}
.matrix-row .warn {
  color: #b3261e;
  font-weight: 600;
}
.compare-note {
  color: #c2410c;
  font-weight: 600;
  margin-right: 6px;
}
.audit-row {
  align-items: flex-start;
  border-bottom: 1px solid #edf1f1;
  display: flex;
  gap: 10px;
  padding: 8px 0;
}
.audit-seq {
  color: #c2410c;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  width: 32px;
}
.audit-main {
  display: flex;
  flex-direction: column;
  flex: 1;
  gap: 2px;
}
.audit-head {
  align-items: baseline;
  display: flex;
  gap: 8px;
}
.audit-action {
  font-size: 13px;
  font-weight: 600;
}
.audit-hash {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.scenario-grid {
  display: grid;
  gap: 8px;
  grid-template-columns: repeat(2, 1fr);
  margin-top: 12px;
}
.scenario {
  background: #f5f8f7;
  border-radius: 10px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px;
}
.scenario.running {
  background: #e7f4ee;
}
.scenario-id {
  color: #c2410c;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  font-weight: 700;
}
.scenario-title {
  font-size: 12px;
}
.replay-result {
  border-top: 1px solid #edf1f1;
  margin-top: 12px;
  padding-top: 12px;
}
.replay-title {
  font-weight: 600;
}
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}
.filter {
  background: #f1f4f5;
  border-radius: 999px;
  color: #53646d;
  font-size: 12px;
  padding: 6px 12px;
}
.filter.active {
  background: #c2410c;
  color: #ffffff;
}
.run-item {
  align-items: center;
  border-bottom: 1px solid #edf1f1;
  display: flex;
  gap: 10px;
  justify-content: space-between;
  padding: 10px 0;
}
.run-item.active {
  background: #f2f8f5;
  border-radius: 8px;
  padding-left: 8px;
  padding-right: 8px;
}
.run-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
}
.run-meta {
  display: flex;
  flex-direction: column;
  flex: 1;
  gap: 2px;
  padding-left: 6px;
}
.status {
  font-size: 12px;
  font-weight: 600;
}
.status.succeeded {
  color: #c2410c;
}
.status.failed {
  color: #b3261e;
}
.status.rejected {
  color: #8a6d3b;
}
.status.running {
  color: #2a5db0;
}
.chip.succeeded {
  background: #e7f4ee;
  color: #c2410c;
}
.chip.failed {
  background: #fdeceb;
  color: #b3261e;
}
.timeline {
  margin-top: 10px;
}
.step {
  display: flex;
  gap: 10px;
  padding: 6px 0;
}
.dot {
  background: #c2410c;
  border-radius: 50%;
  flex: none;
  height: 8px;
  margin-top: 6px;
  width: 8px;
}
.step-main {
  flex: 1;
}
.step-head {
  display: flex;
  gap: 8px;
  align-items: baseline;
}
.node {
  font-size: 13px;
  font-weight: 600;
}
.kv {
  display: grid;
  gap: 10px;
  grid-template-columns: 1fr 1fr;
  margin-top: 10px;
}
.kv-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.k {
  color: #8a7466;
  font-size: 12px;
}
.v {
  font-size: 16px;
  font-weight: 600;
}
.v.warn {
  color: #b3261e;
}
.checks {
  margin-top: 10px;
}
.check {
  align-items: baseline;
  border-bottom: 1px dashed #edf1f1;
  display: flex;
  gap: 8px;
  padding: 6px 0;
}
.check.bad {
  color: #b3261e;
}
.mark {
  font-weight: 700;
}
.name {
  font-size: 13px;
  min-width: 190px;
}
.small {
  font-size: 12px;
}
</style>
