<script setup lang="ts">
/**
 * 今天：今天练什么、吃什么、还能改哪里。
 *
 * 版式参考食谱类 App：顶部一张暖色主卡片（今天最重要的一句话），
 * 下面是白卡片列表，动作和餐食都带缩略图，改动入口是横向胶囊。
 */
import { computed, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, ApiError, type CurrentPlan, type PatchPreset, type TodayAgentResult, type TodayPlanSnapshot } from '../../api'
import PatchComposer from '../../components/PatchComposer.vue'
import PlanDraftSheet from '../../components/PlanDraftSheet.vue'
import FoodOptionsSheet from '../../components/FoodOptionsSheet.vue'
import KnowledgeSheet from '../../components/KnowledgeSheet.vue'
import McdOrderSheet from '../../components/McdOrderSheet.vue'
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
const agentTrace = ref<Array<{ node: string; status: string; detail?: Record<string, unknown> }>>([])
/** 执行过程：开始时间 + 计时器，让"等待"变成看得见的时间 */
const agentStartedAt = ref(0)
const agentElapsed = ref(0)
const agentTraceOpen = ref(false)
let traceTimer: ReturnType<typeof setInterval> | null = null

/** trace 节点 → 人话（模型自己决定顺序，这里只负责翻译） */
const STEP_LABELS: Record<string, string> = {
  agent: '思考怎么安排',
  get_today_plan: '读今天的计划',
  get_nutrition_targets: '读营养目标',
  safety_screen: '安全筛查',
  search_knowledge: '查知识库',
  search_food_options: '找替换餐 / 平台链接',
  get_recovery_tips: '排训练与恢复时间',
  mcd_nutrition: '查麦当劳官方营养库',
  parse: '解析模型输出',
  validate: '校验结果',
}

const STEP_STATUS: Record<string, string> = {
  queued: '排队中',
  thinking: '正在思考…',
  tools: '',
  final: '',
  ok: '完成',
  failed: '没通过',
  repaired: '修好了',
}

const STEP_ICONS: Record<string, string> = {
  queued: '·',
  thinking: '◌',
  tools: '→',
  final: '→',
  ok: '✓',
  failed: '!',
  repaired: '↻',
}

function stepLabel(step: { node: string; status: string; detail?: Record<string, unknown> }) {
  // agent 的三种状态信息量最大：第几轮思考、这轮打算调哪些工具、还是开始写答案
  if (step.node === 'agent') {
    if (step.status === 'thinking') {
      const round = Number(step.detail?.step || 0)
      return round ? `第 ${round} 轮思考` : '思考怎么安排'
    }
    if (step.status === 'tools') {
      const calls = (step.detail?.tool_calls as string[] | undefined) || []
      // 同名工具可能连调多次（查 4 次知识库），合并成 "查知识库 ×4"
      const counted = new Map<string, number>()
      for (const name of calls) {
        const label = STEP_LABELS[name] || name
        counted.set(label, (counted.get(label) || 0) + 1)
      }
      const parts = [...counted.entries()].map(([label, times]) => (
        times > 1 ? `${label} ×${times}` : label
      ))
      return parts.length ? `决定调用：${parts.join('、')}` : '决定调用工具'
    }
    if (step.status === 'final') return '整理答案'
  }
  return STEP_LABELS[step.node] || step.node
}

const traceRows = computed(() => {
  const rows: Array<{
    key: string
    icon: string
    label: string
    statusText: string
    state: string
    count: number
  }> = []
  agentTrace.value.forEach((step, index) => {
    if (step.status === 'queued') return // 排队那一步没有信息量，不占一行
    const status = step.status || 'ok'
    const active = index === agentTrace.value.length - 1 && agentBusy.value
    const row = {
      key: `${step.node}-${index}`,
      icon: STEP_ICONS[status] || '·',
      label: stepLabel(step),
      // tools / final 这两种状态左边已经说清楚了，右边留空（不能用 || 兜底，空字符串是有效值）
      statusText: status in STEP_STATUS ? STEP_STATUS[status] : status,
      state: status === 'failed' ? 'failed' : active ? 'active' : 'done',
      count: 1,
    }
    const last = rows[rows.length - 1]
    // 连续相同的步骤（比如连着查 4 次知识库）合并成 "×4"，不要刷屏
    if (last && last.label === row.label && last.statusText === row.statusText
        && last.state === row.state) {
      last.count += 1
      return
    }
    rows.push(row)
  })
  return rows
})

