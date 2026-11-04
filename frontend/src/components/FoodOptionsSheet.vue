<script setup lang="ts">
/**
 * 「换一换」：某一餐的 3 个替换推荐 + 京东/淘宝/美团搜索深链。
 *
 * 只做搜索跳转：不登录平台、不代下单、不碰支付。价格/库存以平台为准。
 */
import { ref, watch } from 'vue'
import { api, ApiError, type FoodOptionPayload } from '../api'

const props = defineProps<{
  visible: boolean
  planId: string
  date: string
  mealSlot: string
  mealTitle: string
}>()
const emit = defineEmits<{ (e: 'close'): void }>()

const loading = ref(false)
const error = ref('')
const data = ref<FoodOptionPayload | null>(null)

async function load(refresh = false) {
  if (!props.planId) return
  loading.value = true
  error.value = ''
  try {
    data.value = await api.foodOptions(props.planId, props.date, props.mealSlot, refresh)
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

watch(
  () => props.visible,
  (open) => {
    if (!open) return
    data.value = null
    load()
  },
)

function openLink(url: string) {
  if (!url) return
  // H5 直接新开搜索页；小程序不能随便打开外链，复制链接让用户去 App 里看
  const globalOpen = (globalThis as unknown as { open?: (u: string, t?: string) => unknown }).open
  if (typeof globalOpen === 'function') {
    globalOpen(url, '_blank')
    return
  }
  uni.setClipboardData({
    data: url,
    success: () => uni.showToast({ title: '链接已复制，去 App 里打开', icon: 'none' }),
  })
}
</script>

<template>
  <view v-if="visible">
    <view class="sheet-mask" @click="emit('close')" />
    <view class="sheet">
      <view class="row">
        <view class="section-title">{{ mealTitle }} · 换一换</view>
        <text class="link" @click="emit('close')">关闭</text>
      </view>

      <view v-if="data" class="list-meta" style="margin-top: 6px">
        目标 {{ data.target_kcal }} kcal · 蛋白 {{ data.target_protein_g }}g
      </view>

      <view v-if="loading" class="muted" style="margin-top: 14px">正在生成替换方案…</view>
      <view v-else-if="error" class="error" style="margin-top: 14px">{{ error }}</view>

      <template v-else-if="data">
        <view v-for="option in data.options" :key="option.id" class="food-card">
          <view class="row">
            <text class="list-title">{{ option.title }}</text>
            <text class="list-value">{{ option.kcal }} kcal</text>
          </view>
          <text class="list-meta">
            {{ option.items.map((item) => `${item.name} ${item.grams}g`).join(' · ') }}
          </text>
          <text class="list-meta">
            蛋白 {{ option.protein_g }}g · 脂肪 {{ option.fat_g }}g · 碳水 {{ option.carb_g }}g
          </text>
          <text class="food-reason">{{ option.reason }}</text>
          <view class="btn-row">
            <view class="btn-sm" @click="openLink(option.links.jd)">京东</view>
            <view class="btn-sm" @click="openLink(option.links.taobao)">淘宝</view>
            <view class="btn-sm" @click="openLink(option.links.meituan)">美团</view>
          </view>
        </view>

        <view class="muted" style="margin-top: 12px">{{ data.disclaimer }}</view>
        <view class="btn-row">
          <view class="btn-sm" @click="load(true)">换一批</view>
        </view>
      </template>
    </view>
  </view>
</template>

<style>
.food-card {
  border-top: 1px solid var(--line-soft);
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 12px;
  padding-top: 12px;
}
.food-reason {
  color: var(--primary-deep);
  font-size: 12px;
}
</style>
