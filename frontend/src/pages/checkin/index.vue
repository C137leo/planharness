<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { onLoad } from '@dcloudio/uni-app'
import { api, type WorkoutKind, type WorkoutLogItem } from '../../api'
import { actionLabel, actionName } from '../../styles/copy'
import { todayStr } from '../../styles/dates'

const busy = ref(false)
const error = ref('')
const form = reactive({
  date: '',
  weight: '',
  sleep: '',
  hunger: 3,
  soreness: 2,
  adherence: '80',
  note: '',
})

function today(): string {
  return todayStr()
}

function numberOrNull(value: string): number | null {
  if (!value.trim()) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

async function submit() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await api.saveCheckin({
      date: form.date || today(),
      weight_kg: numberOrNull(form.weight),
      sleep_hours: numberOrNull(form.sleep),
      hunger_level: form.hunger,
      soreness_level: form.soreness,
      adherence_pct: numberOrNull(form.adherence),
      note: form.note,
    })
    uni.showToast({ title: '已记录', icon: 'success' })
    setTimeout(() => uni.switchTab({ url: '/pages/today/index' }), 600)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    busy.value = false
  }
}

// ---- 训练记录：热量只能从"真的练了"的记录算，不能拿计划反推 ----

const workout = reactive({
  duration_min: '30',
})
// 多选：一次可以记多个动作（今天计划里的动作通常 3 个，逐个点太麻烦）
const selectedRefs = ref<string[]>([])
const workoutBusy = ref(false)
const workoutError = ref('')
const workoutLogs = ref<WorkoutLogItem[]>([])

type WorkoutOption = { ref: string; label: string; kind: WorkoutKind; duration: number }

// 兜底列表：还没生成计划时也能记一笔
const FALLBACK_OPTIONS: WorkoutOption[] = [
  { ref: 'brisk_walk', label: '快走', kind: 'cardio', duration: 30 },
  { ref: 'easy_walk', label: '低冲击快走', kind: 'cardio', duration: 25 },
  { ref: 'bodyweight_squat', label: '自重深蹲', kind: 'strength', duration: 12 },
  { ref: 'pushup', label: '俯卧撑', kind: 'strength', duration: 12 },
  { ref: 'row', label: '哑铃划船', kind: 'strength', duration: 12 },
  { ref: 'glute_bridge', label: '臀桥', kind: 'strength', duration: 12 },
  { ref: 'stretch', label: '全身拉伸', kind: 'stretch' as WorkoutKind, duration: 15 },
]

const exerciseOptions = ref<WorkoutOption[]>(FALLBACK_OPTIONS)
const planHint = ref('')

/** 打卡内容默认对齐今天的训练：从计划里取今天的动作，而不是让用户凭记忆填。 */
async function loadTodayPlan() {
  try {
    const plan = await api.getCurrentPlan()
    const todayActions = (plan.today?.actions || []).filter(
      (action) => action.action_type === 'exercise',
    )
    if (!todayActions.length) return
    exerciseOptions.value = todayActions.map((action) => ({
      ref: action.action_ref,
      label: actionLabel(action),
      kind: (action.slot === 'cardio' ? 'cardio' : 'strength') as WorkoutKind,
      duration: action.duration_min || 12,
    }))
    // 默认全选今天的动作，用户点一下就记完整场训练
    selectedRefs.value = exerciseOptions.value.map((option) => option.ref)
    workout.duration_min = String(exerciseOptions.value[0].duration)
    planHint.value = `已按今天的计划预填 ${exerciseOptions.value.length} 个动作（默认全选）`
  } catch {
    // 没有计划就保留兜底列表
  }
}

function toggleOption(option: WorkoutOption) {
  const index = selectedRefs.value.indexOf(option.ref)
  if (index >= 0) {
    selectedRefs.value = selectedRefs.value.filter((ref) => ref !== option.ref)
    return
  }
  selectedRefs.value = [...selectedRefs.value, option.ref]
  // 第一个选中的动作决定默认时长
  if (selectedRefs.value.length === 1) workout.duration_min = String(option.duration)
}

const selectedOptions = computed(() =>
  exerciseOptions.value.filter((option) => selectedRefs.value.includes(option.ref)),
)

async function loadWorkouts() {
  try {
    const data = await api.listWorkoutLogs(7)
    workoutLogs.value = data.items
  } catch {
    // 进展页会再拉一次，这里失败不挡住打卡主表单。
  }
}

async function logWorkout() {
  if (workoutBusy.value) return
  const options = selectedOptions.value
  if (!options.length) {
    workoutError.value = '先选至少一个动作'
    return
  }
  const duration = Number(workout.duration_min)
  if (!Number.isFinite(duration) || duration <= 0) {
    workoutError.value = '时长要填一个大于 0 的分钟数'
    return
  }
  workoutBusy.value = true
  workoutError.value = ''
  try {
    const created: WorkoutLogItem[] = []
    for (const option of options) {
      created.push(
        await api.logWorkout({
          date: form.date || today(),
          action_ref: option.ref,
          kind: option.kind,
          // 每个动作用计划里的时长，用户手填的时长作为统一覆盖
          duration_min: Math.round(Number(workout.duration_min) || option.duration),
        }),
      )
    }
    workoutLogs.value = [...created, ...workoutLogs.value]
    const kcal = created.reduce((sum, item) => sum + item.kcal, 0)
    uni.showToast({ title: `记下 ${created.length} 项 · ${Math.round(kcal)} kcal`, icon: 'none' })
  } catch (err) {
    workoutError.value = err instanceof Error ? err.message : '记录失败'
  } finally {
    workoutBusy.value = false
  }
}

