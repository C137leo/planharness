<script setup lang="ts">
/**
 * 计划：整周安排、待确认的改动、以及「撤销上一次改动」。
 *
 * 撤销不是删历史：它生成一个新版本，内容回到上一个版本。
 * 所以撤销本身也能被审计和再撤销。
 */
import { computed, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, type CurrentPlan, type PlanAction, type PlanPatchSummary, type PlanVersionHistory } from '../../api'
import PatchComposer from '../../components/PatchComposer.vue'
import PlanDraftSheet from '../../components/PlanDraftSheet.vue'
import { actionLabel, describeOperation, slotLabel, sortMeals } from '../../styles/copy'
import { thumbOf } from '../../styles/thumbs'
import { exerciseSearchTerm, openExerciseDbTerm } from '../../styles/exerciseDb'
import { WEEKDAY_LABELS, isoWeekday, todayStr } from '../../styles/dates'

const plan = ref<CurrentPlan | null>(null)
const pending = ref<PlanPatchSummary[]>([])
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const toast = ref('')
const versions = ref<PlanVersionHistory | null>(null)
const historyOpen = ref(false)
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
    // 只取真正等待确认的：已拒绝/已提交的补丁留在这里，点确认会 422
    const patches = await api.listPatches(20, 'pending_approval')
    pending.value = patches.filter((item) => item.status === 'pending_approval')
    // 版本链：撤销/回滚前先让用户看到"现在在哪一版、之前改过什么"
    if (plan.value) {
      try {
        versions.value = await api.planVersions(plan.value.plan_id, 8)
      } catch {
        versions.value = null
      }
    }
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

/** 回到某个历史版本：不是删历史，而是用旧内容再提交一个新版本 */
async function rollbackTo(version: number) {
  if (!plan.value || busy.value) return
  if (version === plan.value.version) return
  busy.value = true
  error.value = ''
  try {
    const result = await api.rollbackPlan(plan.value.plan_id, version)
    toast.value = `已回到 v${version} 的内容，记为 v${result.new_version}`
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '回滚失败'
  } finally {
    busy.value = false
  }
}

function versionLabel(row: PlanVersionHistory['versions'][number]) {
  const changes = row.changes
  if (!changes) return row.patch?.reason_text || '初始版本'
  const parts = [`改 ${changes.changed_slots}/${changes.total_slots} 个动作`]
  const meals = (changes as unknown as { meals_changed?: number }).meals_changed
  if (meals) parts.push(`${meals} 餐`)
  if (row.patch?.reason_text) parts.push(row.patch.reason_text)
  return parts.join(' · ')
}

async function onApplied() {
  composerOpen.value = false
  await load()
}

async function onConfirmed() {
  draftOpen.value = false
  await load()
}

// 日历：未来 14 天，每天取"同星期几"的动作与餐食（计划是周计划）
const selectedDate = ref(todayStr())
const calendarDays = computed(() => {
  const actions = plan.value?.actions || []
  const meals = plan.value?.meals || []
  const today = new Date()
  const rows = []
  for (let offset = 0; offset < 14; offset += 1) {
    const date = new Date(today.getFullYear(), today.getMonth(), today.getDate() + offset)
    const iso = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
    const weekday = isoWeekday(date)
    rows.push({
      date: iso,
      weekday,
      label: `${date.getMonth() + 1}/${date.getDate()}`,
      week: WEEKDAY_LABELS[weekday - 1],
      isToday: iso === todayStr(),
      actions: actions.filter((item) => item.day_index === weekday),
      meals: sortMeals(meals.filter((meal) => meal.day_index === weekday)),
    })
  }
  return rows
})

const selectedDay = computed(
  () => calendarDays.value.find((day) => day.date === selectedDate.value) || calendarDays.value[0],
)

function pickDate(date: string) {
  selectedDate.value = date
}

/** 当天餐食总热量：直接按餐次累加，不在前端重复算营养。 */
function dayMealKcal(day: { meals: Array<{ kcal: number }> }): number {
  return day.meals.reduce((sum, meal) => sum + (meal.kcal || 0), 0)
}

function exerciseDbTermForAction(action: PlanAction): string {
  return exerciseSearchTerm(actionLabel(action))
}

function openActionExerciseDb(action: PlanAction) {
  openExerciseDbTerm(exerciseDbTermForAction(action))
}

/** 待确认补丁的改动数：旧后端没有 operations 字段，不能直接取 .length。 */
function operationCount(patch: PlanPatchSummary): number {
  return patch.operations?.length ?? patch.diff?.changed_slots ?? 0
}

