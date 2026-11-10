<script setup lang="ts">
/**
 * 我的：把"计划从哪来"摆明白。
 *
 * 这里录的每个字段都会真的影响生成结果：目标 → skill 的训练结构，
 * 体型 → BMR/TDEE，器械 → 动作降级，过敏原 → 食谱排除 + 安全门。
 */
import { computed, reactive, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, type Me, type MemoryItem, type Profile, type TemporaryContextItem } from '../../api'
import GlobalCoachWorkspace from '../../components/GlobalCoachWorkspace.vue'
import DesktopSidebar from '../../components/DesktopSidebar.vue'
import { syncDesktopChrome } from '../../composables/desktopChrome'
import { resetCoachWorkspace } from '../../composables/coachWorkspace'

const loading = ref(true)
const busy = ref(false)
const error = ref('')
const toast = ref('')
const me = ref<Me | null>(null)
const saved = ref(false)
const coachOpen = ref(false)
const coachText = ref('')
const coachAuto = ref(false)
const memories = ref<MemoryItem[]>([])
const temporaryItems = ref<TemporaryContextItem[]>([])
const memoryBusy = ref(false)
const editingId = ref('')
const editingValue = ref('')

const form = reactive({
  sex: 'male' as 'male' | 'female',
  birth_year: 1997,
  height_cm: 178,
  weight_kg: 78,
  goal: 'fat_loss' as 'fat_loss' | 'muscle_gain' | 'cardio' | 'health',
  goal_note: '',
  weekly_days: 3,
  session_minutes: 40,
  activity_level: 'moderate' as 'sedentary' | 'light' | 'moderate' | 'active',
  training_experience: 'beginner' as 'beginner' | 'intermediate' | 'advanced',
  equipment: [] as string[],
  allergiesText: '',
  dietText: '',
  likesText: '',
  preferred_time_label: '',
  preferred_time_window: '',
  preferred_location: '',
})

const GOALS = [
  { key: 'fat_loss', label: '减脂' },
  { key: 'muscle_gain', label: '增肌' },
  { key: 'cardio', label: '心肺' },
  { key: 'health', label: '综合健康' },
] as const

const EQUIPMENT = [
  { key: 'dumbbell', label: '哑铃' },
  { key: 'band', label: '弹力带' },
  { key: 'bench', label: '训练凳' },
  { key: 'treadmill', label: '跑步机' },
  { key: 'mat', label: '瑜伽垫' },
] as const

const TIME_OPTIONS = [
  { label: '午休', window: '12:00-13:00' },
  { label: '早上', window: '07:00-08:00' },
  { label: '晚上', window: '19:00-20:00' },
  { label: '不限', window: '' },
] as const

const LOCATION_OPTIONS = [
  { key: 'home', label: '家里' },
  { key: 'gym', label: '健身房' },
  { key: 'office', label: '办公室' },
  { key: 'outdoor', label: '户外' },
] as const

const ready = computed(
  () => form.height_cm >= 120 && form.weight_kg >= 30 && form.birth_year >= 1940,
)

function applyProfile(profile: Profile) {
  form.sex = (profile.sex as 'male' | 'female') || 'male'
  form.birth_year = profile.birth_year || 1997
  form.height_cm = profile.height_cm || 178
  form.weight_kg = profile.weight_kg || 78
  form.goal = (profile.goal as typeof form.goal) || 'fat_loss'
  form.goal_note = profile.goal_note || ''
  form.weekly_days = profile.weekly_days || 3
  form.session_minutes = profile.session_minutes || 40
  form.activity_level = (profile.activity_level as typeof form.activity_level) || 'moderate'
  form.training_experience = (profile.training_experience as typeof form.training_experience) || 'beginner'
  form.equipment = [...(profile.equipment || [])]
  form.allergiesText = (profile.allergies || []).join('、')
  form.dietText = (profile.diet_preferences || []).join('、')
  form.likesText = (profile.likes || []).join('、')
  form.preferred_time_label = profile.preferred_time_label || ''
  form.preferred_time_window = profile.preferred_time_window || ''
  form.preferred_location = profile.preferred_location || ''
}

