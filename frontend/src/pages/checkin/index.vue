<script setup lang="ts">
import { reactive, ref } from 'vue'
import { onLoad } from '@dcloudio/uni-app'
import { api } from '../../api'

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
  const now = new Date()
  const month = `${now.getMonth() + 1}`.padStart(2, '0')
  const day = `${now.getDate()}`.padStart(2, '0')
  return `${now.getFullYear()}-${month}-${day}`
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

onLoad(() => {
  form.date = today()
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

    <button class="btn" :disabled="busy" @click="submit">
      {{ busy ? '保存中…' : '保存今日打卡' }}
    </button>
  </view>
</template>

<style>
.field {
  border-bottom: 1px solid #edf1f1;
  padding: 12px 0;
}
.field:last-child {
  border-bottom: none;
}
.label {
  color: #6b7c85;
  display: block;
  font-size: 13px;
  margin-bottom: 8px;
}
.input {
  background: #f6f8f8;
  border-radius: 10px;
  font-size: 16px;
  padding: 12px;
  width: 100%;
}
</style>
