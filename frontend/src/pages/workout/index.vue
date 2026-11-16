<script setup lang="ts">
import { actionLabel } from '../../styles/copy'
import StableScroll from '../../components/StableScroll.vue'
/**
 * 训练执行页：从「今天练什么」进入「现在练到哪一组」。
 *
 * 所有状态都来自后端 WorkoutSession：
 * - 完成一组 → POST /sets（真实落库）
 * - 跳过 / 替换 / 暂停 → 只改当前及之后，已完成组数保留
 * - 结束 → POST /finish，写 WorkoutLog + 短期记忆
 * 训练中的 Agent 入口和今天页共用同一个 /agent/chat，只是多带 session_id。
 */
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { onLoad } from '@dcloudio/uni-app'
import {
  api,
  ApiError,
  type AgentDay,
  type WorkoutSession,
} from '../../api'
import GlobalCoachWorkspace from '../../components/GlobalCoachWorkspace.vue'
import DesktopSidebar from '../../components/DesktopSidebar.vue'
import { syncDesktopChrome } from '../../composables/desktopChrome'
import KnowledgeSheet from '../../components/KnowledgeSheet.vue'
import { slotLabel } from '../../styles/copy'
import { thumbOf } from '../../styles/thumbs'
import { todayStr } from '../../styles/dates'

const equipmentNames:Record<string,string>={bodyweight:'徒手',dumbbell:'哑铃',barbell:'杠铃',band:'弹力带',bench:'训练凳',treadmill:'跑步机',mat:'瑜伽垫',kettlebell:'壶铃'}
const date = ref(todayStr())
const sessionId = ref('')
const session = ref<WorkoutSession | null>(null)
// 组记录：默认带出计划次数，重量留空；换动作时重置，避免把上一个动作的重量带过去。
const setReps = ref<number | null>(null)
const setWeight = ref<number | null>(null)
const day = ref<AgentDay | null>(null)
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const toast = ref('')

const coachOpen = ref(false)
const coachText = ref('')
const coachAuto = ref(false)

const restRemaining = ref(0)
let restTimer: ReturnType<typeof setInterval> | null = null

const finishOpen = ref(false)
const finishResult = ref<Record<string, unknown> | null>(null)
const finishForm = reactive({
  completed: true,
  difficulty: 3,
  fatigue: 3,
  actual_minutes: '',
  note: '',
})

const knowledgeOpen = ref(false)
const knowledgeQuestion = ref('')

const currentItem = computed(() => {
  if (!session.value) return null
  if (session.value.current_item) return session.value.current_item
  return session.value.items.find((item) => item.status === 'pending' || item.status === 'active') || null
})
// currentItem 声明之后再监听，避免在 setup 阶段读到 TDZ。
watch(
  () => currentItem.value?.action_ref,
  () => {
    setReps.value = currentItem.value?.target_reps ?? null
    setWeight.value = null
  },
  { immediate: true },
)
const progress = computed(() => session.value?.progress || null)
const isFinished = computed(
  () => !!finishResult.value || session.value?.status === 'finished',
)
const showRest = computed(() => restRemaining.value > 0 && !isFinished.value)
const nextItems = computed(() =>
  (session.value?.items || []).filter((item) => item.status === 'pending' || item.status === 'active'),
)

const summary = computed(() => finishResult.value?.summary as Record<string, unknown> | undefined)
const summaryItems = computed<Array<Record<string, unknown>>>(() => {
  const items = summary.value?.items
  return Array.isArray(items) ? items as Array<Record<string, unknown>> : []
})

function setDifficulty(event: { detail: { value: number } }) {
  finishForm.difficulty = Number(event.detail.value)
}

function setFatigue(event: { detail: { value: number } }) {
  finishForm.fatigue = Number(event.detail.value)
}

function describeError(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return '请求失败'
}

