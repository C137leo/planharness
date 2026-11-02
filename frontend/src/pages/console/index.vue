<script setup lang="ts">
import { ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, type CurrentPlan } from '../../api'

const plan = ref<CurrentPlan | null>(null)
const error = ref('')

async function load() {
  try {
    plan.value = await api.getCurrentPlan()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  }
}

onShow(load)
</script>

<template>
  <view class="page">
    <view class="eyebrow">HARNESS CONSOLE</view>
    <view class="title">把工程证据放在台面上。</view>
    <view class="subtitle">场景 → Run → Trace → Diff → 指标；当前为骨架页。</view>

    <view v-if="error" class="error">{{ error }}</view>

    <view class="card">
      <view class="row">
        <view class="eyebrow">CURRENT STATE</view>
        <view class="chip">live</view>
      </view>
      <view v-if="plan" class="console-summary">
        <view class="big">v{{ plan.version }}</view>
        <view class="muted">{{ plan.actions.length }} actions · {{ Math.round(plan.calorie_target) }} kcal</view>
      </view>
      <view v-else class="muted">加载中…</view>
    </view>

    <view class="card">
      <view class="eyebrow">A/B/C COMPARISON</view>
      <view class="matrix">
        <view class="matrix-row header">
          <text>指标</text><text>A 裸奔</text><text>B 安全门</text><text>C 补丁</text>
        </view>
        <view class="matrix-row">
          <text>安全违规</text><text>≥1</text><text>0</text><text>0</text>
        </view>
        <view class="matrix-row">
          <text>churn</text><text>100%</text><text>~92%</text><text>25%</text>
        </view>
        <view class="matrix-row">
          <text>审批覆盖</text><text>0%</text><text>100%</text><text>100%</text>
        </view>
      </view>
      <view class="notice" style="margin-top: 14px">
        下一轮接：场景切换、真实 Run、Trace 时间线、Quality 看板。
      </view>
    </view>

    <button class="btn secondary" @click="uni.switchTab({ url: '/pages/today/index' })">
      返回今天
    </button>
  </view>
</template>

<style>
.console-summary {
  margin-top: 12px;
}
.big {
  font-size: 40px;
  font-weight: 700;
  color: #176b52;
}
.matrix {
  margin-top: 10px;
}
.matrix-row {
  border-bottom: 1px solid #edf1f1;
  display: grid;
  font-size: 12px;
  gap: 6px;
  grid-template-columns: 1.2fr 1fr 1fr 1fr;
  padding: 9px 0;
}
.matrix-row.header {
  color: #6b7c85;
  font-weight: 600;
}
.matrix-row text:not(:first-child) {
  text-align: right;
}
</style>
