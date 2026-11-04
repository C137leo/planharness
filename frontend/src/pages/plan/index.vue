<script setup lang="ts">
/**
 * 计划：整周安排、待确认的改动、以及「撤销上一次改动」。
 *
 * 撤销不是删历史：它生成一个新版本，内容回到上一个版本。
 * 所以撤销本身也能被审计和再撤销。
 */
import { computed, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, type CurrentPlan, type PlanAction, type PlanPatchSummary } from '../../api'
import PatchComposer from '../../components/PatchComposer.vue'
import PlanDraftSheet from '../../components/PlanDraftSheet.vue'
import { actionLabel, slotLabel } from '../../styles/copy'
import { WEEKDAY_LABELS } from '../../styles/dates'

const plan = ref<CurrentPlan | null>(null)
const pending = ref<PlanPatchSummary[]>([])
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const toast = ref('')
const composerOpen = ref(false)
const draftOpen = ref(false)

const grouped = computed(() => {
  const groups: Record<number, PlanAction[]> = {}
  for (const action of plan.value?.actions || []) {
    ;(groups[action.day_index] ||= []).push(action)
  }
  return Object.entries(groups)
    .map(([day, actions]) => ({ day: Number(day), actions }))
    .sort((a, b) => a.day - b.day)
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    plan.value = await api.getCurrentPlan()
    pending.value = await api.listPatches(10)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function undo() {
  if (!plan.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const result = await api.undoPlan(plan.value.plan_id)
    toast.value = `已撤销，现在是 v${result.new_version}（内容回到上一版）`
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '撤销失败'
  } finally {
    busy.value = false
  }
}

async function onApplied() {
  composerOpen.value = false
  await load()
}

async function onConfirmed() {
  draftOpen.value = false
  await load()
}

const todayMeals = computed(() => {
  const day = plan.value?.today?.weekday || 1
  return (plan.value?.meals || []).filter((meal) => meal.day_index === day)
})

/** 待确认补丁的改动数：旧后端没有 operations 字段，不能直接取 .length。 */
function operationCount(patch: PlanPatchSummary): number {
  return patch.operations?.length ?? patch.diff?.changed_slots ?? 0
}

/** 槽位 emoji：训练/餐食一眼能区分，不用真实图片也能看出结构。 */
function slotEmoji(action: { action_type: string; slot: string }): string {
  if (action.action_type === 'meal') return '🍽️'
  if (action.slot === 'cardio') return '🏃'
  if (action.slot === 'mobility') return '🧘'
  if (action.slot === 'warmup') return '🔥'
  return '🏋️'
}

function openToday() {
  uni.switchTab({ url: '/pages/today/index' })
}

function openConsole() {
  uni.navigateTo({ url: '/pages/console/index' })
}

onShow(load)
</script>

<template>
  <view class="page">
    <view class="eyebrow">PLANHARNESS · 计划</view>
    <view class="title">这一周怎么练、怎么吃。</view>
    <view class="subtitle">任何一行都可以被一句话改掉，改完会留下版本记录。</view>

    <view v-if="loading" class="card muted">正在加载…</view>
    <view v-else-if="error" class="error">{{ error }}</view>

    <template v-else-if="plan">
      <view v-if="toast" class="notice">{{ toast }}</view>

      <view class="hero">
        <view class="row">
          <text class="badge">v{{ plan.version }}</text>
          <text class="badge">{{ plan.source === 'coach' ? '教练计划' : '模板计划' }}</text>
        </view>
        <view class="hero-title">{{ plan.nutrition?.target_kcal || plan.calorie_target }} kcal / 天</view>
        <view class="hero-sub">{{ plan.summary }}</view>
        <view class="stat-row">
          <view class="stat">
            <text class="stat-value">{{ Math.round(plan.protein_target_g) }}g</text>
            <text class="stat-label">蛋白目标</text>
          </view>
          <view class="stat">
            <text class="stat-value">{{ grouped.length }}</text>
            <text class="stat-label">训练天</text>
          </view>
          <view class="stat">
            <text class="stat-value">{{ plan.actions.length }}</text>
            <text class="stat-label">槽位</text>
          </view>
        </view>
      </view>

      <view v-if="pending.length" class="card">
        <view class="section-title">还有 {{ pending.length }} 个待确认的改动</view>
        <view v-for="patch in pending" :key="patch.id" class="pending">
          <text class="pending-text">{{ patch.reason_text || patch.reason_code }}</text>
          <text class="pending-meta">{{ operationCount(patch) }} 处改动 · 待确认</text>
        </view>
        <view class="btn-row">
          <view class="btn-sm primary" @click="openToday">去今天页修改计划</view>
        </view>
      </view>

      <view v-if="todayMeals.length" class="card">
        <view class="row">
          <view class="section-title">今天吃</view>
          <text class="link" @click="openToday">今天页 →</text>
        </view>
        <view v-for="meal in todayMeals" :key="meal.slot" class="line">
          <text class="line-name">{{ meal.title }}</text>
          <text class="line-meta">{{ meal.kcal }} kcal · 蛋白 {{ meal.protein_g }}g</text>
        </view>
      </view>

      <view v-for="group in grouped" :key="group.day" class="card">
        <view class="row">
          <view class="day-title">{{ WEEKDAY_LABELS[group.day - 1] || `Day ${group.day}` }}</view>
          <view class="chip">{{ group.actions.length }}</view>
        </view>
        <view v-for="action in group.actions" :key="action.id" class="list-row">
          <view class="list-body">
            <view class="thumb">{{ slotEmoji(action) }}</view>
            <view class="list-main">
              <text class="list-title">{{ actionLabel(action) }}</text>
              <text class="list-meta">
                {{ slotLabel(action.slot) }}
                <template v-if="action.sets"> · {{ action.sets }}×{{ action.reps }}</template>
                <template v-if="action.duration_min"> · {{ action.duration_min }} 分钟</template>
              </text>
            </view>
          </view>
        </view>
      </view>

      <view class="btn-row">
        <view class="btn-sm primary" @click="composerOpen = true">改计划</view>
        <view class="btn-sm" @click="undo">{{ busy ? '撤销中…' : '撤销上一次改动' }}</view>
        <view class="btn-sm" @click="draftOpen = true">换一份计划</view>
      </view>
      <view class="muted" style="margin-top: 10px; text-align: center">
        撤销会新建一个版本，历史版本不会被删除。
      </view>

      <!-- #ifdef H5 -->
      <view class="console-link" @click="openConsole">打开 Harness Console →</view>
      <!-- #endif -->
    </template>

    <PlanDraftSheet
      :visible="draftOpen"
      :has-plan="true"
      @close="draftOpen = false"
      @confirmed="onConfirmed"
    />

    <PatchComposer
      :visible="composerOpen"
      :plan-id="plan?.plan_id || ''"
      @close="composerOpen = false"
      @applied="onApplied"
    />
  </view>
</template>

<style>
.pending {
  border-top: 1px solid var(--line-soft);
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 10px;
  padding-top: 10px;
}
.pending-text {
  font-size: 14px;
  font-weight: 600;
}
.pending-meta {
  color: var(--muted);
  font-size: 12px;
}
.day-title {
  color: var(--primary-deep);
  font-size: 14px;
  font-weight: 700;
}
.console-link {
  color: var(--primary);
  font-size: 13px;
  margin-top: 22px;
  text-align: center;
  text-decoration: underline;
}
</style>
