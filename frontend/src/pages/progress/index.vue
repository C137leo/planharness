<script setup lang="ts">
/**
 * 进展：真实训练数据 + 教练发现。
 *
 * 所有指标来自 WorkoutSession / WorkoutLog / Checkin；
 * 「教练发现」由后端基于这些事实确定性生成，点击后把相关数据带进统一 Agent。
 */
import { computed, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import {
  api,
  type CalorieStats,
  type CheckinRecord,
  type ProgressFinding,
  type ProgressInsights,
  type WorkoutLogItem,
} from '../../api'
import GlobalCoachWorkspace from '../../components/GlobalCoachWorkspace.vue'
import DesktopSidebar from '../../components/DesktopSidebar.vue'
import { syncDesktopChrome } from '../../composables/desktopChrome'
import { actionName } from '../../styles/copy'
import { todayStr } from '../../styles/dates'

const stats = ref<CalorieStats | null>(null)
const insights = ref<ProgressInsights | null>(null)
const logs = ref<WorkoutLogItem[]>([])
const checkins = ref<CheckinRecord[]>([])
const loading = ref(true)
const error = ref('')

const coachOpen = ref(false)
const coachText = ref('')
const coachAuto = ref(false)

const completion = computed(() => insights.value?.weekly.avg_completion_pct ?? 0)
const totalMinutes = computed(() => insights.value?.weekly.total_minutes ?? stats.value?.total_duration_min ?? 0)
const sessionCount = computed(() => insights.value?.weekly.sessions ?? stats.value?.sessions ?? 0)
const findings = computed(() => insights.value?.findings || [])
const maxDayKcal = computed(() =>
  Math.max(1, ...(stats.value?.days || []).map((day) => day.kcal)),
)
const weights = computed(() =>
  checkins.value
    .filter((item) => item.weight_kg !== null)
    .slice(0, 8)
    .reverse(),
)
const weightDelta = computed(() => {
  if (weights.value.length < 2) return null
  const first = Number(weights.value[0].weight_kg)
  const last = Number(weights.value[weights.value.length - 1].weight_kg)
  return Math.round((last - first) * 10) / 10
})
const maxTrend = computed(() =>
  Math.max(1, ...(insights.value?.trend || []).map((item) => item.completion_pct)),
)

function dayWidth(kcal: number): string {
  return `${Math.max(4, Math.round((kcal / maxDayKcal.value) * 100))}%`
}

function trendHeight(value: number): string {
  return `${Math.max(8, Math.round((value / maxTrend.value) * 100))}%`
}

function shortDate(date: string): string {
  return date.slice(5)
}

function findingToneClass(tone: string): string {
  if (tone === 'warn') return 'warn'
  if (tone === 'good') return 'good'
  return 'info'
}

function openCoach(text = '', auto = false) {
  coachText.value = text
  coachAuto.value = auto
  coachOpen.value = true
}

function askFinding(finding: ProgressFinding) {
  openCoach(finding.suggested_prompt, true)
}

const coachContext = computed(() => ({
  weeklyCompletion: completion.value,
  weeklyMinutes: totalMinutes.value,
  weeklySessions: sessionCount.value,
  recentFindings: findings.value.map((item) => item.title),
  recentCompletionTrend: (insights.value?.trend || []).map((item) => item.completion_pct),
  avgDifficulty: insights.value?.weekly.avg_difficulty ?? '',
  avgFatigue: insights.value?.weekly.avg_fatigue ?? '',
}))

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [calorieData, logData, checkinData, insightData] = await Promise.all([
      api.getCalorieStats(7),
      api.listWorkoutLogs(14),
      api.listCheckins(),
      api.getProgressInsights(7),
    ])
    stats.value = calorieData
    logs.value = logData.items
    checkins.value = checkinData
    insights.value = insightData
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function openCheckin() {
  uni.navigateTo({ url: '/pages/checkin/index' })
}

function onAgentChanged() {
  void load()
}

function onOpenWorkout(payload: { sessionId?: string; date?: string }) {
  const date = payload.date || todayStr()
  const suffix = payload.sessionId ? `&session_id=${payload.sessionId}` : ''
  uni.navigateTo({ url: `/pages/workout/index?date=${date}${suffix}` })
}

function onOpenMemory() {
  uni.switchTab({ url: '/pages/profile/index' })
}

function onOpenTodayBrief() {
  uni.switchTab({ url: '/pages/today/index' })
}

function onOpenProgress() {
  // 已经在进展页
}

onShow(() => {
  syncDesktopChrome()
  load()
})
</script>

<template>
  <view class="page progress-page with-sidebar">
    <DesktopSidebar active="progress" />
    <view class="progress-top">
      <view>
        <view class="eyebrow">WORKOUT PLAN HARNESS · {{ todayStr() }} · 训练进展</view>
        <view class="title">练了多少，有没有在变。</view>
        <view class="subtitle">真实训练数据 + 教练发现。没有综合评价分，只回答发生了什么。</view>
      </view>
      <view class="progress-top-badge">
        <text class="progress-top-value">{{ insights?.weekly.gap_days ?? '—' }}</text>
        <text class="progress-top-label">天未训练</text>
      </view>
    </view>

    <view v-if="loading" class="card muted">正在加载…</view>
    <view v-else-if="error" class="error">{{ error }}</view>

    <template v-else>
      <view class="progress-metrics">
        <view class="metric-card">
          <text class="metric-label">本周完成率</text>
          <view class="metric-value-row">
            <text class="metric-value">{{ completion.toFixed(0) }}</text>
            <text class="metric-unit">%</text>
          </view>
          <text class="metric-sub">{{ sessionCount }} 次训练</text>
        </view>
        <view class="metric-card">
          <text class="metric-label">本周训练时长</text>
          <view class="metric-value-row">
            <text class="metric-value">{{ totalMinutes }}</text>
            <text class="metric-unit">分钟</text>
          </view>
          <text class="metric-sub">{{ insights?.weekly.avg_difficulty ?? '—' }}/5 平均难度</text>
        </view>
        <view class="metric-card">
          <text class="metric-label">近 7 天消耗</text>
          <view class="metric-value-row">
            <text class="metric-value">{{ stats?.total_kcal ?? 0 }}</text>
            <text class="metric-unit">kcal</text>
          </view>
          <text class="metric-sub">只算训练消耗</text>
        </view>
        <view class="metric-card">
          <text class="metric-label">体重变化</text>
          <view class="metric-value-row">
            <text class="metric-value">{{ weightDelta === null ? '—' : `${weightDelta > 0 ? '+' : ''}${weightDelta}` }}</text>
            <text class="metric-unit">kg</text>
          </view>
          <text class="metric-sub">基于最近打卡</text>
        </view>
      </view>

      <!-- 教练发现 -->
      <view class="card coach-findings">
        <view class="row">
          <view>
            <view class="section-title">教练发现</view>
            <view class="muted" style="margin-top: 4px">从真实训练记录中总结，不是通用建议。</view>
          </view>
          <text class="link" @click="openCoach('根据我最近的训练数据，帮我看看接下来该怎么调整', true)">
            问教练 →
          </text>
        </view>

        <view v-if="!findings.length" class="finding-empty">
          <text class="muted">最近没有明显的训练异常。继续保持，或让教练根据目标做一次主动评估。</text>
        </view>
        <view
          v-for="finding in findings"
          :key="finding.id"
          class="finding-row"
          :class="findingToneClass(finding.tone)"
        >
          <view class="finding-icon">{{ finding.tone === 'warn' ? '!' : finding.tone === 'good' ? '↗' : 'i' }}</view>
          <view class="finding-main">
            <text class="finding-title">{{ finding.title }}</text>
            <text class="finding-detail">{{ finding.detail }}</text>
            <text class="finding-evidence">{{ finding.evidence }}</text>
          </view>
          <view class="finding-action" @click="askFinding(finding)">问教练</view>
        </view>
      </view>

      <view class="progress-grid">
        <view class="card">
          <view class="row">
            <view class="section-title">完成率趋势</view>
            <view class="chip">{{ insights?.trend?.length || 0 }} 个训练日</view>
          </view>
          <view class="trend-chart">
            <view v-for="item in insights?.trend || []" :key="item.date" class="trend-col">
              <view class="trend-bar-wrap">
                <view class="trend-bar" :style="{ height: trendHeight(item.completion_pct) }" />
              </view>
              <text class="trend-value">{{ item.completion_pct.toFixed(0) }}%</text>
              <text class="trend-label">{{ shortDate(item.date) }}</text>
            </view>
            <view v-if="!(insights?.trend || []).length" class="muted">
              还没有训练记录，完成一场后这里会出现趋势。
            </view>
          </view>
          <view class="formula">{{ stats?.formula }}</view>
          <view class="muted" style="margin-top: 6px">{{ stats?.note }}</view>
        </view>

        <view class="card">
          <view class="row">
            <view class="section-title">近 7 天按天消耗</view>
            <view class="chip">{{ stats?.sessions || 0 }} 次</view>
          </view>
          <view class="bars">
            <view v-for="day in stats?.days || []" :key="day.date" class="bar-row">
              <text class="bar-label">{{ shortDate(day.date) }}</text>
              <view class="bar-track">
                <view class="bar-fill" :style="{ width: dayWidth(day.kcal) }" />
              </view>
              <text class="bar-value">{{ day.kcal }}</text>
            </view>
            <view v-if="!(stats?.days || []).length" class="muted">
              还没有训练记录，打卡后这里会按天累加。
            </view>
          </view>
        </view>
      </view>

      <view class="progress-grid">
        <view class="card">
          <view class="row">
            <view class="section-title">最近训练记录</view>
            <text class="link" @click="openCheckin">去打卡 →</text>
          </view>
          <view v-if="!(insights?.recent_sessions || []).length && !logs.length" class="muted" style="margin-top: 10px">
            还没有记录。
          </view>
          <view v-for="session in insights?.recent_sessions || []" :key="session.session_id" class="session-row">
            <view class="session-main">
              <text class="session-date">{{ session.date }}</text>
              <text class="session-meta">
                完成 {{ session.completion_pct?.toFixed(0) }}% · {{ session.actual_minutes }} 分钟
                <text v-if="session.difficulty"> · 难度 {{ session.difficulty }}/5</text>
              </text>
              <text v-if="session.note" class="session-note">{{ session.note }}</text>
            </view>
            <text class="session-status" :class="{ good: session.completion_pct >= 100 }">
              {{ session.completion_pct >= 100 ? '已完成' : session.completion_pct >= 60 ? '部分完成' : '未完成' }}
            </text>
          </view>
          <view v-for="log in logs.slice(0, 6)" :key="log.log_id" class="log-row">
            <view class="log-main">
              <text class="log-name">{{ actionName(log.action_ref) }}</text>
              <text class="log-meta">{{ log.date }} · {{ log.duration_min }} 分钟 · {{ log.kcal }} kcal</text>
            </view>
          </view>
        </view>

        <view class="card">
          <view class="row">
            <view class="section-title">体重</view>
            <view v-if="weightDelta !== null" class="chip">{{ weightDelta > 0 ? '+' : '' }}{{ weightDelta }} kg</view>
          </view>
          <view v-if="!weights.length" class="muted" style="margin-top: 10px">
            打卡时填一次体重，这里就会画出曲线。
          </view>
          <view v-for="item in weights" :key="item.id" class="weight-row">
            <text class="weight-date">{{ item.date }}</text>
            <text class="weight-value">{{ item.weight_kg }} kg</text>
          </view>
          <view class="btn-row">
            <view class="btn-sm primary" @click="openCheckin">去打卡</view>
            <view class="btn-sm" @click="openCoach('根据我最近的训练和体重变化，帮我调整下一周计划', true)">
              让教练调整
            </view>
          </view>
        </view>
      </view>

      <view class="muted" style="margin-top: 12px; text-align: center">{{ insights?.note }}</view>
    </template>

    <GlobalCoachWorkspace
      :visible="coachOpen"
      page="progress"
      :date="todayStr()"
      :initial-text="coachText"
      :auto-send="coachAuto"
      :context="coachContext"
      @close="coachOpen = false"
      @changed="onAgentChanged"
      @open-workout="onOpenWorkout"
      @open-memory="onOpenMemory"
      @open-today-brief="onOpenTodayBrief"
      @open-progress="onOpenProgress"
    />
  </view>
</template>

<style>
.progress-page {
  max-width: none;
}
.progress-top {
  align-items: flex-start;
  display: flex;
  gap: 20px;
  justify-content: space-between;
}
.progress-top-badge {
  align-items: flex-end;
  background: var(--surface);
  border: 1px solid var(--line-soft);
  border-radius: 16px;
  display: flex;
  flex-direction: column;
  min-width: 96px;
  padding: 12px 16px;
}
.progress-top-value {
  color: var(--primary);
  font-size: 28px;
  font-weight: 800;
  line-height: 1;
}
.progress-top-label {
  color: var(--muted);
  font-size: 10px;
  margin-top: 4px;
}
.progress-metrics {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin-top: 16px;
}
.metric-card {
  background: var(--surface);
  border: 1px solid var(--line-soft);
  border-radius: 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px;
}
.metric-label {
  color: var(--muted);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.metric-value-row {
  align-items: baseline;
  display: flex;
  gap: 4px;
}
.metric-value {
  color: var(--ink);
  font-size: 30px;
  font-weight: 800;
  line-height: 1.1;
}
.metric-unit {
  color: var(--muted);
  font-size: 11px;
}
.metric-sub {
  color: var(--muted);
  font-size: 10px;
}
.coach-findings {
  border-color: var(--primary-soft);
}
.finding-row {
  align-items: flex-start;
  border-top: 1px solid var(--line-soft);
  display: flex;
  gap: 10px;
  margin-top: 10px;
  padding-top: 10px;
}
.finding-icon {
  align-items: center;
  background: var(--line-soft);
  border-radius: 10px;
  color: var(--muted);
  display: flex;
  flex: 0 0 auto;
  font-size: 13px;
  font-weight: 800;
  height: 30px;
  justify-content: center;
  width: 30px;
}
.finding-row.warn .finding-icon {
  background: #fff0e5;
  color: #a34b1f;
}
.finding-row.good .finding-icon {
  background: var(--ok-soft);
  color: var(--ok);
}
.finding-main {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.finding-title {
  font-size: 14px;
  font-weight: 700;
}
.finding-detail {
  color: var(--ink-soft);
  font-size: 12px;
  line-height: 1.6;
}
.finding-evidence {
  color: var(--muted);
  font-size: 10px;
}
.finding-action {
  background: var(--primary-soft);
  border-radius: 999px;
  color: var(--primary-deep);
  flex: 0 0 auto;
  font-size: 11px;
  font-weight: 700;
  padding: 7px 11px;
}
.finding-empty {
  background: var(--line-soft);
  border-radius: 12px;
  margin-top: 10px;
  padding: 10px 12px;
}
.progress-grid {
  align-items: start;
  display: grid;
  gap: 14px;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  margin-top: 14px;
}
.progress-grid > .card {
  margin-top: 0;
}
.trend-chart {
  align-items: flex-end;
  display: flex;
  gap: 10px;
  height: 180px;
  margin-top: 14px;
  overflow-x: auto;
}
.trend-col {
  align-items: center;
  display: flex;
  flex: 0 0 44px;
  flex-direction: column;
  gap: 4px;
  height: 100%;
  justify-content: flex-end;
}
.trend-bar-wrap {
  align-items: flex-end;
  display: flex;
  flex: 1;
  width: 100%;
}
.trend-bar {
  background: linear-gradient(180deg, #f08a5d, var(--primary));
  border-radius: 8px 8px 4px 4px;
  width: 100%;
}
.trend-value {
  color: var(--ink-soft);
  font-size: 10px;
  font-weight: 700;
}
.trend-label {
  color: var(--muted);
  font-size: 9px;
}
.bars {
  margin-top: 14px;
}
.bar-row {
  align-items: center;
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.bar-label {
  color: var(--muted);
  font-size: 11px;
  width: 40px;
}
.bar-track {
  background: var(--primary-soft);
  border-radius: 999px;
  flex: 1;
  height: 10px;
  overflow: hidden;
}
.bar-fill {
  background: linear-gradient(90deg, #f08a5d 0%, var(--primary) 100%);
  border-radius: 999px;
  height: 10px;
}
.bar-value {
  color: var(--ink-soft);
  font-size: 11px;
  text-align: right;
  width: 46px;
}
.formula {
  background: var(--line-soft);
  border-radius: 12px;
  color: var(--ink-soft);
  font-size: 12px;
  margin-top: 14px;
  padding: 10px 12px;
}
.session-row,
.weight-row,
.log-row {
  align-items: center;
  border-top: 1px solid var(--line-soft);
  display: flex;
  justify-content: space-between;
  padding: 10px 0;
}
.session-main,
.log-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.session-date,
.weight-date,
.log-meta {
  color: var(--muted);
  font-size: 12px;
}
.session-meta,
.log-name {
  font-size: 13px;
  font-weight: 600;
}
.session-note {
  color: var(--muted);
  font-size: 11px;
}
.session-status {
  border-radius: 999px;
  color: var(--muted);
  font-size: 11px;
  padding: 4px 8px;
}
.session-status.good {
  background: var(--ok-soft);
  color: var(--ok);
}
.weight-value,
.log-kcal {
  font-size: 14px;
  font-weight: 600;
}
@media (max-width: 900px) {
  .progress-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .progress-grid {
    grid-template-columns: 1fr;
  }
  .progress-top {
    display: block;
  }
  .progress-top-badge {
    align-items: flex-start;
    flex-direction: row;
    gap: 8px;
    margin-top: 10px;
    width: fit-content;
  }
}
</style>