async function load() {
  // 只在第一次进入时显示骨架屏；后续 onShow 不再把表单卸载重建，
  // 否则 textarea 的 resize-sensor 会在页面激活钩子里拿到空的 rootRef
  if (!me.value) loading.value = true
  error.value = ''
  try {
    me.value = await api.me()
    applyProfile(await api.getProfile())
    await loadMemories()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function pickTime(option: { label: string; window: string }) {
  form.preferred_time_label = option.label === '不限' ? '' : option.label
  form.preferred_time_window = option.window
}

function pickLocation(key: string) {
  form.preferred_location = form.preferred_location === key ? '' : key
}

function toggleEquipment(key: string) {
  form.equipment = form.equipment.includes(key)
    ? form.equipment.filter((item) => item !== key)
    : [...form.equipment, key]
}

function splitList(text: string): string[] {
  return text
    .split(/[、,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

async function save(): Promise<boolean> {
  if (busy.value || !ready.value) return false
  busy.value = true
  error.value = ''
  toast.value = ''
  try {
    const profile = await api.updateProfile({
      sex: form.sex,
      birth_year: form.birth_year,
      height_cm: form.height_cm,
      weight_kg: form.weight_kg,
      goal: form.goal,
      goal_note: form.goal_note,
      weekly_days: form.weekly_days,
      session_minutes: form.session_minutes,
      activity_level: form.activity_level,
      training_experience: form.training_experience,
      equipment: form.equipment,
      allergies: splitList(form.allergiesText),
      diet_preferences: splitList(form.dietText),
      likes: splitList(form.likesText),
      preferred_time_label: form.preferred_time_label,
      preferred_time_window: form.preferred_time_window,
      preferred_location: form.preferred_location,
    })
    applyProfile(profile)
    saved.value = true
    await loadMemories()
    toast.value = '已保存，档案和记忆已同步'
    return true
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存失败'
    return false
  } finally {
    busy.value = false
  }
}

async function saveThenDraft() {
  if (await save()) openCoach('根据我的资料生成计划', true)
}

function openCoach(text = '', auto = false) {
  coachText.value = text
  coachAuto.value = auto
  coachOpen.value = true
}

async function loadMemories() {
  try {
    const data = await api.listMemories()
    memories.value = data.items
    temporaryItems.value = data.temporary || []
  } catch {
    memories.value = []
    temporaryItems.value = []
  }
}

const longTermMemories = computed(() => memories.value.filter((item) => item.kind === 'long_term'))
const shortTermMemories = computed(() => memories.value.filter((item) => item.kind === 'short_term'))

function temporaryText(item: TemporaryContextItem): string {
  if (item.text) return item.text
  const value = item.value || {}
  if (value.minutes) return `${value.minutes} 分钟`
  if (value.window) return `${value.label || ''} ${value.window}`.trim()
  if (value.location) return `训练地点：${value.location}`
  if (value.busy) return '器械被占用'
  if (Array.isArray(value.regions)) return `不适：${value.regions.join('、')}`
  return item.label
}

function startEdit(memory: MemoryItem) {
  editingId.value = memory.id
  editingValue.value = memory.value
}

function cancelEdit() {
  editingId.value = ''
  editingValue.value = ''
}

async function saveEdit(memory: MemoryItem) {
  if (memoryBusy.value || !editingValue.value.trim()) return
  memoryBusy.value = true
  try {
    const updated = await api.updateMemory(memory.id, { value: editingValue.value.trim() })
    memories.value = memories.value.map((item) => item.id === updated.id ? updated : item)
    editingId.value = ''
    editingValue.value = ''
    if (updated.canonical) applyProfile(await api.getProfile())
    uni.showToast({ title: '已更新记忆', icon: 'none' })
  } catch (err) {
    uni.showToast({ title: err instanceof Error ? err.message : '更新失败', icon: 'none' })
  } finally {
    memoryBusy.value = false
  }
}

function removeMemory(memory: MemoryItem) {
  uni.showModal({
    title: '删除这条记忆？',
    content: memory.value,
    success: async (result) => {
      if (!result.confirm) return
      memoryBusy.value = true
      try {
        await api.deleteMemory(memory.id)
        memories.value = memories.value.filter((item) => item.id !== memory.id)
        if (memory.canonical) applyProfile(await api.getProfile())
        uni.showToast({ title: '已删除', icon: 'none' })
      } catch (err) {
        uni.showToast({ title: err instanceof Error ? err.message : '删除失败', icon: 'none' })
      } finally {
        memoryBusy.value = false
      }
    },
  })
}

async function onAgentChanged() {
  await loadMemories()
}

function onOpenWorkout(payload: { sessionId?: string; date?: string }) {
  const date = payload.date || ''
  const suffix = payload.sessionId ? `&session_id=${payload.sessionId}` : ''
  uni.navigateTo({ url: `/pages/workout/index?date=${date}${suffix}` })
}

function onOpenTodayBrief() {
  uni.switchTab({ url: '/pages/today/index' })
}

function onOpenMemory() {
  // 已经在记忆页，滚到卡片即可
  uni.pageScrollTo({ scrollTop: 380, duration: 200 })
}

function onOpenProgress() {
  uni.switchTab({ url: '/pages/progress/index' })
}

function logout() {
  api.logout()
  resetCoachWorkspace()
  uni.reLaunch({ url: '/pages/login/index' })
}

function openConsole() {
  uni.navigateTo({ url: '/pages/console/index' })
}

onShow(() => {
  syncDesktopChrome()
  load()
})
</script>

<template>
  <view class="page profile-page with-sidebar">
    <DesktopSidebar active="profile" />
    <view class="profile-head">
      <view class="profile-avatar">{{ (me?.nickname || 'L').slice(0, 1) }}</view>
      <view class="profile-head-copy">
        <view class="eyebrow">WORKOUT PLAN HARNESS · 我的档案</view>
        <view class="title">{{ me?.nickname || '你的' }} 的健身档案</view>
        <view class="subtitle">坚持运动，遇见更好的自己。这里的事实会直接影响计划与教练记忆。</view>
      </view>
      <view class="profile-head-badge">
        <text class="profile-head-value">{{ form.weekly_days }} 天</text>
        <text class="profile-head-label">每周训练</text>
      </view>
    </view>

    <view v-if="loading" class="card muted">正在加载…</view>
    <view v-else>
      <view class="btn-row profile-actions">
        <view class="btn-sm primary" @click="openCoach('根据我的档案和记忆，帮我看看下一步该怎么调整', true)">
          和教练聊聊我的档案
        </view>
        <view class="btn-sm" @click="openCoach('你记得我什么？', true)">教练记忆里有什么</view>
      </view>
      <view v-if="toast" class="notice" style="margin-top: 14px">{{ toast }}</view>
      <view v-if="error" class="error" style="margin-top: 14px">{{ error }}</view>

      <view class="profile-layout">
        <view class="profile-main">
      <view class="card">
        <view class="row">
          <view class="section-title">账号</view>
          <text class="link" @click="logout">退出登录</text>
        </view>
        <view class="list" v-if="me">
          <view class="list-row">
            <text class="list-title">{{ me.nickname || '未命名' }}</text>
            <text class="list-meta">{{ me.phone }}</text>
          </view>
        </view>
        <view class="muted" style="margin-top: 8px">
          账号只用于登录；计划、打卡、审计都按这个账号隔离。
        </view>
      </view>

      <view class="card">
        <view class="section-title">目标</view>
        <view class="chip-grid">
          <view
            v-for="goal in GOALS"
            :key="goal.key"
            class="chip option"
            :class="{ active: form.goal === goal.key }"
            @click="form.goal = goal.key"
          >
            {{ goal.label }}
          </view>
        </view>
        <textarea
          v-model="form.goal_note"
          class="input"
          maxlength="200"
          placeholder="用一句话说清目标，例如：3 个月瘦 8 公斤，不想每天称重"
        />
      </view>

      <view class="card">
        <view class="row">
          <view class="section-title">训练偏好（档案）</view>
          <text class="list-meta">长期事实 · 会同步 Agent 记忆</text>
        </view>
        <view class="muted" style="margin-top: 6px">
          这里修改的是长期偏好；「今天只有 20 分钟」这类临时要求不会改到这里。
        </view>
        <view class="field">
          <text class="label">通常训练时间</text>
          <view class="chip-grid" style="margin-top: 0">
            <view
              v-for="option in TIME_OPTIONS"
              :key="option.window || 'none'"
              class="chip option"
              :class="{ active: form.preferred_time_window === option.window }"
              @click="pickTime(option)"
            >
              {{ option.label }}
            </view>
          </view>
        </view>
        <view class="field">
          <text class="label">通常训练地点</text>
          <view class="chip-grid" style="margin-top: 0">
            <view
              v-for="option in LOCATION_OPTIONS"
              :key="option.key"
              class="chip option"
              :class="{ active: form.preferred_location === option.key }"
              @click="pickLocation(option.key)"
            >
              {{ option.label }}
            </view>
          </view>
        </view>
        <view class="field">
          <text class="label">喜欢的动作</text>
          <input v-model="form.likesText" class="input" placeholder="例如：划船、臀桥" />
        </view>
        <text class="list-meta">
          不喜欢/不能做的动作仍由教练对话记录；修改档案后 Agent 记忆会同步成 canonical 镜像。
        </text>
      </view>

        </view>
        <view class="profile-side">
      <view class="card memory-card">
        <view class="row">
          <view class="section-title">我的健身记忆</view>
          <text class="link" @click="openCoach('你记得我什么？', true)">问教练 →</text>
        </view>
        <view class="muted" style="margin-top: 6px">
          这里是 Agent 真正记住的东西。带「档案」标签的条目以 Profile 为唯一事实来源，
          修改后会同步档案；临时要求（今天只有 20 分钟）不会写进长期记忆。
        </view>

        <view v-if="!memories.length && !temporaryItems.length" class="muted" style="margin-top: 10px">
          还没有记忆。直接对教练说“以后训练安排在午休”“我不喜欢深蹲”就会写进来。
        </view>

        <view v-if="longTermMemories.length" class="memory-group">
          <text class="memory-group-title">长期记忆</text>
          <view v-for="memory in longTermMemories" :key="memory.id" class="memory-row">
            <view class="memory-main">
              <view class="memory-head">
                <text class="memory-tag">{{ memory.category_label }}</text>
                <text v-if="memory.canonical" class="memory-source">档案</text>
                <text v-else-if="memory.source === 'user_edit'" class="memory-source">手动编辑</text>
                <text v-else-if="memory.source === 'derived'" class="memory-source">行为总结</text>
              </view>
              <template v-if="editingId === memory.id">
                <input v-model="editingValue" class="input memory-input" maxlength="200" />
                <view class="btn-row">
                  <view class="btn-sm primary" @click="saveEdit(memory)">{{ memoryBusy ? '保存中…' : '保存' }}</view>
                  <view class="btn-sm" @click="cancelEdit">取消</view>
                </view>
              </template>
              <template v-else>
                <text class="memory-value">{{ memory.value }}</text>
                <view class="memory-actions">
                  <text class="link" @click="startEdit(memory)">编辑</text>
                  <text class="memory-delete" @click="removeMemory(memory)">删除</text>
                </view>
              </template>
            </view>
          </view>
        </view>

        <view v-if="shortTermMemories.length" class="memory-group">
          <text class="memory-group-title">近期状态</text>
          <view v-for="memory in shortTermMemories" :key="memory.id" class="memory-row">
            <view class="memory-main">
              <view class="memory-head">
                <text class="memory-tag short">{{ memory.category_label }}</text>
                <text class="memory-source">
                  {{ memory.source }}<text v-if="memory.expires_at"> · 到期归档</text>
                </text>
              </view>
              <text class="memory-value">{{ memory.value }}</text>
              <view class="memory-actions">
                <text class="memory-delete" @click="removeMemory(memory)">删除</text>
              </view>
            </view>
          </view>
        </view>

        <view v-if="temporaryItems.length" class="memory-group">
          <text class="memory-group-title">临时要求 · 仅今天有效</text>
          <view v-for="item in temporaryItems" :key="item.id" class="memory-row temporary-row">
            <view class="memory-main">
              <view class="memory-head">
                <text class="memory-tag temporary">{{ item.label }}</text>
                <text class="memory-source">{{ item.status === 'completed' ? '已结束' : '今天有效' }}</text>
              </view>
              <text class="memory-value">{{ temporaryText(item) }}</text>
              <text class="memory-source">
                生效 {{ item.effective_date }}<text v-if="item.expires_at"> · 到期自动失效</text>
              </text>
            </view>
          </view>
        </view>
      </view>


      <view class="card">
        <view class="section-title">基础信息</view>
        <view class="field">
          <text class="label">性别</text>
          <view class="chip-grid" style="margin-top: 0">
            <view class="chip option" :class="{ active: form.sex === 'male' }" @click="form.sex = 'male'">男</view>
            <view class="chip option" :class="{ active: form.sex === 'female' }" @click="form.sex = 'female'">女</view>
          </view>
        </view>
        <view class="field">
          <text class="label">出生年</text>
          <input v-model.number="form.birth_year" class="input" type="number" />
        </view>
        <view class="field">
          <text class="label">身高（cm）</text>
          <input v-model.number="form.height_cm" class="input" type="number" />
        </view>
        <view class="field">
          <text class="label">体重（kg）</text>
          <input v-model.number="form.weight_kg" class="input" type="number" />
        </view>
        <view class="field">
          <text class="label">每周可练天数</text>
          <input v-model.number="form.weekly_days" class="input" type="number" />
        </view>
        <view class="field">
          <text class="label">每次时长（分钟）</text>
          <input v-model.number="form.session_minutes" class="input" type="number" />
        </view>
      </view>

      <view class="card">
        <view class="section-title">器械</view>
        <view class="chip-grid">
          <view
            v-for="item in EQUIPMENT"
            :key="item.key"
            class="chip option"
            :class="{ active: form.equipment.includes(item.key) }"
            @click="toggleEquipment(item.key)"
          >
            {{ item.label }}
          </view>
        </view>
      </view>

      <view class="card">
        <view class="section-title">饮食</view>
        <view class="field">
          <text class="label">过敏原（会进安全门和食谱筛选）</text>
          <input v-model="form.allergiesText" class="input" placeholder="例如：乳制品、花生、海鲜" />
        </view>
        <view class="field">
          <text class="label">忌口 / 不吃的</text>
          <input v-model="form.dietText" class="input" placeholder="例如：牛肉、香菜" />
        </view>
      </view>


        </view>
      </view>

      <view class="btn-row">
        <view class="btn-sm primary" @click="save">{{ busy ? '保存中…' : '保存' }}</view>
        <view class="btn-sm" @click="saveThenDraft">保存并生成计划</view>
        <view class="btn-sm" @click="openCoach('换一份计划', true)">换一份计划</view>
      </view>

      <!-- #ifdef H5 -->
      <view class="card link-card" @click="openConsole">
        <view class="section-title">Harness Console</view>
        <view class="muted" style="margin-top: 6px">
          看每次生成的 trace、上下文预算、审计链和评测结果。
        </view>
      </view>
      <!-- #endif -->

      <view class="card">
        <view class="section-title">这个 demo 的真实边界</view>
        <view class="limit">· 训练/营养由 skill 的确定性引擎算，模型只负责编排与解释</view>
        <view class="limit">· 热量只算消耗，不含基础代谢与饮食摄入</view>
        <view class="limit">· MET 是通用取值，个体差异可达 ±20%</view>
        <view class="limit">· 审计链能发现篡改，但不能阻止直接改库</view>
        <view class="limit">· 过敏信息只用于排除食材，不构成医疗建议</view>
      </view>
    </view>

    <GlobalCoachWorkspace
      :visible="coachOpen"
      page="profile"
      :initial-text="coachText"
      :auto-send="coachAuto"
      :context="{
        profileGoal: form.goal_note || form.goal,
        profileExperience: form.training_experience,
        preferredTime: form.preferred_time_window,
        preferredLocation: form.preferred_location,
        memoryCount: memories.length + temporaryItems.length,
      }"
      @close="coachOpen = false"
      @changed="onAgentChanged"
      @open-workout="onOpenWorkout"
      @open-today-brief="onOpenTodayBrief"
      @open-memory="onOpenMemory"
      @open-progress="onOpenProgress"
    />
  </view>
</template>

<style>
.profile-page {
  max-width: none;
}
.profile-head {
  align-items: center;
  display: flex;
  gap: 16px;
}
.profile-avatar {
  align-items: center;
  background: linear-gradient(135deg, #f8b26a, var(--primary));
  border-radius: 22px;
  color: #fff;
  display: flex;
  flex: 0 0 auto;
  font-size: 34px;
  font-weight: 800;
  height: 76px;
  justify-content: center;
  width: 76px;
}
.profile-head-copy {
  flex: 1;
  min-width: 0;
}
.profile-actions {
  margin-top: 14px;
}
.profile-layout {
  align-items: stretch;
  display: grid;
  gap: 14px;
  grid-template-columns: minmax(0, 1.12fr) minmax(320px, 0.88fr);
  height: clamp(560px, calc(100vh - 260px), 820px);
  margin-top: 14px;
}
.profile-main,
.profile-side {
  min-height: 0;
  overflow-y: auto;
  padding-right: 4px;
  scrollbar-width: thin;
}
.profile-main {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
}
.profile-main > .card,
.profile-side > .card {
  margin-top: 0;
}
.profile-side {
  min-width: 0;
}
@media (max-width: 980px) {
  .profile-layout {
    height: auto;
    grid-template-columns: 1fr;
  }
  .profile-main,
  .profile-side {
    overflow: visible;
    padding-right: 0;
  }
}
.profile-head-badge {
  align-items: flex-end;
  background: var(--surface);
  border: 1px solid var(--line-soft);
  border-radius: 16px;
  display: flex;
  flex-direction: column;
  min-width: 96px;
  padding: 12px 16px;
}
.profile-head-value {
  color: var(--primary);
  font-size: 26px;
  font-weight: 800;
  line-height: 1;
}
.profile-head-label {
  color: var(--muted);
  font-size: 10px;
  margin-top: 4px;
}
.memory-tag.temporary {
  background: #fff2e8;
  color: #a34b1f;
}
.temporary-row {
  background: #fffaf5;
  border-radius: 10px;
  padding: 9px;
}
.memory-card {
  border-color: var(--primary-soft);
}
.memory-group {
  border-top: 1px dashed var(--line);
  margin-top: 12px;
  padding-top: 10px;
}
.memory-group-title {
  color: var(--muted);
  display: block;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  margin-bottom: 6px;
  text-transform: uppercase;
}
.memory-row {
  border-top: 1px solid var(--line-soft);
  padding: 9px 0;
}
.memory-row:first-of-type {
  border-top: none;
}
.memory-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.memory-head {
  align-items: center;
  display: flex;
  gap: 6px;
}
.memory-tag {
  background: var(--primary-soft);
  border-radius: 999px;
  color: var(--primary-deep);
  font-size: 10px;
  padding: 2px 7px;
}
.memory-tag.short {
  background: var(--line-soft);
  color: var(--muted);
}
.memory-source {
  color: var(--muted);
  font-size: 10px;
}
.memory-value {
  font-size: 13px;
  line-height: 1.55;
}
.memory-actions {
  display: flex;
  gap: 12px;
  margin-top: 2px;
}
.memory-delete {
  color: var(--accent);
  font-size: 12px;
}
.memory-input {
  height: 42px;
  margin-top: 6px;
}
.chip-grid {
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
.field {
  border-top: 1px solid var(--line-soft);
  padding: 12px 0 0;
  margin-top: 12px;
}
.field:first-of-type {
  border-top: none;
}
.label {
  color: var(--muted);
  display: block;
  font-size: 12px;
  margin-bottom: 8px;
}
/* 尺寸交给 theme.css；这里只管上下文间距 */
.input {
  margin-top: 8px;
}
textarea.input {
  margin-top: 12px;
}
.link-card {
  border-color: var(--primary-soft);
}
.limit {
  color: var(--ink-soft);
  font-size: 13px;
  line-height: 1.9;
}
@media (max-width: 820px) {
  .profile-head {
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .profile-head-badge {
    align-items: flex-start;
    flex-direction: row;
    gap: 8px;
    width: fit-content;
  }
}
</style>
