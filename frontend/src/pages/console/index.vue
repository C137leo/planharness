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
  type RunDetail,
  type RunSummary,
} from '../../api'

const runs = ref<RunSummary[]>([])
const selected = ref<RunDetail | null>(null)
const error = ref('')
const loading = ref(false)
const statusFilter = ref('')
const limit = ref(20)
const auditEvents = ref<AuditEvent[]>([])
const auditVerify = ref<AuditVerify | null>(null)
const auditBusy = ref(false)
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
  if (!planId) return
  try {
    auditEvents.value = await api.getAudit(planId)
  } catch {
    auditEvents.value = []
  }
}

async function verifyAuditChain() {
  const planId = selected.value?.plan_id
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

async function select(id: string) {
  try {
    selected.value = await api.getRun(id)
    await loadAudit(selected.value.plan_id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '读取 run 失败'
  }
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
    runs.value = await api.listRuns(limit.value, statusFilter.value)
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
        <view class="eyebrow">RUNS</view>
        <view class="chip">{{ loading ? '加载中' : `${runs.length} 条` }}</view>
      </view>

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
        <view class="run-id">{{ shortId(run.id) }}</view>
        <view class="run-meta">
          <text class="status" :class="run.status">{{ label(STATUS_LABEL, run.status) }}</text>
          <text class="muted small">{{ label(STOP_LABEL, run.stop_reason) }}</text>
        </view>
        <view class="muted small">{{ clock(run.created_at) }}</view>
      </view>

      <button v-if="runs.length > 0 && runs.length >= limit" class="btn secondary" @click="loadMore">
        加载更多
      </button>
    </view>

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
              <view v-if="step.detail" class="muted small">
                <text>{{ JSON.stringify(step.detail) }}</text>
              </view>
            </view>
          </view>
          <view v-if="selected.trace.length === 0" class="muted">没有 trace 记录。</view>
        </view>
      </view>

      <view class="card">
        <view class="eyebrow">DIFF</view>
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
        <view v-else class="muted">这次运行没有关联补丁。</view>
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
            <text class="v">{{ selected.output?.tokens_in ?? 0 }}</text>
          </view>
          <view class="kv-item">
            <text class="k">tokens out</text>
            <text class="v">{{ selected.output?.tokens_out ?? 0 }}</text>
          </view>
          <view class="kv-item">
            <text class="k">成本（元）</text>
            <text class="v">{{ Number(selected.output?.cost_cny ?? 0).toFixed(4) }}</text>
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
          <view class="eyebrow">AUDIT CHAIN</view>
          <view v-if="auditVerify" class="chip" :class="auditVerify.valid ? 'succeeded' : 'failed'">
            {{ auditVerify.valid ? '链完整' : `断裂 @ #${auditVerify.broken_at}` }}
          </view>
          <view v-else class="chip">{{ auditEvents.length }} 条事件</view>
        </view>

        <view v-if="auditEvents.length === 0" class="muted">这次运行没有关联的审计流。</view>

        <view v-for="ev in auditEvents" :key="ev.seq" class="audit-row">
          <text class="audit-seq">#{{ ev.seq }}</text>
          <view class="audit-main">
            <view class="audit-head">
              <text class="audit-action">{{ ev.action }}</text>
              <text class="muted small">{{ ev.actor_type }}/{{ shortId(ev.actor_id) }}</text>
            </view>
            <text class="muted small audit-hash">
              {{ ev.prev_hash.slice(0, 8) }} → {{ ev.hash.slice(0, 8) }}
            </text>
          </view>
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
      </view>

      <view class="card">
        <view class="row">
          <view class="eyebrow">VERIFICATION</view>
          <view class="chip" :class="selected.verification?.passed ? 'succeeded' : 'failed'">
            {{ selected.verification?.passed ? '通过' : '未通过' }}
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
        <view v-else class="muted">这次运行没有走到验证环节。</view>
        <view v-if="failedChecks.length" class="notice" style="margin-top: 12px">
          验证失败会触发自动回滚，计划已退回提交前的版本。
        </view>
      </view>
    </template>

    <button class="btn secondary" @click="load">刷新</button>
  </view>
</template>

<style>
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
