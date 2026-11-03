<script setup lang="ts">
import { computed, ref } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { api, type PlanPatch, type PlanPatchSummary } from '../../api'

const pending = ref<PlanPatch | null>(null)
const history = ref<PlanPatchSummary[]>([])
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const notice = ref('')
// 审批是高风险动作：进入页面先冷却 800ms，并要求显式勾选，防止「连点穿透」误提交
const cooldown = ref(true)
const confirmed = ref(false)

const actionNames: Record<string, string> = {
  goblet_squat: '高脚杯深蹲',
  squat: '深蹲',
  row: '哑铃划船',
  brisk_walk: '快走',
  glute_bridge: '臀桥',
  easy_walk: '低冲击快走',
}

const STATUS_LABEL: Record<string, string> = {
  pending_approval: '待审批',
  committed: '已提交',
  rejected: '已拒绝',
  draft: '草稿',
}

const changes = computed(() => {
  if (!pending.value) return []
  return pending.value.operations.map((op) => {
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

function label(status: string): string {
  return STATUS_LABEL[status] || status
}

function clock(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    // 服务端是唯一真相：先看待审批，再看历史
    const list = await api.listPatches(50)
    const waiting = list.find((item) => item.status === 'pending_approval')
    history.value = list.filter((item) => item.status !== 'pending_approval')
    pending.value = waiting ? await api.getPatch(waiting.id) : null
    confirmed.value = false
    cooldown.value = true
    setTimeout(() => {
      cooldown.value = false
    }, 800)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function approve() {
  if (!pending.value || busy.value) return
  const accepted = await new Promise<boolean>((resolve) => {
    uni.showModal({
      title: '确认提交新版本？',
      content: '提交后旧版本仍可回滚。',
      confirmText: '确认提交',
      cancelText: '再看看',
      confirmColor: 'var(--primary)',
      success: (result) => resolve(result.confirm),
      fail: () => resolve(false),
    })
  })
  if (!accepted) return
  busy.value = true
  notice.value = ''
  try {
    const result = await api.approvePatch(pending.value.id, '前端审批通过')
    notice.value = `已提交 v${result.committed_version}。如需再次审批，请先到「今天」页生成新的补丁。`
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '审批失败'
  } finally {
    busy.value = false
  }
}

async function regenerate() {
  if (busy.value) return
  busy.value = true
  notice.value = ''
  try {
    const plan = await api.generatePlan()
    notice.value = `已重新生成 v${plan.version}。可以回今天页再改。`
  } catch (err) {
    error.value = err instanceof Error ? err.message : '重新生成失败'
  } finally {
    busy.value = false
  }
}

async function reject() {
  if (!pending.value || busy.value) return
  busy.value = true
  notice.value = ''
  try {
    await api.rejectPatch(pending.value.id, '前端拒绝')
    notice.value = '已拒绝。计划保持原版本，可随时生成新补丁。'
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '拒绝失败'
  } finally {
    busy.value = false
  }
}

onShow(load)
</script>

<template>
  <view class="page">
    <view class="eyebrow">APPROVAL</view>
    <view class="title">{{ pending ? '这次只改 3 个槽位。' : '没有待审批的补丁。' }}</view>
    <view class="subtitle">审批前不会写入新版本；拒绝后计划保持不变。</view>

    <view v-if="notice" class="notice">{{ notice }}</view>

    <view v-if="loading" class="card muted">加载中…</view>
    <view v-else-if="error" class="error">{{ error }}</view>

    <!-- 有 pending：只显示审批卡，不显示历史 -->
    <template v-else-if="pending">
      <view class="card">
        <view class="row">
          <view class="chip">v{{ pending.base_version }} → v{{ pending.base_version + 1 }}</view>
          <view class="status">待审批</view>
        </view>
        <view class="subtitle" style="margin-top: 10px">{{ pending.reason_text }}</view>

        <view class="diff-grid">
          <view class="diff-cell">
            <text class="diff-value">{{ pending.diff.changed_slots }}/{{ pending.diff.total_slots }}</text>
            <text class="diff-label">变更槽位</text>
          </view>
          <view class="diff-cell">
            <text class="diff-value">{{ Math.round(pending.diff.churn * 100) }}%</text>
            <text class="diff-label">churn</text>
          </view>
          <view class="diff-cell">
            <text class="diff-value">{{ Math.round(pending.diff.unchanged_ratio * 100) }}%</text>
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

      <view v-if="pending.validation.explanation" class="notice">
        {{ pending.validation.explanation }}
      </view>

      <view class="confirm-row" @click="confirmed = !confirmed">
        <view class="checkbox" :class="{ on: confirmed }">{{ confirmed ? '✓' : '' }}</view>
        <text class="muted small">我已核对以上 3 处变更，确认提交 v{{ pending.base_version + 1 }}</text>
      </view>

      <view class="btn-row">
        <view class="btn-sm primary" @click="approve">
          {{ busy ? '处理中…' : cooldown ? '请先核对变更…' : '通过并提交 v' + (pending.base_version + 1) }}
        </view>
        <view class="btn-sm accent" @click="reject">拒绝</view>
      </view>
    </template>

    <!-- 没有 pending：只有历史，给出「去今天页改计划」的入口 -->
    <template v-else>
      <view class="card">
        <view class="muted">
          当前没有等待审批的补丁。审批按钮只会在有新补丁时出现。
        </view>
        <view class="btn-row">
          <view class="btn-sm primary" @click="uni.switchTab({ url: '/pages/today/index' })">
            去今天页修改计划
          </view>
          <view class="btn-sm" @click="regenerate">{{ busy ? '生成中…' : '重新生成模板计划' }}</view>
        </view>
        <view class="muted small" style="margin-top: 8px">
          打完一次补丁后，风险动作已被替换；要再演示一遍，先全量重新生成计划。
        </view>
      </view>

      <view class="card">
        <view class="row">
          <view class="eyebrow">HISTORY</view>
          <view class="chip">{{ history.length }} 条</view>
        </view>
        <view v-if="history.length === 0" class="muted">还没有历史补丁。</view>
        <view v-for="item in history" :key="item.id" class="history-item">
          <view class="history-main">
            <view class="change-line">
              <text class="status">{{ label(item.status) }}</text>
              <text class="muted small">v{{ item.base_version }} → v{{ item.base_version + 1 }}</text>
            </view>
            <view class="muted small">{{ item.reason_text }}</view>
          </view>
          <view class="muted small">{{ clock(item.created_at) }}</view>
        </view>
      </view>
    </template>
  </view>
</template>

<style>
.confirm-row {
  align-items: center;
  display: flex;
  gap: 8px;
  padding: 6px 0 14px;
}
.checkbox {
  align-items: center;
  border: 1px solid var(--line);
  border-radius: 4px;
  display: flex;
  height: 18px;
  justify-content: center;
  width: 18px;
  font-size: 13px;
}
.checkbox.on {
  background: var(--primary);
  border-color: var(--primary);
  color: #fff;
}
.status {
  color: #8a5a15;
  font-size: 12px;
}
.diff-grid {
  display: flex;
  gap: 10px;
  margin-top: 16px;
}
.diff-cell {
  background: var(--primary-soft);
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
  color: var(--muted);
  font-size: 11px;
  margin-top: 3px;
}
.change {
  border-top: 1px solid var(--line-soft);
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
  color: #9a2f22;
  text-decoration: line-through;
}
.after {
  color: var(--primary);
  font-weight: 600;
}
.arrow {
  color: var(--muted);
}
.change-reason {
  color: var(--muted);
  font-size: 12px;
  margin-top: 5px;
}
.history-item {
  align-items: center;
  border-bottom: 1px solid var(--line-soft);
  display: flex;
  gap: 10px;
  justify-content: space-between;
  padding: 10px 0;
}
.history-main {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.small {
  font-size: 12px;
}
</style>