/** 计划页直接处理「待确认改动」：之前只能看、没法确认，等于把用户困住。 */
async function approvePending(patch: PlanPatchSummary) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const result = await api.approvePatch(patch.id, '计划页确认')
    toast.value = `已应用改动，现在是 v${result.committed_version}`
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '应用失败'
  } finally {
    busy.value = false
  }
}

async function rejectPending(patch: PlanPatchSummary) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await api.rejectPatch(patch.id, '计划页丢弃')
    toast.value = '已丢弃这条改动'
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '取消失败'
  } finally {
    busy.value = false
  }
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
        <view class="row">
          <view class="section-title">待确认的改动</view>
          <text class="link" @click="openToday">再提一个 →</text>
        </view>
        <view v-for="patch in pending" :key="patch.id" class="pending">
          <view class="pending-main">
            <text class="pending-text">{{ patch.reason_text || patch.reason_code }}</text>
            <text class="pending-meta">
              {{ operationCount(patch) }} 处 · 基于 v{{ patch.base_version }}（确认后才生效）
            </text>
            <view v-for="(op, index) in (patch.operations || []).slice(0, 4)" :key="index" class="pending-op">
              <text class="list-meta">· {{ describeOperation(op) }}</text>
            </view>
            <view v-if="operationCount(patch) > 4" class="pending-op">
              <text class="list-meta">· 还有 {{ operationCount(patch) - 4 }} 处…</text>
            </view>
          </view>
          <view class="pending-actions">
            <view class="btn-sm primary" @click="approvePending(patch)">确认应用</view>
            <view class="btn-sm" @click="rejectPending(patch)">不要了</view>
          </view>
        </view>
      </view>

      <view class="card">
        <view class="row">
          <view class="section-title">日历 · 未来 14 天</view>
          <text class="link" @click="openToday">今天页 →</text>
        </view>
        <view class="cal-row">
          <view
            v-for="day in calendarDays"
            :key="day.date"
            class="cal-day"
            :class="{ active: day.date === selectedDate, today: day.isToday }"
            @click="pickDate(day.date)"
          >
            <text class="cal-week">{{ day.week }}</text>
            <text class="cal-date">{{ day.label }}</text>
            <text class="cal-dot">{{ day.actions.length ? day.actions.length + ' 练' : '休' }}</text>
          </view>
        </view>

        <view v-if="selectedDay" class="cal-detail">
          <view class="row">
            <text class="list-title">{{ selectedDay.date }} · {{ selectedDay.week }}</text>
            <text class="list-meta">{{ selectedDay.actions.length }} 个动作</text>
          </view>

          <view v-if="!selectedDay.actions.length" class="muted" style="margin-top: 8px">
            这天没有安排训练。
          </view>
          <view v-for="action in selectedDay.actions" :key="action.id" class="list-row">
            <view class="list-body">
              <view class="thumb">
                <image class="thumb-img" :src="thumbOf(action)" mode="aspectFill" />
              </view>
              <view class="list-main">
                <text class="list-title">{{ actionLabel(action) }}</text>
                <text class="list-meta">
                  {{ slotLabel(action.slot) }}
                  <template v-if="action.sets && action.reps"> · {{ action.sets }}×{{ action.reps }}</template>
                  <template v-else-if="action.sets"> · {{ action.sets }} 组</template>
                  <template v-if="action.duration_min"> · {{ action.duration_min }} 分钟</template>
                </text>
              </view>
            </view>
            <text
              v-if="exerciseDbTermForAction(action)"
              class="link exercise-db-link"
              @click.stop="openActionExerciseDb(action)"
            >
              动作库 · {{ exerciseDbTermForAction(action) }} →
            </text>
          </view>

          <view v-if="selectedDay.meals.length" class="cal-meals">
            <view class="row" style="margin-top: 10px">
              <text class="section-title">这天吃</text>
              <text class="list-meta">{{ dayMealKcal(selectedDay) }} kcal</text>
            </view>
            <view v-for="meal in selectedDay.meals" :key="meal.slot" class="meal-block">
              <view class="row">
                <text class="line-name">{{ meal.title }}</text>
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
          <view v-else-if="selectedDay.actions.length" class="muted" style="margin-top: 8px">
            这天没有餐食记录。
          </view>
        </view>
      </view>

      <view v-for="group in grouped" :key="group.day" class="card">
        <view class="row">
          <view class="day-title">{{ WEEKDAY_LABELS[group.day - 1] || `Day ${group.day}` }}</view>
          <view class="chip">{{ group.actions.length }}</view>
        </view>
        <view v-for="action in group.actions" :key="action.id" class="list-row">
          <view class="list-body">
            <view class="thumb">
              <image class="thumb-img" :src="thumbOf(action)" mode="aspectFill" />
            </view>
            <view class="list-main">
              <text class="list-title">{{ actionLabel(action) }}</text>
              <text class="list-meta">
                {{ slotLabel(action.slot) }}
                <template v-if="action.sets"> · {{ action.sets }}×{{ action.reps }}</template>
                <template v-if="action.duration_min"> · {{ action.duration_min }} 分钟</template>
              </text>
            </view>
          </view>
          <text
            v-if="exerciseDbTermForAction(action)"
            class="link exercise-db-link"
            @click.stop="openActionExerciseDb(action)"
          >
            动作库 · {{ exerciseDbTermForAction(action) }} →
          </text>
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

      <view v-if="versions" class="card">
        <view class="row" @click="historyOpen = !historyOpen">
          <view class="section-title">版本记录</view>
          <text class="link">
            共 {{ versions.total }} 版 · 当前 v{{ versions.current_version }}
            {{ historyOpen ? '收起' : '查看 →' }}
          </text>
        </view>
        <view v-if="historyOpen" class="version-list">
          <view v-for="row in versions.versions" :key="row.version" class="version-row">
            <view class="list-main">
              <text class="list-title">
                v{{ row.version }}
                <text v-if="row.is_current" class="version-now">当前</text>
              </text>
              <text class="list-meta">{{ versionLabel(row) }}</text>
              <text class="list-meta">来自 v{{ row.parent_version ?? '—' }} · {{ row.status }}</text>
            </view>
            <view class="btn-sm" v-if="!row.is_current" @click="rollbackTo(row.version)">
              回到这一版
            </view>
          </view>
        </view>
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
.exercise-db-link {
  flex: 0 0 auto;
  font-weight: 600;
  max-width: 46%;
  text-align: right;
}
.version-list {
  margin-top: 10px;
}

