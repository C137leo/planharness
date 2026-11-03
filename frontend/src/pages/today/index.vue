<script setup lang="ts">
/**
 * 今天：只看今天要做的事 + 一个「改计划」入口。
 *
 * 改计划走底部抽屉（PatchComposer），不再是一个独立 tab：
 * 「要不要同意」是当下这个动作的一部分，不该让用户再去找页面。
 */
import { computed, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, type CalorieStats, type CurrentPlan, type PlanAction } from '../../api'
import PatchComposer from '../../components/PatchComposer.vue'
import { actionName, slotLabel } from '../../styles/copy'
import { WEEKDAY_LABELS, isoWeekday, todayStr } from '../../styles/dates'

const plan = ref<CurrentPlan | null>(null)
const calories = ref<CalorieStats | null>(null)
const loading = ref(true)
const error = ref('')
const composerOpen = ref(false)
const presetText = ref('')
const presetQuick = ref('')

const weekday = isoWeekday()
const todaysActions = computed<PlanAction[]>(() =>
  (plan.value?.actions || []).filter((action) => action.day_index === weekday),
)
const todaysKcal = computed(() => {
  const today = calories.value?.days.find((day) => day.date === todayStr())
  return today?.kcal ?? 0
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    plan.value = await api.getCurrentPlan()
    calories.value = await api.getCalorieStats(7)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function openComposer(text = '', quick = '') {
  presetText.value = text
  presetQuick.value = quick
  composerOpen.value = true
}

async function onApplied() {
  composerOpen.value = false
  await load()
}

function openCheckin() {
  uni.navigateTo({ url: '/pages/checkin/index' })
}

onShow(load)
</script>

<template>
  <view class="page">
    <view class="eyebrow">PLANHARNESS · 今天</view>
    <view class="title">{{ WEEKDAY_LABELS[weekday - 1] }}，只做今天的事。</view>
    <view class="subtitle">计划随时能改，改了才叫计划；不能改的只是愿望。</view>

    <view v-if="loading" class="card muted">正在加载…</view>
    <view v-else-if="error" class="error">{{ error }}</view>

    <template v-else-if="plan">
      <view class="card">
        <view class="row">
          <view class="section-title">今天</view>
          <view class="chip">v{{ plan.version }}</view>
        </view>

        <view v-if="!todaysActions.length" class="muted" style="margin-top: 10px">
          今天没有安排训练。休息也是计划的一部分。
        </view>

        <view v-for="action in todaysActions" :key="action.id" class="task">
          <view class="task-main">
            <text class="task-name">{{ actionName(action.action_ref) }}</text>
            <text class="task-meta">
              {{ slotLabel(action.slot) }}
              <template v-if="action.sets"> · {{ action.sets }}×{{ action.reps }}</template>
              <template v-if="action.duration_min"> · {{ action.duration_min }} 分钟</template>
            </text>
          </view>
          <text class="task-kind">{{ action.action_type === 'meal' ? '餐' : '练' }}</text>
        </view>

        <view class="kcal-strip">
          <text class="kcal-value">{{ todaysKcal }}</text>
          <text class="kcal-label">今天已记录消耗 kcal</text>
        </view>
      </view>

      <view class="actions">
        <button class="btn" @click="openComposer()">改计划</button>
        <view class="quick-grid">
          <view class="quick" @click="openComposer('今天想休息', 'rest')">今天想休息</view>
          <view class="quick" @click="openComposer('膝盖疼，换成不伤膝的动作', 'injury_swap')">
            受伤了
          </view>
          <view class="quick" @click="openComposer('想加点强度', 'increase')">想加量</view>
          <view class="quick" @click="openComposer('晚餐想吃得清淡一点', 'diet')">吃得清淡</view>
        </view>
        <button class="btn secondary" @click="openCheckin">去打卡 / 记训练</button>
      </view>

      <view class="notice">
        演示口径：模型只负责提议，安全门（受伤部位 / 可用天数 / 过敏原）和你的确认说了算。
      </view>
    </template>

    <PatchComposer
      :visible="composerOpen"
      :plan-id="plan?.plan_id || ''"
      :preset-text="presetText"
      :preset-quick="presetQuick"
      @close="composerOpen = false"
      @applied="onApplied"
    />
  </view>
</template>

<style>
.task {
  align-items: center;
  border-bottom: 1px solid #f0ece4;
  display: flex;
  justify-content: space-between;
  padding: 12px 0;
}
.task-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.task-name {
  font-size: 16px;
  font-weight: 600;
}
.task-meta {
  color: var(--muted);
  font-size: 12px;
}
.task-kind {
  color: var(--muted);
  font-size: 12px;
}
.kcal-strip {
  align-items: baseline;
  background: var(--primary-soft);
  border-radius: 14px;
  display: flex;
  gap: 8px;
  margin-top: 14px;
  padding: 12px 14px;
}
.kcal-value {
  color: var(--primary);
  font-size: 24px;
  font-weight: 700;
}
.kcal-label {
  color: var(--primary);
  font-size: 12px;
}
.actions {
  margin-top: 16px;
}
.quick-grid {
  display: grid;
  gap: 8px;
  grid-template-columns: 1fr 1fr;
  margin-top: 10px;
}
.quick {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 14px;
  font-size: 14px;
  padding: 14px 12px;
  text-align: center;
}
</style>
