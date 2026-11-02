<script setup lang="ts">
import { computed, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, type PlanPatch } from '../../api'

const patch = ref<PlanPatch | null>(null)
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const patchId = ref('')
const PATCH_KEY = 'planharness_patch_id'

const actionNames: Record<string, string> = {
  goblet_squat: '高脚杯深蹲',
  squat: '深蹲',
  row: '哑铃划船',
  brisk_walk: '快走',
  glute_bridge: '臀桥',
  easy_walk: '低冲击快走',
}

const changes = computed(() => {
  if (!patch.value) return []
  return patch.value.operations.map((op) => {
    if (op.op === 'remove_action') {
      return {
        key: op.slot_key,
        before: actionNames[op.before_action_ref || ''] || op.before_action_ref || '动作',
        after: '删除',
        reason: op.reason,
      }
    }
    return {
      key: op.slot_key,
      before: actionNames[op.before_action_ref || ''] || op.before_action_ref || '动作',
      after: actionNames[op.after_action?.action_ref || ''] || op.after_action?.action_ref || '替代动作',
      reason: op.reason,
    }
  })
})

async function load() {
  if (!patchId.value) {
    error.value = '缺少 patch id'
    loading.value = false
    return
  }
  loading.value = true
  try {
    patch.value = await api.getPatch(patchId.value)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function approve() {
  if (!patch.value || busy.value) return
  const confirmed = await new Promise<boolean>((resolve) => {
    uni.showModal({
      title: '确认提交 v2？',
      content: '提交后旧版本仍可回滚。',
      success: (result) => resolve(result.confirm),
    })
  })
  if (!confirmed) return
  busy.value = true
  try {
    const result = await api.approvePatch(patch.value.id, '前端审批通过')
    uni.showToast({ title: `已提交 v${result.committed_version}`, icon: 'success' })
    setTimeout(() => uni.switchTab({ url: '/pages/today/index' }), 700)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '审批失败'
  } finally {
    busy.value = false
  }
}

async function reject() {
  if (!patch.value || busy.value) return
  busy.value = true
  try {
    await api.rejectPatch(patch.value.id, '前端拒绝')
    uni.removeStorageSync(PATCH_KEY)
    uni.showToast({ title: '已拒绝', icon: 'none' })
    setTimeout(() => uni.switchTab({ url: '/pages/today/index' }), 700)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '拒绝失败'
  } finally {
    busy.value = false
  }
}

onShow(() => {
  const id = String(uni.getStorageSync(PATCH_KEY) || '')
  if (!id) {
    loading.value = false
    error.value = '暂无待审批补丁，请先在“今天”页生成膝痛补丁。'
    return
  }
  if (id !== patchId.value) {
    patchId.value = id
    load()
  }
})
</script>

<template>
  <view class="page">
    <view class="eyebrow">APPROVAL</view>
    <view class="title">这次只改 3 个槽位。</view>
    <view class="subtitle">审批前不会写入新版本；拒绝后 v1 保持不变。</view>

    <view v-if="loading" class="card muted">加载补丁…</view>
    <view v-else-if="error" class="error">{{ error }}</view>

    <template v-else-if="patch">
      <view class="card">
        <view class="row">
          <view class="chip">v{{ patch.base_version }} → v{{ patch.base_version + 1 }}</view>
          <view class="status">{{ patch.status }}</view>
        </view>
        <view class="subtitle" style="margin-top: 10px">{{ patch.reason_text }}</view>

        <view class="diff-grid">
          <view class="diff-cell">
            <text class="diff-value">{{ patch.diff.changed_slots }}/{{ patch.diff.total_slots }}</text>
            <text class="diff-label">变更槽位</text>
          </view>
          <view class="diff-cell">
            <text class="diff-value">{{ Math.round(patch.diff.churn * 100) }}%</text>
            <text class="diff-label">churn</text>
          </view>
          <view class="diff-cell">
            <text class="diff-value">{{ Math.round(patch.diff.unchanged_ratio * 100) }}%</text>
            <text class="diff-label">保留</text>
          </view>
        </view>
      </view>

      <view class="card">
        <view class="eyebrow">CHANGES</view>
        <view v-for="change in changes" :key="change.key" class="change">
          <view class="change-line">
            <text class="before">{{ change.before }}</text>
            <text class="arrow">→</text>
            <text class="after">{{ change.after }}</text>
          </view>
          <view class="change-reason">{{ change.reason }}</view>
        </view>
      </view>

      <view v-if="patch.validation.explanation" class="notice">
        {{ patch.validation.explanation }}
      </view>

      <button class="btn" :disabled="busy || patch.status !== 'pending_approval'" @click="approve">
        {{ busy ? '提交中…' : '通过并提交 v' + (patch.base_version + 1) }}
      </button>
      <button class="btn danger" :disabled="busy || patch.status !== 'pending_approval'" @click="reject">
        拒绝
      </button>
    </template>
  </view>
</template>

<style>
.status {
  color: #8a6d1d;
  font-size: 12px;
}
.diff-grid {
  display: flex;
  gap: 10px;
  margin-top: 16px;
}
.diff-cell {
  background: #f3f7f5;
  border-radius: 12px;
  display: flex;
  flex: 1;
  flex-direction: column;
  padding: 12px;
}
.diff-value {
  font-size: 18px;
  font-weight: 700;
}
.diff-label {
  color: #6b7c85;
  font-size: 11px;
  margin-top: 3px;
}
.change {
  border-top: 1px solid #edf1f1;
  padding: 12px 0;
}
.change:first-of-type {
  border-top: none;
}
.change-line {
  align-items: center;
  display: flex;
  gap: 8px;
  font-size: 15px;
}
.before {
  color: #8a3f36;
  text-decoration: line-through;
}
.after {
  color: #176b52;
  font-weight: 600;
}
.arrow {
  color: #9aa8a8;
}
.change-reason {
  color: #6b7c85;
  font-size: 12px;
  margin-top: 5px;
}
</style>