function openCoach(text = '', auto = false) {
  coachText.value = text
  coachAuto.value = auto
  coachOpen.value = true
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    if (sessionId.value) {
      session.value = await api.getWorkout(sessionId.value)
      const active = await api.getActiveWorkout(date.value)
      day.value = active.day
    } else {
      const active = await api.getActiveWorkout(date.value)
      day.value = active.day
      if (active.session) {
        session.value = active.session
        sessionId.value = active.session.session_id
      } else {
        const started = await api.startWorkout(date.value)
        session.value = started.session
        day.value = started.day
        sessionId.value = started.session.session_id
      }
    }
    if (session.value?.status === 'finished') {
      const payload = await api.getWorkout(session.value.session_id)
      session.value = payload
      if (!finishResult.value) {
        finishResult.value = {
          summary: {
            actual_minutes: payload.actual_minutes,
            actual_minutes_known: payload.actual_minutes_known,
            completion_pct: payload.completion_pct,
            difficulty: payload.difficulty,
            fatigue: payload.fatigue,
            done_actions: payload.progress.done_actions,
            total_actions: payload.progress.total_actions,
            kcal: 0,
          },
        }
      }
    }
  } catch (err) {
    error.value = describeError(err)
  } finally {
    loading.value = false
  }
}

function stopRest() {
  if (restTimer) {
    clearInterval(restTimer)
    restTimer = null
  }
  restRemaining.value = 0
}

function startRest(seconds: number) {
  stopRest()
  restRemaining.value = Math.max(5, Math.min(300, Number(seconds) || 60))
  restTimer = setInterval(() => {
    restRemaining.value = Math.max(0, restRemaining.value - 1)
    if (restRemaining.value === 0) stopRest()
  }, 1000)
}

function adjustRest(delta: number) {
  if (restRemaining.value <= 0) return
  restRemaining.value = Math.max(0, Math.min(600, restRemaining.value + delta))
}

async function completeSet() {
  if (!session.value || busy.value) return
  const previous = currentItem.value
  busy.value = true
  error.value = ''
  try {
    const result = await api.completeWorkoutSet(session.value.session_id, {
      reps: setReps.value,
      weight_kg: setWeight.value,
    })
    session.value = result.session
    toast.value = previous ? `「${previous.name}」完成一组` : '完成一组'
    if (result.session.status !== 'completed' && result.session.status !== 'finished') {
      startRest(previous?.rest_sec || 60)
    }
    if (result.session.status === 'completed') {
      stopRest()
      openFinish()
    }
  } catch (err) {
    error.value = describeError(err)
  } finally {
    busy.value = false
  }
}

async function skipAction() {
  if (!session.value || busy.value) return
  busy.value = true
  try {
    const result = await api.skipWorkoutAction(session.value.session_id, '用户跳过')
    session.value = result.session
    stopRest()
    toast.value = '已跳过当前动作，完成记录保留'
    if (result.session.status === 'completed') openFinish()
  } catch (err) {
    error.value = describeError(err)
  } finally {
    busy.value = false
  }
}

function replaceMenu() {
  if (!session.value || busy.value) return
  uni.showActionSheet({
    itemList: ['器械被占用，换徒手动作', '我有点累，简单一点', '这个动作我不会', '换个更适合我的动作'],
    success: async (result) => {
      if (result.tapIndex === 2) {
        openCoach('这个动作我不会。', true)
        return
      }
      if (result.tapIndex === 3) {
        openCoach('我想换一个动作，帮我挑一个同肌群的。', true)
        return
      }
      busy.value = true
      try {
        const response = await api.replaceWorkoutAction(session.value!.session_id, {
          equipment_busy: result.tapIndex === 0,
          easier: result.tapIndex === 1,
          reason: result.tapIndex === 0 ? '器械被占用，换成徒手动作' : '按疲劳程度降低难度',
        })
        session.value = response.session
        stopRest()
        toast.value = response.changes[0] || '已调整当前动作'
      } catch (err) {
        error.value = describeError(err)
      } finally {
        busy.value = false
      }
    },
  })
}

async function togglePause() {
  if (!session.value || busy.value) return
  busy.value = true
  try {
    const result = await api.pauseWorkout(session.value.session_id)
    session.value = result.session
    toast.value = result.session.status === 'paused' ? '已暂停' : '继续训练'
    if (result.session.status === 'paused') stopRest()
  } catch (err) {
    error.value = describeError(err)
  } finally {
    busy.value = false
  }
}

function openGuidance() {
  if (!currentItem.value) return
  knowledgeQuestion.value = `${currentItem.value.name} 的动作要领`
  knowledgeOpen.value = true
}

function openFinish() {
  if (!session.value) return
  finishForm.completed = true
  finishForm.difficulty = session.value.difficulty || 3
  finishForm.fatigue = session.value.fatigue || 3
  finishForm.actual_minutes = ''
  finishForm.note = ''
  finishOpen.value = true
}

