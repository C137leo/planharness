<script setup lang="ts">
/**
 * 今天：今天练什么、吃什么、还能吃多少。
 *
 * 三个入口固定在这里：
 *   - 改计划（底部抽屉，用户自己说需求）
 *   - 换/生成计划（整份重排，走模型 + skill）
 *   - 打卡（记录实际做了什么）
 */
import { computed, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, ApiError, type CurrentPlan, type PatchPreset } from '../../api'
import PatchComposer from '../../components/PatchComposer.vue'
import PlanDraftSheet from '../../components/PlanDraftSheet.vue'
import { QUICK_ACTIONS, actionLabel, slotLabel } from '../../styles/copy'
import { WEEKDAY_LABELS, isoWeekday } from '../../styles/dates'

const plan = ref<CurrentPlan | null>(null)
const presets = ref<PatchPreset[]>([])

/** 接口拿不到预设时退回内置列表，页面不会空一块。 */
const presetCards = computed(() => {
  if (presets.value.length) {
    return presets.value.map((item) => ({
      key: item.key,
      label: item.label,
      example: item.example,
      hint: item.hint,
    }))
  }
  return QUICK_ACTIONS.map((item) => ({
    key: item.key,
    label: item.label,
    example: item.text,
    hint: item.hint || '',
  }))
})

/** 动作主肌群：meta 是后端给的附加信息，模板里不写 TS 断言。 */
function muscleOf(action: { meta?: Record<string, unknown> }): string {
  const value = (action.meta || {}).primary_muscle
  return typeof value === 'string' ? value : ''
}
const loading = ref(true)
const error = ref('')
const hasPlan = ref(true)

const composerOpen = ref(false)
const presetText = ref('')
const presetQuick = ref('')
const draftOpen = ref(false)

const weekday = isoWeekday()
const today = computed(() => plan.value?.today || null)
const training = computed(() =>
  (today.value?.actions || []).filter((action) => action.action_type === 'exercise'),
)
const meals = computed(() => today.value?.meals || [])
const macro = computed(() => {
  const macros = plan.value?.nutrition?.macros || {}
  const protein = macros.protein_g || {}
  return {
    kcal: plan.value?.nutrition?.target_kcal || plan.value?.calorie_target || 0,
    protein: protein.high_g || protein.low_g || plan.value?.protein_target_g || 0,
  }
})
const eatenKcal = computed(() => meals.value.reduce((sum, meal) => sum + meal.kcal, 0))

/** 今天没安排时，指出下一次是哪天、练什么——别让页面看起来像空的。 */
const nextSession = computed(() => {
  const days: Record<number, typeof training.value> = {}
  for (const action of plan.value?.actions || []) {
    if (action.action_type !== 'exercise') continue
    ;(days[action.day_index] ||= []).push(action)
  }
  const indexes = Object.keys(days).map(Number).sort((a, b) => a - b)
  if (!indexes.length) return null
  const next = indexes.find((index) => index > weekday) ?? indexes[0]
  return { weekday: next, actions: days[next], isNextWeek: next <= weekday }
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    plan.value = await api.getCurrentPlan()
    hasPlan.value = true
  } catch (err) {
    if (err instanceof ApiError && err.statusCode === 404) {
      hasPlan.value = false
      plan.value = null
    } else {
      error.value = err instanceof Error ? err.message : '加载失败'
    }
  } finally {
    loading.value = false
  }
}

async function loadPresets() {
  try {
    presets.value = await api.listPatchPresets()
  } catch {
    // 预设拿不到就用组件内置的兜底列表
  }
}

function openComposer(text = '', quick = '') {
  presetText.value = text
  presetQuick.value = quick
  composerOpen.value = true
}

async function onPatchApplied() {
  composerOpen.value = false
  await load()
}

async function onPlanConfirmed() {
  draftOpen.value = false
  await load()
}

function openProfile() {
  uni.switchTab({ url: '/pages/profile/index' })
}

function openCheckin() {
  uni.navigateTo({ url: '/pages/checkin/index' })
}

onShow(() => {
  load()
  loadPresets()
})
</script>