const elapsedLabel = computed(() => `${agentElapsed.value}s`)

function startTraceTimer() {
  agentStartedAt.value = Date.now()
  agentElapsed.value = 0
  if (traceTimer) clearInterval(traceTimer)
  traceTimer = setInterval(() => {
    agentElapsed.value = Math.floor((Date.now() - agentStartedAt.value) / 1000)
  }, 500)
}

function stopTraceTimer() {
  if (traceTimer) {
    clearInterval(traceTimer)
    traceTimer = null
  }
}
const agentResult = ref<TodayAgentResult | null>(null)
const agentRunId = ref('')
const mcdOrderOpen = ref(false)
/** 今天已经保存过的规划（按天存，重开页面直接看到） */
const savedPlan = ref<TodayPlanSnapshot | null>(null)
const retargeting = ref(false)
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

/** 读今天的快照：有就直接能看，不用再花一次模型调用 */
async function loadSavedPlan() {
  try {
    savedPlan.value = await api.getTodayPlan(todayStr())
  } catch {
    savedPlan.value = null // 读不到就当作没生成过，不挡页面
  }
}

/** 打开抽屉看已保存的那一份（不再重新生成，但过程还能从 run 里读回来） */
async function viewSavedPlan() {
  if (!savedPlan.value) return
  agentResult.value = savedPlan.value.output
  agentRunId.value = savedPlan.value.run_id || ''
  agentTrace.value = []
  agentElapsed.value = 0
  agentOpen.value = true
  const runId = savedPlan.value.run_id
  if (!runId) return
  try {
    const run = await api.getRun(runId)
    agentTrace.value = (run.trace || []).map((item) => ({
      node: item.node,
      status: item.status,
      detail: item.detail as Record<string, unknown> | undefined,
    }))
    if (run.created_at && run.finished_at) {
      agentElapsed.value = Math.max(
        0,
        Math.round((new Date(run.finished_at).getTime() - new Date(run.created_at).getTime()) / 1000),
      )
    }
  } catch {
    // 过程拿不到不影响看结果
  }
}

