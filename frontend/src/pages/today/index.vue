<script setup lang="ts">
import { computed, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, type CurrentPlan, type PlanAction } from '../../api'

const plan = ref<CurrentPlan | null>(null)
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const createdPatchId = ref('')

const slotLabels: Record<string, string> = {
  warmup: '热身',
  strength: '力量',
  cardio: '有氧',
  mobility: '放松',
  meal_breakfast: '早餐',
  meal_lunch: '午餐',
  meal_dinner: '晚餐',
}

const actionNames: Record<string, string> = {
  dynamic_warmup: '动态热身',
  goblet_squat: '高脚杯深蹲',
  squat: '深蹲',
  bodyweight_squat: '自重深蹲',
  pushup: '俯卧撑',
  row: '哑铃划船',
  backpack_row: '背包划船',
  brisk_walk: '快走',
  easy_walk: '低冲击快走',
  glute_bridge: '臀桥',
  full_body_stretch: '全身拉伸',
  chicken_rice_bowl: '鸡胸糙米碗',
  oats_eggs: '燕麦鸡蛋',
  beef_salad: '牛肉沙拉',
  fish_veggies: '鱼排蔬菜',
  tofu_veggies: '豆腐时蔬',
  rice_veggies: '米饭时蔬',
}

const grouped = computed(() => {
  const groups: Record<number, PlanAction[]> = {}
  for (const action of plan.value?.actions || []) {
    ;(groups[action.day_index] ||= []).push(action)
  }
  return Object.entries(groups).map(([day, actions]) => ({ day: Number(day), actions }))
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    plan.value = await api.getCurrentPlan()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function makeKneePatch() {
  if (!plan.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const patch = await api.createKneePatch(plan.value.plan_id, 2)
    // 不再自动跳转：跳转后审批按钮通常就在刚才点击的位置，
    // 连点两下很容易直接点穿到「通过」并一路确认掉。
    createdPatchId.value = patch.id
    uni.showToast({ title: '补丁已生成', icon: 'success' })
  } catch (err) {
    error.value = err instanceof Error ? err.message : '生成补丁失败'
  } finally {
    busy.value = false
  }
}

function openCheckin() {
  uni.switchTab({ url: '/pages/checkin/index' })
}

function openConsole() {
  uni.navigateTo({ url: '/pages/console/index' })
}

onShow(load)
</script>

<template>
  <view class="page">
    <view class="eyebrow">PLANHARNESS · TODAY</view>
    <view class="title">今天，只改该改的。</view>
    <view class="subtitle">
      模型可以提议，但计划变更必须经过安全门和你的确认。
    </view>

    <view v-if="loading" class="card muted">正在加载当前计划…</view>
    <view v-else-if="error" class="error">{{ error }}</view>

    <template v-else-if="plan">
      <view class="card">
        <view class="row">
          <view>
            <view class="eyebrow">CURRENT PLAN</view>
            <view style="font-size: 22px; font-weight: 700; margin-top: 4px">
              v{{ plan.version }}
            </view>
          </view>
          <view class="chip">{{ plan.actions.length }} 个动作</view>
        </view>
        <view class="subtitle" style="margin-top: 10px">{{ plan.summary }}</view>
        <view class="metrics">
          <view class="metric">
            <text class="metric-value">{{ Math.round(plan.calorie_target) }}</text>
            <text class="metric-label">kcal</text>
          </view>
          <view class="metric">
            <text class="metric-value">{{ Math.round(plan.protein_target_g) }}</text>
            <text class="metric-label">protein g</text>
          </view>
          <view class="metric">
            <text class="metric-value">{{ plan.step_target }}</text>
            <text class="metric-label">steps</text>
          </view>
        </view>
      </view>

      <view class="card">
        <view class="row">
          <view class="eyebrow">7-DAY PLAN</view>
          <view class="chip">v{{ plan.version }}</view>
        </view>
        <view v-for="group in grouped" :key="group.day" class="day">
          <view class="day-title">Day {{ group.day }}</view>
          <view v-for="action in group.actions" :key="action.id" class="action">
            <view class="action-main">
              <text class="action-name">{{ actionNames[action.action_ref] || action.action_ref }}</text>
              <text class="action-meta">
                {{ slotLabels[action.slot] || action.slot }}
                <template v-if="action.sets"> · {{ action.sets }}×{{ action.reps }}</template>
                <template v-if="action.duration_min"> · {{ action.duration_min }} min</template>
              </text>
            </view>
            <text class="action-type">{{ action.action_type === 'meal' ? '餐食' : '训练' }}</text>
          </view>
        </view>
      </view>

      <view class="notice">
        演示动作：假设你右膝疼痛，本周只能练 2 天。系统应只改 3/12 个槽位，而不是重生成整份计划。
      </view>

      <button class="btn" :disabled="busy" @click="makeKneePatch">
        {{ busy ? '生成补丁…' : '生成膝痛补丁（3/12）' }}
      </button>
      <button
        v-if="createdPatchId"
        class="btn secondary"
        style="margin-top: 10px"
        @click="uni.switchTab({ url: '/pages/approval/index' })"
      >
        补丁已生成 → 去审批
      </button>
      <button class="btn secondary" @click="openCheckin">去打卡</button>

      <!-- #ifdef H5 -->
      <view class="console-link" @click="openConsole">打开 Harness Console →</view>
      <!-- #endif -->
    </template>
  </view>
</template>

<style>
.metrics {
  display: flex;
  gap: 10px;
  margin-top: 16px;
}
.metric {
  background: #f3f7f5;
  border-radius: 14px;
  display: flex;
  flex: 1;
  flex-direction: column;
  padding: 12px;
}
.metric-value {
  font-size: 20px;
  font-weight: 700;
}
.metric-label {
  color: #6b7c85;
  font-size: 11px;
  margin-top: 2px;
}
.day {
  border-top: 1px solid #edf1f1;
  margin-top: 14px;
  padding-top: 12px;
}
.day-title {
  color: #176b52;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.06em;
  margin-bottom: 6px;
}
.action {
  align-items: center;
  display: flex;
  justify-content: space-between;
  padding: 9px 0;
}
.action-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.action-name {
  font-size: 15px;
  font-weight: 600;
}
.action-meta {
  color: #6b7c85;
  font-size: 12px;
}
.action-type {
  color: #8a9a9a;
  font-size: 11px;
}
.console-link {
  color: #176b52;
  font-size: 13px;
  margin-top: 22px;
  text-align: center;
  text-decoration: underline;
}
</style>
