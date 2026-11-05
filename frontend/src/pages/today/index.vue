<script setup lang="ts">
/**
 * 今天：今天练什么、吃什么、还能改哪里。
 *
 * 版式参考食谱类 App：顶部一张暖色主卡片（今天最重要的一句话），
 * 下面是白卡片列表，动作和餐食都带缩略图，改动入口是横向胶囊。
 */
import { computed, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, ApiError, type CurrentPlan, type PatchPreset, type TodayAgentResult } from '../../api'
import PatchComposer from '../../components/PatchComposer.vue'
import PlanDraftSheet from '../../components/PlanDraftSheet.vue'
import FoodOptionsSheet from '../../components/FoodOptionsSheet.vue'
import KnowledgeSheet from '../../components/KnowledgeSheet.vue'
import { QUICK_ACTIONS, actionLabel, slotLabel } from '../../styles/copy'
import { THUMBS, thumbOf } from '../../styles/thumbs'
import { WEEKDAY_LABELS, isoWeekday, todayStr } from '../../styles/dates'

const plan = ref<CurrentPlan | null>(null)
const presets = ref<PatchPreset[]>([])
const loading = ref(true)
const error = ref('')
const hasPlan = ref(true)

const composerOpen = ref(false)
const presetText = ref('')
const presetQuick = ref('')
const draftOpen = ref(false)

// 订餐推荐 & 知识卡片
const foodOpen = ref(false)
const foodMeal = ref<{ slot: string; title: string } | null>(null)
const knowledgeOpen = ref(false)
const knowledgeQuestion = ref('')
const knowledgeBodyPart = ref('')

// ---- 今日规划 Agent：模型自己决定调哪些工具 ----
const agentOpen = ref(false)
const agentText = ref('')
const agentBusy = ref(false)
const agentTrace = ref<Array<{ node: string; status: string }>>([])
const agentResult = ref<TodayAgentResult | null>(null)
const agentRunId = ref('')
const agentError = ref('')

const MEAL_TITLES: Record<string, string> = {
  breakfast: '早餐',
  lunch: '午餐',
  dinner: '晚餐',
  snack: '加餐',
}

const AGENT_PRESETS = ['帮我规划今天', '今天想休息', '午餐换清淡一点', '膝盖不舒服，别安排深蹲']
const selectedMeals = ref<Record<string, string>>({})
const applying = ref(false)

function toggleMeal(slot: string, title: string) {
  selectedMeals.value = {
    ...selectedMeals.value,
    [slot]: selectedMeals.value[slot] === title ? '' : title,
  }
}

function citationById(id: string) {
  return (agentResult.value?.citations || []).find((item) => item.id === id)
}

async function applySelections() {
  if (applying.value || !agentResult.value) return
  const meals = agentResult.value.meals || []
  const selections = meals
    .map((meal) => {
      const title = selectedMeals.value[meal.slot]
      const option = (meal.options || []).find((item) => item.title === title)
      return option ? { slot: meal.slot, ...option } : null
    })
    .filter(Boolean)
  if (!selections.length) {
    uni.showToast({ title: '先选每餐想要的选项', icon: 'none' })
    return
  }
  applying.value = true
  try {
    const result = await api.applyAgentMeals(agentRunId.value, selections, todayStr())
    uni.showToast({ title: `已替换今日餐食（v${result.new_version}）`, icon: 'none' })
    selectedMeals.value = {}
    await load()
  } catch (err) {
    uni.showToast({ title: err instanceof Error ? err.message : '保存失败', icon: 'none' })
  } finally {
    applying.value = false
  }
}

function openAgent(text = '') {
  agentText.value = text
  agentOpen.value = true
}

async function runAgent() {
  if (agentBusy.value || !agentText.value.trim()) return
  agentBusy.value = true
  agentError.value = ''
  agentResult.value = null
  agentTrace.value = []
  try {
    const started = await api.startTodayPlan(agentText.value.trim())
    agentRunId.value = started.run_id
    for (let attempt = 0; attempt < 25; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1200))
      const run = await api.getRun(started.run_id)
      agentTrace.value = (run.trace || []).map((item) => ({ node: item.node, status: item.status }))
      if (run.status !== 'running') {
        if (run.status === 'failed') {
          agentError.value = '规划失败，请重试'
        } else {
          agentResult.value = run.output as unknown as TodayAgentResult
        }
        break
      }
    }
  } catch (err) {
    agentError.value = err instanceof Error ? err.message : '规划失败'
  } finally {
    agentBusy.value = false
  }
}