async function submitFinish() {
  if (!session.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const result = await api.finishWorkout(session.value.session_id, {
      completed: finishForm.completed,
      completion_basis: finishForm.completed ? 'confirm_current_plan' : 'recorded_only',
      expected_revision: session.value.record_revision,
      completion_pct: finishForm.completed ? 100 : session.value.progress.percent,
      difficulty: finishForm.difficulty,
      fatigue: finishForm.fatigue,
      actual_minutes: finishForm.actual_minutes.trim() ? Number(finishForm.actual_minutes) : undefined,
      note: finishForm.note,
    })
    session.value = result.session
    day.value = result.day
    finishResult.value = result
    finishOpen.value = false
    stopRest()
  } catch (err) {
    error.value = describeError(err)
  } finally {
    busy.value = false
  }
}

async function reloadFromAgent() {
  await load()
}

function backToToday() {
  uni.switchTab({ url: '/pages/today/index' })
}

function onOpenTodayBrief() {
  uni.switchTab({ url: '/pages/today/index' })
}

function onOpenMemory() {
  uni.switchTab({ url: '/pages/profile/index' })
}

function onOpenProgress() {
  uni.switchTab({ url: '/pages/progress/index' })
}

onLoad((options) => {
  syncDesktopChrome()
  if (options?.date) date.value = String(options.date)
  if (options?.session_id) sessionId.value = String(options.session_id)
  load()
})

onUnmounted(stopRest)
</script>