/** 后端已按本地时间返回 "2026-11-05 20:23"，这里只取 "11-05 20:23" */
function savedAtLabel() {
  if (!savedPlan.value?.saved_at) return ''
  return savedPlan.value.saved_at.slice(5, 16)
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
  agentTraceOpen.value = false
  startTraceTimer()
  try {
    const started = await api.startTodayPlan(agentText.value.trim())
    agentRunId.value = started.run_id
    // 800ms 一轮：后端每完成一步就写库，前端就能"边跑边看"
    for (let attempt = 0; attempt < 40; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 800))
      const run = await api.getRun(started.run_id)
      agentTrace.value = (run.trace || []).map((item) => ({
        node: item.node,
        status: item.status,
        detail: item.detail as Record<string, unknown> | undefined,
      }))
      if (run.status !== 'running') {
        if (run.status === 'failed') {
          agentError.value = '规划失败，请重试'
        } else {
          agentResult.value = run.output as unknown as TodayAgentResult
          // 后端已经按天存好了，前端同步一下"已保存"状态
          await loadSavedPlan()
        }
        break
      }
    }
  } catch (err) {
    agentError.value = err instanceof Error ? err.message : '规划失败'
  } finally {
    stopTraceTimer()
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

/**
 * 打开麦当劳官方订餐页（https://www.mcd.cn/）。
 * 麦当劳网页端要先手机号登录、选门店才能下单，所以只能整页跳过去；
 * 单项没有可用的深链，点名字时顺便把餐名复制到剪贴板，省得用户自己打。
 */
function openMcdOrder(name: string) {
  const url = agentResult.value?.mcdonalds?.links?.order || 'https://www.mcd.cn/'
  // 先同步开新窗口：iOS Safari / Chrome 会把"异步回调里的 window.open"当弹窗拦掉，
  // 复制餐名放在后面，失败了也不影响跳转
  openAgentLink(url)
  if (name) {
    uni.setClipboardData({
      data: name,
      success: () => uni.showToast({ title: `已复制「${name}」`, icon: 'none' }),
    })
  }
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
/** 麦当劳营养库：预算来自后端（目标 - 今天已排），前端只做展示 */
const mcdBudget = computed(() => agentResult.value?.mcdonalds?.budget || null)
const mcdCombo = computed(() => agentResult.value?.mcdonalds?.combo || null)

/** 去麦当劳点餐时要匹配的餐名：搭配里的 + 单点候选里的，去重后最多 5 个 */
const mcdSuggestions = computed(() => {
  const block = agentResult.value?.mcdonalds
  if (!block) return [] as string[]
  const names = [
    ...(block.combo?.items || []).map((item) => item.name),
    ...(block.options || []).map((item) => item.name),
  ]
  return [...new Set(names.filter(Boolean))].slice(0, 5)
})

/** 麦当劳失败原因：把后端错误码翻成人能看懂的一句话 */
function mcdErrorLabel(code?: string) {
  if (!code) return '未知原因'
  if (code === 'mcd_mcp_unavailable') return '未配置 MCD_MCP_TOKEN'
  if (code === 'mcd_unauthorized') return 'MCP Token 无效或已过期'
  if (code === 'mcd_rate_limited') return '调用过于频繁（上限 600 次/分钟）'
  if (code === 'mcd_empty_menu') return '官方营养表解析失败'
  if (code === 'no_active_plan') return '当前没有生效中的计划'
  return code
}
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

async function retarget() {
  if (!plan.value || retargeting.value) return
  retargeting.value = true
  try {
    const result = await api.retargetPlan(plan.value.plan_id)
    uni.showToast({ title: `已按当前体重重算（v${result.new_version}）`, icon: 'none' })
    await load()
  } catch (err) {
    uni.showToast({ title: err instanceof Error ? err.message : '重算失败', icon: 'none' })
  } finally {
    retargeting.value = false
  }
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
  loadSavedPlan()
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
          {{ restMode || !training.length ? '今天休息' : '今日训练目标' }}
        </view>
        <view class="hero-sub">
          {{ training.length
            ? `${training.length} 个动作 · 顺序按教练安排走，状态不对就点下面的胶囊改，改完你确认才生效。`
            : '休息也是计划的一部分，明天继续。' }}
        </view>
      </view>

      <!-- 今日规划 Agent -->
      <view class="card">
        <view class="row">
          <view class="section-title">今日规划</view>
          <text class="link" @click="openAgent('帮我规划今天')">让 Agent 规划 →</text>
        </view>
        <view v-if="savedPlan" class="saved-row">
          <view class="saved-bits">
            <text class="saved-chip">今天已保存</text>
            <text class="list-meta">
              v{{ savedPlan.plan_version ?? '—' }} · {{ savedAtLabel() }} 生成
              <text v-if="savedPlan.has_mcdonalds"> · 含麦当劳推荐</text>
            </text>
          </view>
          <text class="link" @click="viewSavedPlan">查看 →</text>
        </view>
        <view v-else class="muted" style="margin-top: 6px">
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
        <view v-if="plan.nutrition?.targets_stale" class="stale-notice">
          <text class="stale-text">{{ plan.nutrition?.stale_hint }}</text>
          <view class="btn-sm primary" @click="retarget">
            {{ retargeting ? '重算中…' : '按当前体重重算' }}
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

        <view v-if="agentBusy" class="agent-card trace-card">
          <view class="card-head">
            <view class="trace-title">
              <view class="pulse-dot" />
              <text class="section-title">Agent 正在工作</text>
            </view>
            <text class="card-badge">{{ elapsedLabel }}</text>
          </view>
          <view class="trace-list">
            <view v-for="row in traceRows" :key="row.key" class="trace-row" :class="row.state">
              <text class="trace-icon">{{ row.icon }}</text>
              <text class="trace-label">
                {{ row.label }}<text v-if="row.count > 1" class="trace-count"> ×{{ row.count }}</text>
              </text>
              <text v-if="row.statusText" class="trace-status">{{ row.statusText }}</text>
            </view>
            <view v-if="!traceRows.length" class="trace-row active">
              <text class="trace-icon">◌</text>
              <text class="trace-label">正在启动模型…</text>
            </view>
          </view>
          <text class="trace-hint">
            这里每一步都是真实发生的：模型决定调哪个工具，代码只做白名单和结果校验。
          </text>
        </view>

        <view v-if="agentError" class="error" style="margin-top: 12px">{{ agentError }}</view>

        <template v-if="agentResult">
          <view v-if="agentTrace.length" class="trace-summary">
            <text class="list-meta">
              {{ agentElapsed }}s · {{ agentTrace.length }} 步完成
            </text>
            <text class="link" @click="agentTraceOpen = !agentTraceOpen">
              {{ agentTraceOpen ? '收起过程' : '看过程 →' }}
            </text>
          </view>
          <view v-if="agentTraceOpen" class="trace-list" style="margin: 8px 0 4px">
            <view v-for="row in traceRows" :key="row.key" class="trace-row" :class="row.state">
              <text class="trace-icon">{{ row.icon }}</text>
              <text class="trace-label">
                {{ row.label }}<text v-if="row.count > 1" class="trace-count"> ×{{ row.count }}</text>
              </text>
              <text v-if="row.statusText" class="trace-status">{{ row.statusText }}</text>
            </view>
          </view>

          <view class="draft-summary">{{ agentResult.summary }}</view>

          <view v-if="agentResult.schedule.length" class="agent-card schedule-card">
            <view class="card-head">
              <text class="section-title">今日安排</text>
              <text class="card-badge">{{ agentResult.schedule.length }} 个时段</text>
            </view>
            <view class="timeline">
              <view v-for="item in agentResult.schedule" :key="item.time" class="tl-item">
                <view class="tl-rail">
                  <text class="tl-time">{{ item.time }}</text>
                  <view class="tl-dot" />
                </view>
                <view class="tl-body">
                  <text class="tl-activity">{{ item.activity }}</text>
                  <text v-if="item.why" class="tl-why">{{ item.why }}</text>
                </view>
              </view>
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

          <view v-if="agentResult.mcdonalds" class="agent-card mcd-card">
            <view class="card-head">
              <view class="mcd-head-left">
                <view class="mcd-mark">M</view>
                <view class="mcd-titles">
                  <text class="mcd-name">麦当劳 · 官方营养库</text>
                  <text class="mcd-sub">留够额度再点，数字来自官方营养表</text>
                </view>
              </view>
            </view>

            <view v-if="agentResult.mcdonalds.status !== 'ok'" class="muted" style="margin-top: 6px">
              这次没取到麦当劳数据（{{ mcdErrorLabel(agentResult.mcdonalds.error || agentResult.mcdonalds.status) }}），其余推荐不受影响。
            </view>

            <view v-else>
              <view v-if="mcdBudget" class="mcd-budget">
                <view class="mcd-budget-bit">
                  <text class="mcd-budget-value">{{ mcdBudget.remaining_kcal }}<text class="mcd-budget-unit">kcal</text></text>
                  <text class="mcd-budget-label">今天还剩的热量</text>
                </view>
                <view class="mcd-budget-bit">
                  <text class="mcd-budget-value">{{ mcdBudget.remaining_protein_g }}<text class="mcd-budget-unit">g</text></text>
                  <text class="mcd-budget-label">蛋白缺口</text>
                </view>
                <text class="mcd-budget-note">计划 {{ mcdBudget.plan_kcal }} / 目标 {{ mcdBudget.target_kcal }} kcal</text>
              </view>

              <view v-if="mcdCombo" class="mcd-combo">
                <text class="mcd-combo-label">想点麦当劳可以这么凑</text>
                <text class="mcd-combo-line">
                  {{ mcdCombo.items.map((item) => `${item.name}（${item.kcal}）`).join(' + ') }}
                  = {{ mcdCombo.kcal }} kcal · 蛋白 {{ mcdCombo.protein_g }} g
                </text>
              </view>
              <view v-else class="muted" style="margin-top: 6px">
                今天的热量额度已经排满，想吃麦当劳就替换一顿，别把它当加餐。
              </view>

              <view
                v-for="item in agentResult.mcdonalds.options || []"
                :key="item.name"
                class="mcd-item"
                @click="openMcdOrder(item.name)"
              >
                <view class="mcd-item-main">
                  <text class="mcd-item-name">{{ item.name }}</text>
                  <text class="mcd-item-why">{{ item.why }}</text>
                </view>
                <view class="mcd-item-right">
                  <text class="mcd-item-kcal">{{ item.kcal }} kcal · 蛋白 {{ item.protein_g }}g</text>
                  <text class="mcd-item-go">去点餐 →</text>
                </view>
              </view>

              <view class="mcd-cta" @click="openMcdOrder('')">
                去麦当劳点餐（自己选门店）
              </view>
              <view class="mcd-cta-outline" @click="mcdOrderOpen = true">
                让 Agent 帮我点好（到付款前）→
              </view>
              <text class="mcd-hint">
                {{ agentResult.mcdonalds.order_hint || '登录 → 选门店 → 加购物车 → 想付再付' }}。
                我们只推荐、不代下单也不收款；点某一项会先复制餐名，方便你在麦当劳页面直接搜。
              </text>

              <text class="mcd-note">
                {{ agentResult.mcdonalds.note }}
                <text v-if="agentResult.mcdonalds.fetched_at">（官方数据取于 {{ agentResult.mcdonalds.fetched_at }}）</text>
              </text>
            </view>
          </view>

          <view v-if="agentResult.tips.length" class="agent-card">
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

          <view v-if="agentResult.citations.length" class="agent-card">
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
    <McdOrderSheet
      :visible="mcdOrderOpen"
      :suggestions="mcdSuggestions"
      @close="mcdOrderOpen = false"
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
/* ---- 执行过程：让"思考中"变成看得见的步骤 ---- */
.trace-card {
  background: linear-gradient(180deg, #fffaf6 0%, #ffffff 70%);
  margin-top: 14px;
}

.trace-title {
  align-items: center;
  display: flex;
  gap: 8px;
}

.pulse-dot {
  animation: pulse 1.4s ease-in-out infinite;
  background: var(--primary);
  border-radius: 999px;
  height: 7px;
  width: 7px;
}

@keyframes pulse {
  0%, 100% { opacity: 0.35; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1.15); }
}

.trace-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 4px;
}

.trace-row {
  align-items: center;
  display: flex;
  gap: 8px;
  padding: 3px 0;
}

.trace-icon {
  flex: 0 0 16px;
  font-size: 12px;
  text-align: center;
}

.trace-label {
  color: var(--ink-soft);
  flex: 1;
  font-size: 13px;
}

.trace-status {
  color: var(--muted);
  flex: 0 0 auto;
  font-size: 11px;
}

.trace-count {
  color: var(--primary-deep);
  font-size: 11px;
  font-weight: 600;
}

.trace-row.done .trace-icon { color: var(--primary); }
.trace-row.active .trace-icon { color: var(--primary-deep); }
.trace-row.active .trace-label { color: var(--ink); font-weight: 600; }
.trace-row.failed .trace-icon,
.trace-row.failed .trace-status { color: #b91c1c; }

.trace-hint {
  color: var(--muted);
  display: block;
  font-size: 11px;
  line-height: 1.5;
  margin-top: 10px;
}

.trace-summary {
  align-items: center;
  display: flex;
  gap: 10px;
  justify-content: space-between;
  margin-top: 10px;
}

.stale-notice {
  align-items: flex-start;
  background: #fff7ed;
  border: 1px solid #fed7aa;
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 10px;
  padding: 10px 12px;
}

.stale-text {
  color: #b45309;
  font-size: 12px;
  line-height: 1.5;
}

.saved-row {
  align-items: center;
  display: flex;
  gap: 10px;
  justify-content: space-between;
  margin-top: 8px;
}

.saved-bits {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.saved-chip {
  align-self: flex-start;
  background: var(--primary-soft);
  border-radius: 999px;
  color: var(--primary-deep);
  font-size: 11px;
  font-weight: 700;
  padding: 2px 8px;
}

/* ---- Agent 结果卡片 ----
   之前所有块都只有 .section-title + 一堆 list-row，视觉上糊成一片：
   给每个块一个卡片边界，今日安排用时间轴、麦当劳用品牌色，扫一眼就知道是什么。 */
.agent-card {
  background: var(--surface);
  border: 1px solid var(--line-soft);
  border-radius: 16px;
  margin-top: 14px;
  padding: 14px;
}

.card-head {
  align-items: center;
  display: flex;
  justify-content: space-between;
  margin-bottom: 6px;
}

.card-badge {
  background: var(--primary-soft);
  border-radius: 999px;
  color: var(--primary-deep);
  font-size: 11px;
  padding: 2px 8px;
}

/* 今日安排：左边时间、右边做什么 + 为什么，竖线把它们串成一天 */
.schedule-card {
  background: linear-gradient(180deg, #fff7f1 0%, #ffffff 62%);
}

.timeline {
  display: flex;
  flex-direction: column;
}

.tl-item {
  display: flex;
  gap: 10px;
  padding: 9px 0;
}

.tl-item + .tl-item {
  border-top: 1px dashed var(--line-soft);
}

.tl-rail {
  align-items: center;
  display: flex;
  flex: 0 0 44px;
  flex-direction: column;
  width: 44px;
}

.tl-time {
  color: var(--primary-deep);
  font-size: 13px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.tl-dot {
  background: var(--primary);
  border-radius: 999px;
  height: 6px;
  margin-top: 6px;
  width: 6px;
}

.tl-body {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.tl-activity {
  color: var(--ink);
  font-size: 14px;
  line-height: 1.45;
}

.tl-why {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}

/* 麦当劳：金拱门红 + 黄，和上面的计划块明显区分 */
.mcd-card {
  border-color: #f1d7cd;
}

.mcd-head-left {
  align-items: center;
  display: flex;
  gap: 10px;
}

.mcd-mark {
  align-items: center;
  background: #da291c;
  border-radius: 9px;
  color: #ffc72c;
  display: flex;
  flex: 0 0 30px;
  font-size: 17px;
  font-weight: 800;
  height: 30px;
  justify-content: center;
  width: 30px;
}

.mcd-titles {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.mcd-name {
  color: var(--ink);
  font-size: 14px;
  font-weight: 700;
}

.mcd-sub {
  color: var(--muted);
  font-size: 11px;
}

.mcd-budget {
  align-items: baseline;
  background: #fff5f1;
  border-radius: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  margin-top: 10px;
  padding: 10px 12px;
}

.mcd-budget-bit {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.mcd-budget-unit {
  font-size: 12px;
  font-weight: 600;
  margin-left: 2px;
}

.mcd-budget-value {
  color: #c2410c;
  font-size: 20px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.mcd-budget-label {
  color: var(--muted);
  font-size: 11px;
}

.mcd-budget-note {
  color: var(--muted);
  font-size: 11px;
  margin-left: auto;
}

.mcd-combo {
  border: 1px dashed #e6b9a9;
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 10px;
  padding: 10px 12px;
}

.mcd-combo-label {
  color: #c2410c;
  font-size: 12px;
  font-weight: 700;
}

.mcd-combo-line {
  color: var(--ink-soft);
  font-size: 12px;
  line-height: 1.5;
}

.mcd-item {
  align-items: center;
  border-top: 1px solid var(--line-soft);
  display: flex;
  gap: 10px;
  justify-content: space-between;
  padding: 11px 0;
}

.mcd-item-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.mcd-item-name {
  color: var(--ink);
  font-size: 14px;
  font-weight: 600;
}

.mcd-item-why {
  color: var(--muted);
  font-size: 11px;
}

.mcd-item-right {
  align-items: flex-end;
  display: flex;
  flex: 0 0 auto;
  flex-direction: column;
  gap: 3px;
}

.mcd-item-kcal {
  color: var(--ink-soft);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.mcd-item-go {
  color: #c2410c;
  font-size: 11px;
  font-weight: 700;
}

.mcd-cta-outline {
  align-items: center;
  border: 1px solid #da291c;
  border-radius: 999px;
  color: #c2410c;
  display: flex;
  font-size: 13px;
  font-weight: 700;
  justify-content: center;
  margin-top: 8px;
  padding: 10px 16px;
}

.mcd-cta {
  align-items: center;
  background: #da291c;
  border-radius: 999px;
  color: #fff;
  display: flex;
  font-size: 14px;
  font-weight: 700;
  justify-content: center;
  margin-top: 12px;
  padding: 11px 16px;
}

.mcd-hint {
  color: var(--muted);
  display: block;
  font-size: 11px;
  line-height: 1.5;
  margin-top: 8px;
}

.mcd-note {
  color: var(--muted);
  display: block;
  font-size: 11px;
  line-height: 1.5;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--line-soft);
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
