<script setup lang="ts">
/**
 * 进展：只显示两件能量化的事——训练消耗、打卡体重。
 *
 * 热量口径固定为「只算消耗」：MET × 3.5 × 体重 / 200 × 时长。
 * 不含基础代谢、不含饮食摄入，接口里也把这句话带回来，避免误读。
 */
import { computed, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, type CalorieStats, type CheckinRecord, type WorkoutLogItem } from '../../api'
import { actionName } from '../../styles/copy'
import { todayStr } from '../../styles/dates'

const stats = ref<CalorieStats | null>(null)
const logs = ref<WorkoutLogItem[]>([])
const checkins = ref<CheckinRecord[]>([])
const loading = ref(true)
const error = ref('')

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

function dayWidth(kcal: number): string {
  return `${Math.max(4, Math.round((kcal / maxDayKcal.value) * 100))}%`
}

function shortDate(date: string): string {
  return date.slice(5)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [calorieData, logData, checkinData] = await Promise.all([
      api.getCalorieStats(7),
      api.listWorkoutLogs(14),
      api.listCheckins(),
    ])
    stats.value = calorieData
    logs.value = logData.items
    checkins.value = checkinData
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function openCheckin() {
  uni.navigateTo({ url: '/pages/checkin/index' })
}

onShow(load)
</script>

<template>
  <view class="page">
    <view class="eyebrow">PLANHARNESS · 进展</view>
    <view class="title">练了多少，有没有在变。</view>
    <view class="subtitle">只统计训练消耗和打卡体重，不做花哨的综合评分。</view>

    <view v-if="loading" class="card muted">正在加载…</view>
    <view v-else-if="error" class="error">{{ error }}</view>

    <template v-else>
      <view class="card">
        <view class="row">
          <view class="section-title">近 7 天消耗</view>
          <view class="chip">{{ stats?.sessions || 0 }} 次训练</view>
        </view>
        <view class="big">
          <text class="big-value">{{ stats?.total_kcal ?? 0 }}</text>
          <text class="big-unit">kcal</text>
        </view>
        <view class="muted">
          共 {{ stats?.total_duration_min || 0 }} 分钟 · 体重口径
          {{ stats?.weight_kg || 0 }}kg（{{ stats?.weight_source === 'checkin' ? '最近打卡'
          : stats?.weight_source === 'profile' ? '画像' : '默认值' }}）
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

        <view class="formula">{{ stats?.formula }}</view>
        <view class="muted" style="margin-top: 6px">{{ stats?.note }}</view>
      </view>

      <view class="card">
        <view class="row">
          <view class="section-title">体重</view>
          <view v-if="weightDelta !== null" class="chip">
            {{ weightDelta > 0 ? '+' : '' }}{{ weightDelta }} kg
          </view>
        </view>
        <view v-if="!weights.length" class="muted" style="margin-top: 10px">
          打卡时填一次体重，这里就会画出曲线。
        </view>
        <view v-for="item in weights" :key="item.id" class="weight-row">
          <text class="weight-date">{{ item.date }}</text>
          <text class="weight-value">{{ item.weight_kg }} kg</text>
        </view>
      </view>

      <view class="card">
        <view class="section-title">最近的训练记录</view>
        <view v-if="!logs.length" class="muted" style="margin-top: 10px">
          还没有记录。
        </view>
        <view v-for="log in logs.slice(0, 10)" :key="log.log_id" class="log-row">
          <view class="log-main">
            <text class="log-name">{{ actionName(log.action_ref) }}</text>
            <text class="log-meta">
              {{ log.date }} · {{ log.duration_min }} 分钟 · MET {{ log.met }}
              <template v-if="log.met_fallback">（通用值）</template>
            </text>
          </view>
          <text class="log-kcal">{{ log.kcal }} kcal</text>
        </view>
      </view>

      <view class="btn-row">
        <view class="btn-sm primary" @click="openCheckin">去打卡</view>
      </view>
      <view class="muted" style="margin-top: 8px; text-align: center">
        记录日期 {{ todayStr() }}
      </view>
    </template>
  </view>
</template>

<style>
.big {
  align-items: baseline;
  display: flex;
  gap: 6px;
  margin: 10px 0 6px;
}
.big-value {
  color: var(--primary);
  font-size: 34px;
  font-weight: 700;
}
.big-unit {
  color: var(--primary);
  font-size: 14px;
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
  background: #f2efe7;
  border-radius: 999px;
  flex: 1;
  height: 10px;
  overflow: hidden;
}
.bar-fill {
  background: var(--primary);
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
  background: #f7f5ef;
  border-radius: 12px;
  color: var(--ink-soft);
  font-size: 12px;
  margin-top: 14px;
  padding: 10px 12px;
}
.weight-row,
.log-row {
  align-items: center;
  border-top: 1px solid #f5f1e9;
  display: flex;
  justify-content: space-between;
  padding: 10px 0;
}
.weight-date,
.log-meta {
  color: var(--muted);
  font-size: 12px;
}
.weight-value,
.log-kcal {
  font-size: 14px;
  font-weight: 600;
}
.log-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.log-name {
  font-size: 15px;
  font-weight: 600;
}
</style>