<template>
  <view class="page workout-page with-sidebar">
    <DesktopSidebar active="" />
    <view class="workout-header"><view>
    <view class="eyebrow">TRAINING · {{ date }}</view>
    <view class="title">{{ isFinished ? '这次练完了。' : '现在，练这一组。' }}</view>
    <view class="subtitle">
      {{ isFinished ? '记录已经保存，下一次安排会参考这次完成度和难度。' : '只记录事实：完成一组点一下，其他的交给我调整。' }}
    </view>

    </view><view v-if="progress" class="workout-overview"><text>{{ progress.done_sets }}/{{ progress.total_sets }}</text><text>组已记录 · {{ progress.percent }}%</text></view></view>

    <view v-if="loading" class="card muted">正在准备训练…</view>
    <view v-else-if="error" class="error">{{ error }}</view>

    <!-- 结束后的总结 -->
    <template v-else-if="isFinished && summary">
      <view class="hero">
        <view class="row">
          <text class="badge">{{ Number(summary.completion_pct || 0).toFixed(0) }}% 完成</text>
          <text class="badge">{{ summary.done_actions || 0 }}/{{ summary.total_actions || 0 }} 动作</text>
        </view>
        <view class="hero-title">训练记录已保存</view>
        <view class="hero-sub">
          {{ summary.actual_minutes_known===false ? '实际时长未记录' : `${summary.actual_minutes || 0} 分钟` }} · 难度 {{ summary.difficulty || '—' }}/5
          · 疲劳 {{ summary.fatigue || '—' }}/5
        </view>
      </view>

      <view class="card">
        <view class="section-title">这次训练</view>
        <view v-for="(item, index) in summaryItems" :key="index" class="list-row">
          <view class="list-main">
            <text class="list-title">{{ actionLabel(item) }}</text>
            <text class="list-meta">
              完成 {{ item.completed_sets }}/{{ item.target_sets }} 组
              <text v-if="item.replaced_from"> · 替换过</text>
            </text>
          </view>
          <text class="chip">{{ item.status === 'done' ? '完成' : '未完成' }}</text>
        </view>
        <view v-if="finishForm.note" class="muted" style="margin-top: 10px">{{ finishForm.note }}</view>
      </view>

      <view class="btn-row">
        <view class="btn-sm primary" @click="backToToday">回到今天</view>
        <view class="btn-sm" @click="openCoach('根据我最近的完成情况重新生成计划', true)">
          让教练调整下一次
        </view>
      </view>
    </template>

    <view v-else-if="session" class="workout-layout">
      <!-- 当前动作 -->
      <view class="card train-card">
        <view class="row">
          <text class="section-title">
            {{ session.status === 'completed' || session.status === 'finished' ? '训练完成' : '当前动作' }}
          </text>
          <text class="chip">
            {{ session.status === 'completed' || session.status === 'finished'
              ? `${session.items.length} / ${session.items.length}`
              : `${(session.current_index ?? 0) + 1} / ${session.items.length}` }}
          </text>
        </view>
        <view v-if="currentItem" class="train-head">
          <view class="thumb train-thumb">
            <image class="thumb-img" :src="thumbOf(currentItem)" mode="aspectFill" />
          </view>
          <view class="train-titles">
            <text class="train-name">{{ actionLabel(currentItem) }}</text>
            <text class="list-meta">
              {{ slotLabel(currentItem.slot) }} · {{ equipmentNames[currentItem.equipment || 'bodyweight'] || currentItem.equipment || '徒手' }}
              <text v-if="currentItem.replaced_from"> · 已替换</text>
            </text>
            <text class="list-meta">
              第 {{ (currentItem.completed_sets || 0) + 1 }} / {{ currentItem.target_sets }} 组
              <text v-if="currentItem.target_reps"> · 每组 {{ currentItem.target_reps }} 次</text>
            </text>
          </view>
        </view>
        <view v-else class="muted" style="margin-top: 10px">所有动作都完成了，提交反馈即可。</view>

        <view v-if="currentItem?.guidance" class="train-guidance">{{ currentItem.guidance }}</view>

        <view class="train-progress">
          <view class="train-progress-bar">
            <view class="train-progress-fill" :style="{ width: `${progress?.percent || 0}%` }" />
          </view>
          <text class="list-meta">
            {{ progress?.done_sets || 0 }}/{{ progress?.total_sets || 0 }} 组 · 剩余约
            {{ progress?.remaining_minutes || 0 }} 分钟
          </text>
        </view>

        <view v-if="showRest" class="rest-card">
          <text class="rest-label">休息一下</text>
          <text class="rest-time">{{ Math.floor(restRemaining / 60) }}:{{ String(restRemaining % 60).padStart(2, '0') }}</text>
          <view class="rest-actions">
            <view class="btn-sm" @click="adjustRest(-15)">-15s</view>
            <view class="btn-sm" @click="adjustRest(15)">+15s</view>
            <view class="btn-sm primary" @click="stopRest">跳过休息</view>
          </view>
          <text class="list-meta">休息时间是建议值，觉得够了就直接下一组。</text>
        </view>

        <view v-if="currentItem && !showRest && currentItem.slot === 'strength'" class="set-log-row">
          <view class="set-log-field">
            <text class="list-meta">实际次数</text>
            <input v-model.number="setReps" class="input" type="number" placeholder="例如 12" />
          </view>
          <view class="set-log-field">
            <text class="list-meta">重量 kg（可选）</text>
            <input v-model.number="setWeight" class="input" type="number" placeholder="例如 8" />
          </view>
        </view>
        <view v-if="currentItem && !showRest" class="btn-row">
          <view class="btn-sm primary train-set-btn" @click="completeSet">
            {{ busy ? '记录中…' : `完成第 ${(currentItem.completed_sets || 0) + 1} 组` }}
          </view>
        </view>
        <view v-if="currentItem" class="btn-row">
          <view class="btn-sm" @click="skipAction">跳过</view>
          <view class="btn-sm" @click="replaceMenu">换动作</view>
          <view class="btn-sm" @click="openGuidance">动作要领</view>
          <view class="btn-sm" @click="togglePause">
            {{ session.status === 'paused' ? '继续' : '暂停' }}
          </view>
        </view>
        <view class="btn-row">
          <view class="btn-sm primary" @click="openFinish">结束并反馈</view>
          <view class="btn-sm" @click="openCoach('', false)">问教练</view>
        </view>
      </view>

      <!-- 接下来 -->
      <view class="card workout-queue">
        <view class="row">
          <view class="section-title">本场安排</view>
          <text class="list-meta">{{ nextItems.length }} 个待完成</text>
        </view>
        <StableScroll scroll-y chain-to-parent class="workout-queue-scroll" aria-label="本场动作列表">
        <view v-for="item in session.items" :key="item.id" class="list-row train-item" :class="item.status">
          <view class="list-body">
            <view class="thumb train-mini-thumb">
              <image class="thumb-img" :src="thumbOf(item)" mode="aspectFill" />
            </view>
            <view class="list-main">
              <text class="list-title">{{ actionLabel(item) }}</text>
              <text class="list-meta">
                已做 {{ item.completed_sets }}/{{ item.target_sets }} 组 · 还差 {{ Math.max(0,item.target_sets-item.completed_sets) }} 组
                <text v-if="item.replaced_from"> · 从 {{ item.replaced_from }} 替换</text>
              </text>
            </view>
          </view>
          <text class="chip">
            {{ item.status === 'done' ? '完成' : item.status === 'skipped' ? '跳过' : item.status === 'active' ? '进行中' : '待做' }}
          </text>
        </view>
        </StableScroll>
      </view>

      <view v-if="toast" class="notice workout-toast">{{ toast }}</view>
    </view>

    <!-- 结束反馈 -->
    <view v-if="finishOpen">
      <view class="sheet-mask" @click="finishOpen = false" />
      <view class="sheet">
        <view class="row">
          <view class="section-title">这次训练怎么样？</view>
          <text class="link" @click="finishOpen = false">稍后</text>
        </view>
        <view class="finish-choice">
          <view class="chip" :class="{ active: finishForm.completed }" @click="finishForm.completed = true">完成了</view>
          <view class="chip" :class="{ active: !finishForm.completed }" @click="finishForm.completed = false">只完成一部分</view>
        </view>
        <view v-if="finishForm.completed" class="notice">
          <text>按当前安排确认完成，重量可留空：</text>
          <view v-for="item in session?.items || []" :key="item.id">{{ actionLabel(item) }} · {{ item.status==='skipped' ? '已跳过' : `${item.target_sets}组` }} · 约 {{ item.duration_min }} 分钟</view>
          <text>提交后按以上默认值保存；实际不同可改选“只完成一部分”，或在聊天中修改摘要。</text>
        </view>
        <view class="field">
          <text class="label">主观难度 {{ finishForm.difficulty }}/5</text>
          <slider :value="finishForm.difficulty" :min="1" :max="5" show-value @change="setDifficulty" />
        </view>
        <view class="field">
          <text class="label">疲劳程度 {{ finishForm.fatigue }}/5</text>
          <slider :value="finishForm.fatigue" :min="1" :max="5" show-value @change="setFatigue" />
        </view>
        <view class="field">
          <text class="label">实际训练时长（分钟）</text>
          <input v-model="finishForm.actual_minutes" class="input" type="number" />
        </view>
        <view class="field">
          <text class="label">补充一句（可不填）</text>
          <input v-model="finishForm.note" class="input" placeholder="例如：最后两个动作没做完 / 膝盖有点不舒服" />
        </view>
        <view class="btn-row">
          <view class="btn-sm primary" @click="submitFinish">{{ busy ? '保存中…' : '保存这次训练' }}</view>
        </view>
        <text class="muted" style="display: block; margin-top: 8px; text-align: center">
          保存后会生成真实训练记录；连续没完成会影响下一次计划的训练量。
        </text>
      </view>
    </view>

    <GlobalCoachWorkspace
      :visible="coachOpen"
      page="workout"
      :date="date"
      :session-id="sessionId"
      :initial-text="coachText"
      :auto-send="coachAuto"
      :context="{
        currentExercise: currentItem?.name || '',
        currentSet: currentItem?.completed_sets ?? 0,
        targetSets: currentItem?.target_sets ?? 0,
        sessionProgress: progress ? `${progress.done_sets}/${progress.total_sets}` : '',
        remainingMinutes: progress?.remaining_minutes ?? 0,
      }"
      @close="coachOpen = false"
      @changed="reloadFromAgent"
      @open-workout="reloadFromAgent"
      @open-today-brief="onOpenTodayBrief"
      @open-memory="onOpenMemory"
      @open-progress="onOpenProgress"
    />
    <KnowledgeSheet
      :visible="knowledgeOpen"
      :question="knowledgeQuestion"
      @close="knowledgeOpen = false"
    />
  </view>