async function removeWorkout(logId: string) {
  try {
    await api.deleteWorkoutLog(logId)
    workoutLogs.value = workoutLogs.value.filter((item) => item.log_id !== logId)
  } catch (err) {
    workoutError.value = err instanceof Error ? err.message : '删除失败'
  }
}

onLoad(() => {
  form.date = today()
  loadWorkouts()
  loadTodayPlan()
})
</script>

<template>
  <view class="page">
    <view class="eyebrow">CHECK-IN</view>
    <view class="title">今天感觉怎么样？</view>
    <view class="subtitle">数据不用完美，真实比完美有用。</view>

    <view v-if="error" class="error">{{ error }}</view>

    <view class="card">
      <view class="field">
        <text class="label">日期</text>
        <input v-model="form.date" class="input" placeholder="YYYY-MM-DD" />
      </view>
      <view class="field">
        <text class="label">体重（kg）</text>
        <input v-model="form.weight" class="input" type="digit" placeholder="例如 77.4" />
      </view>
      <view class="field">
        <text class="label">睡眠（小时）</text>
        <input v-model="form.sleep" class="input" type="digit" placeholder="例如 7.5" />
      </view>
      <view class="field">
        <text class="label">饥饿感 {{ form.hunger }}/5</text>
        <slider :value="form.hunger" :min="1" :max="5" show-value @change="(e) => (form.hunger = Number(e.detail.value))" />
      </view>
      <view class="field">
        <text class="label">酸痛 {{ form.soreness }}/5</text>
        <slider :value="form.soreness" :min="1" :max="5" show-value @change="(e) => (form.soreness = Number(e.detail.value))" />
      </view>
      <view class="field">
        <text class="label">执行率（%）</text>
        <input v-model="form.adherence" class="input" type="number" placeholder="0-100" />
      </view>
      <view class="field">
        <text class="label">备注</text>
        <input v-model="form.note" class="input" placeholder="膝盖感觉、加班、聚餐…" />
      </view>
    </view>

    <view class="card">
      <view class="row">
        <view class="section-title">记一条训练</view>
        <view class="chip">只算消耗</view>
      </view>
      <view class="muted" style="margin-top: 6px">
        勾选今天练的动作（可多选），点一下就把这一场都记下来；热量按 MET × 3.5 × 体重 ÷ 200 × 时长 估算。
      </view>

      <view class="option-grid">
        <view
          v-for="option in exerciseOptions"
          :key="option.ref"
          class="chip option"
          :class="{ active: selectedRefs.includes(option.ref) }"
          @click="toggleOption(option)"
        >
          {{ option.label }}
        </view>
      </view>
      <view class="list-meta" style="margin-top: 8px">
        已选 {{ selectedRefs.length }} 个 · 类型自动按计划带（力量/有氧/拉伸）
      </view>

      <input v-model="workout.duration_min" class="input" placeholder="时长（分钟）" />

      <view v-if="workoutError" class="error" style="margin-top: 10px">{{ workoutError }}</view>
      <view class="btn-row">
        <view class="btn-sm primary" @click="logWorkout">
          {{ workoutBusy ? '记录中…' : `记下选中的 ${selectedRefs.length} 个` }}
        </view>
      </view>

      <view v-for="log in workoutLogs.slice(0, 5)" :key="log.log_id" class="log">
        <view class="log-main">
          <text class="log-name">{{ actionName(log.action_ref) }}</text>
          <text class="log-meta">
            {{ log.date }} · {{ log.duration_min }} 分钟 · {{ log.kcal }} kcal
          </text>
        </view>
        <text class="log-del" @click="removeWorkout(log.log_id)">删除</text>
      </view>
    </view>

    <view class="btn-row">
      <view class="btn-sm primary" @click="submit">{{ busy ? '保存中…' : '保存今日打卡' }}</view>
    </view>
  </view>
</template>

<style>
.field {
  border-bottom: 1px solid var(--line-soft);
  padding: 12px 0;
}
.field:last-child {
  border-bottom: none;
}
.label {
  color: var(--muted);
  display: block;
  font-size: 13px;
  margin-bottom: 8px;
}
/* 尺寸（高度/内边距）统一由 theme.css 的 .input 管，页面只调整间距 */
.input {
  margin-top: 8px;
}
</style>

<style>
.option-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}
.option {
  background: var(--line-soft);
  border: 1px solid transparent;
  color: var(--ink-soft);
}
.option.active {
  background: var(--primary-soft);
  border-color: var(--primary);
  color: var(--primary);
  font-weight: 600;
}
.log {
  align-items: center;
  border-top: 1px solid var(--line-soft);
  display: flex;
  justify-content: space-between;
  margin-top: 10px;
  padding-top: 10px;
}
.log-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.log-name {
  font-size: 14px;
  font-weight: 600;
}
.log-meta {
  color: var(--muted);
  font-size: 12px;
}
.log-del {
  color: var(--accent);
  font-size: 12px;
}
</style>