function agentLinks(links: Record<string, string>) {
  return [
    { label: '京东', url: links.jd },
    { label: '淘宝', url: links.taobao },
    { label: '美团', url: links.meituan },
  ]
}

function openFoodOptions(meal: { slot: string; title: string }) {
  foodMeal.value = meal
  foodOpen.value = true
}

function openAgentLink(url: string) {
  if (!url) return
  const globalOpen = (globalThis as unknown as { open?: (u: string, t?: string) => unknown }).open
  if (typeof globalOpen === 'function') {
    globalOpen(url, '_blank')
    return
  }
  uni.setClipboardData({ data: url })
}

function openKnowledge(question: string, bodyPart = '') {
  knowledgeQuestion.value = question
  knowledgeBodyPart.value = bodyPart
  knowledgeOpen.value = true
}

function actionBodyPart(action: { meta?: Record<string, unknown> }): string {
  const mode = String((action.meta || {}).movement_mode || '')
  if (mode === 'knee_dominant' || mode === 'hip_dominant') return 'knee'
  if (mode === 'horizontal_push' || mode === 'vertical_push') return 'chest'
  if (mode === 'horizontal_pull' || mode === 'vertical_pull') return 'back'
  if (mode === 'core') return 'core'
  return ''
}

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
      {{ hasPlan ? '你想要的改变，藏在今天的每一次训练里。' : '填完基础信息和目标，我让教练生成计划给你确认。' }}
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

      <!-- 今日规划 Agent -->
      <view class="card">
        <view class="row">
          <view class="section-title">今日规划</view>
          <text class="link" @click="openAgent('帮我规划今天')">让 Agent 规划 →</text>
        </view>
        <view class="muted" style="margin-top: 6px">
          它会自己决定先查知识、查计划，还是先找外卖；每一步都能看到。
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
            <text class="link" @click="openKnowledge(`${actionLabel(action)} 的动作要领`, actionBodyPart(action))">
              要领
            </text>
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
            <text
              class="link"
              @click="openFoodOptions({ slot: meal.slot, title: meal.title })"
            >
              换一换 / 点外卖
            </text>
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
          热量与蛋白目标是按你的身高、体重、训练天数估算的，仅供参考，不构成医疗建议。
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
    <!-- 今日规划抽屉 -->
    <view v-if="agentOpen">
      <view class="sheet-mask" @click="agentOpen = false" />
      <view class="sheet">
        <view class="row">
          <view class="section-title">帮我规划今天</view>
          <text class="link" @click="agentOpen = false">关闭</text>
        </view>

        <textarea
          v-if="!agentResult && !agentBusy"
          v-model="agentText"
          class="input"
          maxlength="300"
          placeholder="例如：帮我规划今天，午餐想吃清淡一点"
        />
        <view v-if="!agentResult && !agentBusy" class="chip-scroll">
          <view v-for="item in AGENT_PRESETS" :key="item" class="pill" @click="agentText = item">
            {{ item }}
          </view>
        </view>
        <view v-if="!agentResult && !agentBusy" class="btn-row">
          <view class="btn-sm primary" @click="runAgent">开始规划</view>
        </view>

        <view v-if="agentBusy" class="block">
          <view class="section-title">Agent 正在工作</view>
          <view v-for="(step, index) in agentTrace" :key="index" class="list-row">
            <text class="list-meta">✓ {{ step.node }}</text>
            <text class="list-meta">{{ step.status }}</text>
          </view>
          <view v-if="!agentTrace.length" class="muted" style="margin-top: 8px">
            正在启动模型…
          </view>
        </view>

        <view v-if="agentError" class="error" style="margin-top: 12px">{{ agentError }}</view>

        <template v-if="agentResult">
          <view class="draft-summary">{{ agentResult.summary }}</view>

          <view v-if="agentResult.schedule.length" class="block">
            <view class="section-title">今日安排</view>
            <view v-for="item in agentResult.schedule" :key="item.time" class="list-row">
              <text class="list-title">{{ item.time }} · {{ item.activity }}</text>
            </view>
          </view>

          <view v-for="meal in agentResult.meals" :key="meal.slot" class="agent-meal">
            <view class="agent-meal-head">
              <text class="list-title">{{ MEAL_TITLES[meal.slot] || meal.slot }}</text>
              <text class="list-meta">目标 {{ meal.target_kcal }} kcal</text>
            </view>
            <view
              v-for="option in meal.options"
              :key="option.title"
              class="food-card"
              :class="{ chosen: selectedMeals[meal.slot] === option.title }"
              @click="toggleMeal(meal.slot, option.title)"
            >
              <view class="row">
                <text class="list-title">{{ option.title }}</text>
                <text class="list-value">{{ option.kcal }} kcal</text>
              </view>
              <text class="list-meta">蛋白 {{ option.protein_g }}g · {{ option.reason }}</text>
              <view class="btn-row">
                <view
                  v-for="link in agentLinks(option.links)"
                  :key="link.label"
                  class="btn-sm"
                  @click="openAgentLink(link.url)"
                >
                  {{ link.label }}
                </view>
              </view>
            </view>
          </view>

          <view v-if="agentResult.mcdonalds" class="block">
            <view class="section-title">麦当劳 · 可直接点餐</view>
            <view class="muted" style="margin-top: 4px">
              {{ agentResult.mcdonalds.items && agentResult.mcdonalds.items.length
                ? '以下价格来自麦当劳 MCP，点单前以官方 App 为准。'
                : '麦当劳 MCP 暂无数据。' }}
            </view>
            <view v-for="(item, index) in agentResult.mcdonalds.items || []" :key="index" class="list-row">
              <text class="list-meta">{{ JSON.stringify(item).slice(0, 120) }}</text>
            </view>
          </view>

          <view v-if="agentResult.tips.length" class="block">
            <view class="section-title">注意事项</view>
            <view v-for="(tip, index) in agentResult.tips" :key="index" class="tip-row">
              <text class="list-meta">{{ tip.text }}</text>
              <view class="tip-sources">
                <text
                  v-for="sourceId in tip.source_ids || []"
                  :key="sourceId"
                  class="link"
                  @click="openAgentLink(citationById(sourceId)?.source_url || '')"
                >
                  {{ citationById(sourceId)?.title || sourceId }} →
                </text>
              </view>
            </view>
          </view>

          <view v-if="agentResult.citations.length" class="block">
            <view class="section-title">依据</view>
            <view v-for="item in agentResult.citations" :key="item.id" class="list-row">
              <text class="list-meta">{{ item.title }}</text>
              <text class="link" @click="openAgentLink(item.source_url)">来源 →</text>
            </view>
          </view>

          <view class="muted" style="margin-top: 12px">{{ agentResult.disclaimer }}</view>
          <view class="btn-row">
            <view class="btn-sm primary" @click="applySelections">
              {{ applying ? '保存中…' : '应用选中的餐食' }}
            </view>
            <view class="btn-sm" @click="agentResult = null; agentTrace = []; selectedMeals = {}">
              重新规划
            </view>
          </view>
        </template>
      </view>
    </view>

    <FoodOptionsSheet
      v-if="plan"
      :visible="foodOpen"
      :plan-id="plan.plan_id"
      :date="todayStr()"
      :meal-slot="foodMeal?.slot || ''"
      :meal-title="foodMeal?.title || '这一餐'"
      @close="foodOpen = false"
    />
    <KnowledgeSheet
      :visible="knowledgeOpen"
      :question="knowledgeQuestion"
      :body-part="knowledgeBodyPart"
      @close="knowledgeOpen = false"
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
.agent-meal {
  border: 1px solid var(--line-soft);
  border-radius: 16px;
  margin-top: 14px;
  overflow: hidden;
}
.agent-meal-head {
  align-items: baseline;
  background: var(--primary-soft);
  display: flex;
  justify-content: space-between;
  padding: 10px 12px;
}
.food-card {
  border-top: 1px solid var(--line-soft);
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
}
.food-card.chosen {
  background: var(--primary-soft);
  box-shadow: inset 0 0 0 2px var(--primary);
}
.tip-row {
  border-top: 1px solid var(--line-soft);
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 0;
}
.tip-sources {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
</style>