<template>
  <view class="page">
    <view class="eyebrow">PLANHARNESS · {{ WEEKDAY_LABELS[weekday - 1] }}</view>
    <view class="title">{{ hasPlan ? '今天这样练，这样吃。' : '先告诉我你是谁。' }}</view>
    <view class="subtitle">
      {{ hasPlan ? '计划可以随时改，改了才叫计划。' : '填完基础信息和目标，我让教练 skill 生成计划给你确认。' }}
    </view>

    <view v-if="loading" class="card muted">正在加载…</view>
    <view v-else-if="error" class="error">{{ error }}</view>

    <!-- 还没有计划：先录画像 -->
    <template v-else-if="!hasPlan">
      <view class="card">
        <view class="section-title">开始之前</view>
        <view class="list">
          <view class="list-row"><text class="list-title">基础信息</text><text class="list-meta">性别 / 身高 / 体重 / 年龄</text></view>
          <view class="list-row"><text class="list-title">目标</text><text class="list-meta">减脂 / 增肌 / 心肺 / 综合健康</text></view>
          <view class="list-row"><text class="list-title">饮食禁忌</text><text class="list-meta">过敏原会进安全门与食谱筛选</text></view>
        </view>
        <view class="btn-row">
          <view class="btn-sm primary" @click="openProfile">去填写</view>
        </view>
      </view>
    </template>

    <template v-else-if="plan">
      <!-- 今日训练 -->
      <view class="card">
        <view class="row">
          <view class="section-title">今天练</view>
          <view class="chip">v{{ plan.version }}</view>
        </view>
        <view v-if="!training.length" class="muted" style="margin-top: 10px">
          今天没有安排训练，休息也是计划的一部分。
        </view>
        <view v-if="!training.length && nextSession" class="next">
          <view class="section-title">
            {{ nextSession.isNextWeek ? '下周' : '下一次' }}：{{ WEEKDAY_LABELS[nextSession.weekday - 1] }}
          </view>
          <view class="list">
            <view v-for="action in nextSession.actions" :key="action.id" class="list-row">
              <text class="list-title">{{ actionLabel(action) }}</text>
              <text class="list-meta">{{ slotLabel(action.slot) }}</text>
            </view>
          </view>
        </view>
        <view v-else class="list">
          <view v-for="action in training" :key="action.id" class="list-row">
            <view class="list-main">
              <text class="list-title">{{ actionLabel(action) }}</text>
              <text class="list-meta">
                {{ slotLabel(action.slot) }}
                <template v-if="action.sets && action.reps"> · {{ action.sets }}×{{ action.reps }}</template>
                <template v-if="action.duration_min"> · {{ action.duration_min }} 分钟</template>
              </text>
            </view>
            <text class="list-value">{{ muscleOf(action) }}</text>
          </view>
        </view>
        <view class="btn-row">
          <view class="btn-sm" @click="openComposer('今天想休息', 'rest')">今天想休息</view>
          <view class="btn-sm" @click="openComposer('这周膝盖不舒服，换不伤膝的动作', 'injury_swap')">受伤换动作</view>
          <view class="btn-sm" @click="openComposer('想加点强度', 'increase')">加强度</view>
        </view>
      </view>

      <!-- 今日食谱 -->
      <view class="card">
        <view class="row">
          <view class="section-title">今天吃</view>
          <view class="list-meta">{{ eatenKcal }} / {{ macro.kcal }} kcal</view>
        </view>
        <view v-if="!meals.length" class="muted" style="margin-top: 10px">
          这一版没有餐食（旧模板计划），换一份教练计划就会有。
        </view>
        <view v-for="meal in meals" :key="meal.slot" class="meal">
          <view class="row">
            <text class="list-title">{{ meal.title }}</text>
            <text class="list-value">{{ meal.kcal }} kcal · 蛋白 {{ meal.protein_g }}g</text>
          </view>
          <text class="list-meta">
            {{ meal.items.map((item) => `${item.name} ${item.grams}g`).join(' · ') }}
          </text>
        </view>
        <view class="stat-row">
          <view class="stat">
            <text class="stat-value">{{ macro.kcal }}</text>
            <text class="stat-label">推荐摄入 kcal</text>
          </view>
          <view class="stat">
            <text class="stat-value">{{ Math.round(macro.protein) }}g</text>
            <text class="stat-label">蛋白目标</text>
          </view>
        </view>
        <view class="muted" style="margin-top: 10px">
          目标来自教练 skill 的估算（Mifflin-St Jeor + 活动系数），不是医疗建议。
        </view>
      </view>

      <!-- 用户自定义改动 -->
      <view class="card">
        <view class="row">
          <view class="section-title">改计划</view>
        </view>
        <view class="muted" style="margin-top: 6px">
          点一个方向，或者自己说一句。模型理解你的意思，安全门决定能不能执行。
        </view>
        <view class="preset-grid">
          <view
            v-for="preset in presetCards"
            :key="preset.key"
            class="preset"
            @click="openComposer(preset.example, preset.key)"
          >
            <text class="preset-label">{{ preset.label }}</text>
            <text class="preset-hint">{{ preset.hint }}</text>
          </view>
        </view>
        <view class="btn-row">
          <view class="btn-sm primary" @click="openComposer()">自己说一句</view>
          <view class="btn-sm" @click="draftOpen = true">换/生成整份计划</view>
        </view>
      </view>

      <view class="btn-row">
        <view class="btn-sm" @click="openCheckin">去打卡 / 记训练</view>
      </view>
    </template>

    <PatchComposer
      :visible="composerOpen"
      :plan-id="plan?.plan_id || ''"
      :preset-text="presetText"
      :preset-quick="presetQuick"
      @close="composerOpen = false"
      @applied="onPatchApplied"
    />
    <PlanDraftSheet
      :visible="draftOpen"
      :has-plan="hasPlan"
      @close="draftOpen = false"
      @confirmed="onPlanConfirmed"
    />
  </view>
</template>

<style>
.next {
  background: #faf9f6;
  border-radius: 14px;
  margin-top: 12px;
  padding: 12px 14px;
}
.meal {
  border-top: 1px solid #f1ede4;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 0;
}
.meal:first-of-type {
  border-top: none;
}
.preset-grid {
  display: grid;
  gap: 8px;
  grid-template-columns: 1fr 1fr;
  margin-top: 12px;
}
.preset {
  background: #faf9f6;
  border: 1px solid var(--line);
  border-radius: 14px;
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 11px 12px;
}
.preset-label {
  font-size: 14px;
  font-weight: 600;
}
.preset-hint {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.4;
}
</style>