.version-row {
  align-items: center;
  border-top: 1px dashed var(--line-soft);
  display: flex;
  gap: 10px;
  justify-content: space-between;
  padding: 10px 0;
}

.version-now {
  background: var(--primary-soft);
  border-radius: 999px;
  color: var(--primary-deep);
  font-size: 10px;
  font-weight: 700;
  padding: 1px 6px;
}

.pending {
  border-top: 1px solid var(--line-soft);
  display: flex;
  gap: 12px;
  justify-content: space-between;
  margin-top: 10px;
  padding-top: 10px;
}

.pending-main {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

/* 按钮放右侧一列：卡片之间更容易对齐，点的时候手指也不会盖住文字 */
.pending-actions {
  align-items: stretch;
  display: flex;
  flex: 0 0 auto;
  flex-direction: column;
  gap: 8px;
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
.pending-op {
  margin-top: 4px;
}
.cal-row {
  display: flex;
  gap: 8px;
  margin-top: 12px;
  overflow-x: auto;
  padding-bottom: 4px;
}
.cal-day {
  background: var(--line-soft);
  border: 1px solid transparent;
  border-radius: 14px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  flex: 0 0 auto;
  padding: 8px 10px;
  min-width: 56px;
}
.cal-day.today {
  border-color: var(--primary);
}
.cal-day.active {
  background: var(--primary);
  color: #fff;
}
.cal-day.active .cal-week,
.cal-day.active .cal-date,
.cal-day.active .cal-dot {
  color: #fff;
}
.cal-week,
.cal-date {
  color: var(--ink-soft);
  font-size: 12px;
}
.cal-dot {
  color: var(--muted);
  font-size: 11px;
}
.cal-detail {
  border-top: 1px solid var(--line-soft);
  margin-top: 12px;
  padding-top: 10px;
}
.cal-meals {
  margin-top: 6px;
}
.line {
  align-items: center;
  border-top: 1px solid var(--line-soft);
  display: flex;
  justify-content: space-between;
  padding: 8px 0;
}
.line-name {
  font-size: 14px;
  font-weight: 600;
}
.line-meta {
  color: var(--muted);
  font-size: 12px;
}
.meal-block {
  border-top: 1px solid var(--line-soft);
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 10px 0;
}
</style>
