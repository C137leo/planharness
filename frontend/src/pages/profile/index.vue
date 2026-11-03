<script setup lang="ts">
/**
 * 我的：画像、设备条件、以及"这个 demo 到底做了什么/没做什么"。
 *
 * 最后一段是故意留的：写清楚哪些是真实实现、哪些是演示边界，
 * 比把它藏起来更有用。
 */
import { ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, type Profile } from '../../api'

const profile = ref<Profile | null>(null)
const loading = ref(true)
const error = ref('')

const activityLabels: Record<string, string> = {
  sedentary: '久坐',
  light: '轻度活动',
  moderate: '中等活动',
  active: '高活动量',
}

const equipmentLabels: Record<string, string> = {
  dumbbell: '哑铃',
  band: '弹力带',
  bench: '训练凳',
  treadmill: '跑步机',
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    profile.value = await api.getProfile()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function openConsole() {
  uni.navigateTo({ url: '/pages/console/index' })
}

onShow(load)
</script>

<template>
  <view class="page">
    <view class="eyebrow">PLANHARNESS · 我的</view>
    <view class="title">计划是按这个画像算出来的。</view>
    <view class="subtitle">目标热量、蛋白、动作选择都从这里的字段推导。</view>

    <view v-if="loading" class="card muted">正在加载…</view>
    <view v-else-if="error" class="error">{{ error }}</view>

    <template v-else-if="profile">
      <view class="card">
        <view class="section-title">基础信息</view>
        <view class="kv"><text class="k">性别 / 出生年</text><text class="v">{{ profile.sex }} · {{ profile.birth_year }}</text></view>
        <view class="kv"><text class="k">身高 / 体重</text><text class="v">{{ profile.height_cm }} cm · {{ profile.weight_kg }} kg</text></view>
        <view class="kv"><text class="k">活动水平</text><text class="v">{{ activityLabels[profile.activity_level] || profile.activity_level }}</text></view>
        <view class="kv"><text class="k">训练经验</text><text class="v">{{ profile.training_experience }}</text></view>
        <view class="kv"><text class="k">每周天数 / 时长</text><text class="v">{{ profile.weekly_days }} 天 · {{ profile.session_minutes }} 分钟</text></view>
      </view>

      <view class="card">
        <view class="section-title">器械与饮食</view>
        <view class="kv">
          <text class="k">可用器械</text>
          <text class="v">
            {{ (profile.equipment || []).map((item) => equipmentLabels[item] || item).join('、') || '自重' }}
          </text>
        </view>
        <view class="kv"><text class="k">饮食类型</text><text class="v">{{ profile.diet_pattern }}</text></view>
        <view class="kv">
          <text class="k">过敏原</text>
          <text class="v">{{ (profile.allergies || []).join('、') || '无' }}</text>
        </view>
        <view class="kv">
          <text class="k">不吃的</text>
          <text class="v">{{ (profile.dislikes || []).join('、') || '无' }}</text>
        </view>
      </view>

      <!-- #ifdef H5 -->
      <view class="card link-card" @click="openConsole">
        <view class="section-title">Harness Console</view>
        <view class="muted" style="margin-top: 6px">
          看每一次 Agent 运行的 trace、上下文预算、审计链和评测结果。
        </view>
      </view>
      <!-- #endif -->

      <view class="card">
        <view class="section-title">这个 demo 的真实边界</view>
        <view class="limit">· 意图解析是规则表（可替换成 LLM，接口不变）</view>
        <view class="limit">· 热量只算消耗，不含基础代谢与饮食摄入</view>
        <view class="limit">· MET 是通用取值，个体差异可达 ±20%</view>
        <view class="limit">· 审计链能发现篡改，但不能阻止直接改库</view>
        <view class="limit">· 演示账号是本地 demo 登录，不是真实账号体系</view>
      </view>
    </template>
  </view>
</template>

<style>
.kv {
  align-items: center;
  border-top: 1px solid #f5f1e9;
  display: flex;
  gap: 12px;
  justify-content: space-between;
  padding: 11px 0;
}
.k {
  color: var(--muted);
  font-size: 13px;
}
.v {
  font-size: 14px;
  font-weight: 600;
  text-align: right;
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
