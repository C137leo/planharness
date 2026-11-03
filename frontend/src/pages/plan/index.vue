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
import { actionName, slotLabel } from '../../styles/copy'
import { WEEKDAY_LABELS } from '../../styles/dates'

const plan = ref<CurrentPlan | null>(null)
const pending = ref<PlanPatchSummary[]>([])
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const toast = ref('')
const composerOpen = ref(false)

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

function openApproval() {
  uni.navigateTo({ url: '/pages/approval/index' })
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

      <view class="card">
        <view class="row">
          <view>
            <view class="section-title">当前版本</view>
            <view class="version">v{{ plan.version }}</view>
          </view>
          <view class="chip">{{ plan.actions.length }} 个槽位</view>
        </view>
        <view class="subtitle" style="margin-top: 10px">{{ plan.summary }}</view>
      </view>

      <view v-if="pending.length" class="card">
        <view class="section-title">还有 {{ pending.length }} 个待确认的改动</view>
        <view v-for="patch in pending" :key="patch.id" class="pending">
          <text class="pending-text">{{ patch.reason_text || patch.reason_code }}</text>
          <text class="pending-meta">{{ patch.operations.length }} 处改动 · 待确认</text>
        </view>
        <button class="btn secondary" style="margin-top: 12px" @click="openApproval">
          去逐条确认
        </button>
      </view>

      <view v-for="group in grouped" :key="group.day" class="card">
        <view class="row">
          <view class="day-title">{{ WEEKDAY_LABELS[group.day - 1] || `Day ${group.day}` }}</view>
          <view class="chip">{{ group.actions.length }}</view>
        </view>
        <view v-for="action in group.actions" :key="action.id" class="line">
          <text class="line-name">{{ actionName(action.action_ref) }}</text>
          <text class="line-meta">
            {{ slotLabel(action.slot) }}
            <template v-if="action.sets"> · {{ action.sets }}×{{ action.reps }}</template>
            <template v-if="action.duration_min"> · {{ action.duration_min }} 分钟</template>
          </text>
        </view>
      </view>

      <button class="btn" @click="composerOpen = true">改计划</button>
      <button class="btn secondary" :disabled="busy" @click="undo">
        {{ busy ? '撤销中…' : '撤销上一次改动' }}
      </button>
      <view class="muted" style="margin-top: 10px; text-align: center">
        撤销会新建一个版本，历史版本不会被删除。
      </view>

      <!-- #ifdef H5 -->
      <view class="console-link" @click="openConsole">打开 Harness Console →</view>
      <!-- #endif -->
    </template>

    <PatchComposer
      :visible="composerOpen"
      :plan-id="plan?.plan_id || ''"
      @close="composerOpen = false"
      @applied="onApplied"
    />
  </view>
</template>

<style>
.version {
  font-size: 26px;
  font-weight: 700;
  margin-top: 2px;
}
.pending {
  border-top: 1px solid #f0ece4;
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
  color: var(--primary);
  font-size: 13px;
  font-weight: 700;
}
.line {
  align-items: center;
  border-top: 1px solid #f5f1e9;
  display: flex;
  justify-content: space-between;
  padding: 10px 0;
}
.line-name {
  font-size: 15px;
  font-weight: 600;
}
.line-meta {
  color: var(--muted);
  font-size: 12px;
}
.console-link {
  color: var(--primary);
  font-size: 13px;
  margin-top: 22px;
  text-align: center;
  text-decoration: underline;
}
</style>
