<script setup lang="ts">
/**
 * 我的：把"计划从哪来"摆明白。
 *
 * 这里录的每个字段都会真的影响生成结果：目标 → skill 的训练结构，
 * 体型 → BMR/TDEE，器械 → 动作降级，过敏原 → 食谱排除 + 安全门。
 */
import { computed, reactive, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, type Me, type Profile } from '../../api'
import PlanDraftSheet from '../../components/PlanDraftSheet.vue'

const loading = ref(true)
const busy = ref(false)
const error = ref('')
const toast = ref('')
const draftOpen = ref(false)
const me = ref<Me | null>(null)
const saved = ref(false)

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
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    me.value = await api.me()
    applyProfile(await api.getProfile())
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
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
    })
    applyProfile(profile)
    saved.value = true
    toast.value = '已保存，可以生成计划了'
    return true
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存失败'
    return false
  } finally {
    busy.value = false
  }
}

async function saveThenDraft() {
  if (await save()) draftOpen.value = true
}

async function onConfirmed() {
  draftOpen.value = false
  uni.showToast({ title: '计划已生效', icon: 'success' })
}

function logout() {
  api.logout()
  uni.reLaunch({ url: '/pages/login/index' })
}

function openConsole() {
  uni.navigateTo({ url: '/pages/console/index' })
}

onShow(load)
</script>

<template>
  <view class="page">
    <view class="eyebrow">PLANHARNESS · 我的</view>
    <view class="title">计划是按这些字段算出来的。</view>
    <view class="subtitle">改完保存，再生成计划，结果会跟着变。</view>

    <view v-if="loading" class="card muted">正在加载…</view>
    <view v-else>
      <view v-if="toast" class="notice" style="margin-top: 14px">{{ toast }}</view>
      <view v-if="error" class="error" style="margin-top: 14px">{{ error }}</view>

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

      <view class="btn-row">
        <view class="btn-sm primary" @click="save">{{ busy ? '保存中…' : '保存' }}</view>
        <view class="btn-sm" @click="saveThenDraft">保存并生成计划</view>
        <view class="btn-sm" @click="draftOpen = true">换一份计划</view>
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

    <PlanDraftSheet
      :visible="draftOpen"
      :has-plan="saved"
      :preset-instruction="form.goal_note"
      @close="draftOpen = false"
      @confirmed="onConfirmed"
    />
  </view>
</template>

<style>
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
</style>
