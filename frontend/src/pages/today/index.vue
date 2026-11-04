<script setup lang="ts">
/**
 * 今天：今天练什么、吃什么、还能改哪里。
 *
 * 版式参考食谱类 App：顶部一张暖色主卡片（今天最重要的一句话），
 * 下面是白卡片列表，动作和餐食都带缩略图，改动入口是横向胶囊。
 */
import { computed, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, ApiError, type CurrentPlan, type PatchPreset } from '../../api'
import PatchComposer from '../../components/PatchComposer.vue'
import PlanDraftSheet from '../../components/PlanDraftSheet.vue'
import { QUICK_ACTIONS, actionLabel, slotLabel } from '../../styles/copy'
import { THUMBS, thumbOf } from '../../styles/thumbs'
import { WEEKDAY_LABELS, isoWeekday } from '../../styles/dates'

const plan = ref<CurrentPlan | null>(null)
const presets = ref<PatchPreset[]>([])
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
/** 所有动作都被换成了拉伸 = 今天是休息日：显示成一条，而不是三行拉伸。 */
const restMode = computed(
  () =>
    training.value.length > 0 &&
    training.value.every((action) =>
      ['stretch', 'full_body_stretch'].includes(action.action_ref),
    ),
)

const totalMinutes = computed(() =>
  training.value.reduce((sum, action) => sum + (action.duration_min || 0), 0),
)

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

/** 今天没安排时，指出下一次是哪天、练什么。 */
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

const MEAL_EMOJI: Record<string, string> = {
  breakfast: '🥣',
  lunch: '🍚',
  dinner: '🍲',
  snack: '🍎',
}
const MOVEMENT_EMOJI: Record<string, string> = {
  knee_dominant: '🦵',
  hip_dominant: '🍑',
  horizontal_push: '🤲',
  horizontal_pull: '💪',
  vertical_push: '🙌',
  vertical_pull: '🧗',
  core: '🧘',
  cardio: '🏃',
}

function mealEmoji(slot: string): string {
  return MEAL_EMOJI[slot] || '🍽️'
}