</template>

<style scoped>
.workout-page {
  max-width: none;
}
.train-card {
  border-color: var(--primary-soft);
}
.train-head {
  align-items: center;
  display: flex;
  gap: 12px;
  margin-top: 10px;
}
.train-thumb {
  height: 64px;
  width: 64px;
}
.train-mini-thumb {
  height: 42px;
  width: 42px;
}
.train-titles {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.train-name {
  font-size: 20px;
  font-weight: 700;
  line-height: 1.3;
}
.train-guidance {
  background: var(--primary-soft);
  border-radius: 10px;
  color: var(--primary-deep);
  font-size: 12px;
  line-height: 1.6;
  margin-top: 10px;
  padding: 8px 10px;
}
.train-progress {
  margin-top: 12px;
}
.train-progress-bar {
  background: var(--line-soft);
  border-radius: 999px;
  height: 8px;
  overflow: hidden;
}
.train-progress-fill {
  background: var(--primary);
  border-radius: 999px;
  height: 100%;
  transition: width 0.25s ease;
}
.train-set-btn {
  flex: 1 1 auto;
  font-size: 16px;
  padding: 14px 18px;
}
.rest-card {
  align-items: center;
  background: var(--primary-soft);
  border-radius: 14px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 12px;
  padding: 14px;
}
.rest-label {
  color: var(--primary-deep);
  font-size: 12px;
  font-weight: 700;
}
.rest-time {
  color: var(--primary-deep);
  font-size: 34px;
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  line-height: 1.1;
}
.rest-actions {
  display: flex;
  gap: 8px;
  margin: 4px 0;
}
.train-item.done .list-title {
  color: var(--muted);
  text-decoration: line-through;
}
.train-item.skipped {
  opacity: 0.55;
}
.finish-choice {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}
.finish-choice .chip {
  background: var(--line-soft);
  color: var(--ink-soft);
  flex: 1;
  text-align: center;
}
.finish-choice .chip.active {
  background: var(--primary-soft);
  color: var(--primary-deep);
  font-weight: 700;
}
.field {
  border-top: 1px solid var(--line-soft);
  margin-top: 12px;
  padding-top: 12px;
}
.label {
  color: var(--muted);
  display: block;
  font-size: 12px;
  margin-bottom: 6px;
}

.workout-header{display:flex;justify-content:space-between;align-items:center;gap:20px;padding:22px;border:1px solid #efdfd1;border-radius:22px;background:linear-gradient(115deg,#fff6e9,#fffdf9)}
.workout-overview{display:flex;flex-direction:column;gap:5px;align-items:flex-end;flex-shrink:0}.workout-overview>text:first-child{font-size:30px;color:var(--primary);font-weight:800}.workout-overview>text:last-child{font-size:11px;color:var(--muted)}
.workout-layout{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(280px,.85fr);gap:18px;align-items:start;margin-top:18px}.workout-layout>.card{margin:0;min-width:0;border-color:#efdfd1;box-shadow:0 5px 18px #7d482805}.workout-toast{grid-column:1/-1}
.train-card{padding:24px}.train-head{gap:18px;margin:20px 0}.train-thumb{height:112px;width:112px;border-radius:22px;flex-shrink:0}.train-name{font-size:26px}.train-titles{gap:7px}.train-card .btn-row{gap:9px;margin-top:14px}.train-set-btn{text-align:center;cursor:pointer;border-radius:16px;box-shadow:0 5px 14px #ec552323}.train-progress{padding:14px;background:#fff8ef;border-radius:14px}.train-progress .list-meta{display:block;margin-top:8px}
.workout-queue-scroll{max-height:490px;padding-right:5px;margin-top:12px}.train-item.active{background:#fff5e9;border-radius:12px;padding:12px;border-color:transparent}.workout-queue .chip{font-size:10px;padding:4px 8px;white-space:nowrap}.train-item .list-body{min-width:0}.train-item .list-main{min-width:0}.train-mini-thumb{flex-shrink:0;width:46px;height:46px}.set-log-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:16px}.set-log-field .input{margin-top:6px}
@media(min-width:1500px){.workout-layout{grid-template-columns:minmax(0,1.35fr) minmax(330px,.85fr)}.train-thumb{width:136px;height:136px}}
@media(max-width:1100px){.workout-layout{grid-template-columns:1fr}.workout-header{padding:16px}.workout-header .title{font-size:24px}.workout-header .subtitle{font-size:12px}.train-card{padding:18px}.train-thumb{width:80px;height:80px}.train-name{font-size:23px}.workout-overview>text:first-child{font-size:24px}.workout-queue-scroll{max-height:340px}}
@media(max-width:520px){.workout-header{align-items:flex-start}.workout-overview{display:none}.train-thumb{width:72px;height:72px}.train-head{gap:13px}.train-name{font-size:21px}.workout-header .eyebrow{font-size:10px}}
</style>