/** 动作主肌群：meta 是后端给的附加信息，模板里不写 TS 断言。 */
function muscleOf(action: { meta?: Record<string, unknown> }): string {
  const value = (action.meta || {}).primary_muscle
  return typeof value === 'string' ? value : ''
}

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
    // 预设拿不到就用内置兜底列表
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
          <view class="list-row">
            <view class="list-main">
              <text class="list-title">基础信息</text>
              <text class="list-meta">性别 / 身高 / 体重 / 年龄</text>
            </view>
          </view>
          <view class="list-row">
            <view class="list-main">
              <text class="list-title">目标</text>
              <text class="list-meta">减脂 / 增肌 / 心肺 / 综合健康</text>
            </view>
          </view>
          <view class="list-row">
            <view class="list-main">
              <text class="list-title">饮食禁忌</text>
              <text class="list-meta">过敏原会进安全门与食谱筛选</text>
            </view>
          </view>
        </view>
        <view class="btn-row">
          <view class="btn-sm primary" @click="openProfile">去填写</view>
        </view>
      </view>
    </template>

    <template v-else-if="plan">
      <!-- 主卡片：今天最重要的一句话 -->
      <view class="hero">
        <view class="row">
          <text class="badge">{{ WEEKDAY_LABELS[weekday - 1] }} · v{{ plan.version }}</text>
          <text v-if="training.length && !restMode" class="badge">约 {{ totalMinutes }} 分钟</text>
        </view>
        <view class="hero-title">
          {{ restMode || !training.length ? '今天休息。' : training.length + ' 个动作，练完就吃。' }}
        </view>
        <view class="hero-sub">
          {{ training.length
            ? '顺序按教练安排走；状态不对就点下面的胶囊改，改完你确认才生效。'
            : '休息也是计划的一部分，明天继续。' }}
        </view>
      </view>

      <!-- 今天练 -->
      <view class="card">
        <view class="row">
          <view class="section-title">今天练</view>
          <text v-if="training.length" class="link" @click="openCheckin">记录 →</text>
        </view>

        <view v-if="!training.length" class="muted" style="margin-top: 10px">
          今天没有安排训练。
        </view>
        <!-- 休息日：一条就够，别摆三行"拉伸" -->
        <view v-else-if="restMode" class="list">
          <view class="list-row">
            <view class="list-body">
              <view class="thumb">
                <image class="thumb-img" :src="THUMBS.stretch" mode="aspectFill" />
              </view>
              <view class="list-main">
                <text class="list-title">休息</text>
                <text class="list-meta">今天以恢复为主，散步或轻度拉伸即可</text>
              </view>
            </view>
          </view>
        </view>
        <view v-else class="list">
          <view v-for="action in training" :key="action.id" class="list-row">
            <view class="list-body">
              <view class="thumb">
                <image class="thumb-img" :src="thumbOf(action)" mode="aspectFill" />
              </view>
              <view class="list-main">
                <text class="list-title">{{ actionLabel(action) }}</text>
                <text class="list-meta">
                  {{ slotLabel(action.slot) }}
                  <template v-if="action.sets && action.reps"> · {{ action.sets }}×{{ action.reps }}</template>
                  <template v-if="action.duration_min"> · {{ action.duration_min }} 分钟</template>
                  <template v-if="muscleOf(action)"> · {{ muscleOf(action) }}</template>
                </text>
              </view>
            </view>
          </view>
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

        <view class="chip-scroll">
          <view class="pill" @click="openComposer('今天想休息', 'rest')">今天想休息</view>
          <view class="pill" @click="openComposer('这周膝盖不舒服，换不伤膝的动作', 'injury_swap')">受伤换动作</view>
          <view class="pill" @click="openComposer('想加点强度', 'increase')">加强度</view>
        </view>
      </view>

      <!-- 今天吃 -->
      <view class="card">
        <view class="row">
          <view class="section-title">今天吃</view>
          <text class="list-meta">{{ eatenKcal }} / {{ macro.kcal }} kcal</text>
        </view>
        <view v-if="!meals.length" class="muted" style="margin-top: 10px">
          这一版没有餐食记录（可能是早期模板或旧补丁留下的）。点「重新生成整份计划」可以补齐食谱。
        </view>
        <view v-for="meal in meals" :key="meal.slot" class="meal-row">
          <view class="thumb">{{ mealEmoji(meal.slot) }}</view>
          <view class="list-main" style="flex: 1">
            <view class="row">
              <text class="list-title">{{ meal.title }}</text>
              <text class="list-value">{{ meal.kcal }} kcal</text>
            </view>
            <text class="list-meta">
              {{ meal.items.map((item) => `${item.name} ${item.grams}g`).join(' · ') }}
            </text>
            <text class="list-meta">
              蛋白 {{ meal.protein_g }}g · 脂肪 {{ meal.fat_g }}g · 碳水 {{ meal.carb_g }}g
            </text>
          </view>
        </view>

        <view class="intake">
          <view class="stat">
            <text class="stat-value">{{ macro.kcal }}</text>
            <text class="stat-label">推荐摄入 kcal</text>
          </view>
          <view class="stat">
            <text class="stat-value">{{ Math.round(macro.protein) }}g</text>
            <text class="stat-label">蛋白目标</text>
          </view>
          <view class="stat">
            <text class="stat-value">{{ meals.length }}</text>
            <text class="stat-label">餐次</text>
          </view>
        </view>
        <view class="muted" style="margin-top: 10px">
          目标来自教练 skill 的估算（Mifflin-St Jeor + 活动系数），不是医疗建议。
        </view>
      </view>

      <!-- 改计划 -->
      <view class="card">
        <view class="row">
          <view class="section-title">改计划</view>
          <text class="link" @click="openComposer()">自己说 →</text>
        </view>
        <view class="muted" style="margin-top: 6px">
          点一个方向，或者自己说一句。模型理解你的意思，安全门决定能不能执行。
        </view>
        <view class="chip-scroll">
          <view
            v-for="preset in presetCards"
            :key="preset.key"
            class="pill"
            @click="openComposer(preset.example, preset.key)"
          >
            {{ preset.label }}
          </view>
        </view>
        <view class="btn-row">
          <view class="btn-sm primary" @click="openComposer()">自己说一句</view>
          <view class="btn-sm" @click="draftOpen = true">重新生成整份计划</view>
          <view class="btn-sm" @click="openCheckin">去打卡</view>
        </view>
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
  background: var(--line-soft);
  border-radius: var(--radius-md);
  margin-top: 12px;
  padding: 12px 14px;
}

.meal-row {
  border-top: 1px solid var(--line-soft);
  display: flex;
  gap: 12px;
  padding: 12px 0;
}

.meal-row:first-of-type {
  border-top: none;
}

.intake {
  background: var(--primary-soft);
  border-radius: var(--radius-md);
  display: flex;
  gap: 22px;
  margin-top: 14px;
  padding: 12px 14px;
}

.intake .stat-value {
  color: var(--primary-deep);
}
</style>
