<script setup lang="ts">
/**
 * 一句话改计划：底部抽屉。
 *
 * 流程只有四步，且每一步都不自动往下走：
 *   1. 输入自然语言（或点快捷方式）
 *   2. 后端返回草案 / 追问 / 被安全门拦下
 *   3. 草案可以「再改改」——重新规划，最多 3 轮
 *   4. 用户亲手点「确认应用」才提交版本
 *
 * 为什么不自动跳页面：审批按钮如果正好落在刚才点击的位置，
 * 连点两下会直接点穿并提交，之前踩过这个坑。
 */
import { computed, ref, watch } from 'vue'
import { api, ApiError, type PatchRequest } from '../api'
import { QUICK_ACTIONS, describeOperation } from '../styles/copy'

const props = defineProps<{
  visible: boolean
  planId: string
  /** 从别的页面带过来的预设说法，比如「今天想休息」 */
  presetText?: string
  presetQuick?: string
}>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'applied', version: number): void
}>()

const text = ref('')
const quickAction = ref('')
const busy = ref(false)
const error = ref('')
const request = ref<PatchRequest | null>(null)
const feedback = ref('')
const confirming = ref(false)
const presets = ref<Array<{ key: string; label: string; example: string; hint: string }>>([])

const draft = computed(() => request.value?.draft || null)
const canApply = computed(
  () => !!request.value && request.value.status === 'ready' && !!draft.value,
)
const roundsLeft = computed(() => Math.max(0, 3 - ((request.value?.round || 1) - 1)))

async function loadPresets() {
  try {
    presets.value = await api.listPatchPresets()
  } catch {
    // 接口拿不到就用内置兜底，抽屉不会空着
  }
}

watch(
  () => props.visible,
  (open) => {
    if (!open) return
    if (!presets.value.length) loadPresets()
    text.value = props.presetText || ''
    quickAction.value = props.presetQuick || ''
    request.value = null
    feedback.value = ''
    error.value = ''
    confirming.value = false
  },
)

/** 受伤部位快捷项：点一下就等于说了"XX 不舒服，先避开它"。
    规则表和模型都认这些部位（膝/踝/腿/髋/腰/背/肩/肘/腕/胸/手臂）。 */
const BODY_PARTS: Array<{ key: string; text: string }> = [
  { key: '膝盖', text: '膝盖不舒服，先避开它' },
  { key: '脚踝', text: '脚踝不舒服，先避开它' },
  { key: '腿', text: '腿有点疼，先避开它' },
  { key: '髋', text: '髋部不舒服，先避开它' },
  { key: '腰', text: '腰疼，先避开它' },
  { key: '背', text: '后背拉伤，先避开它' },
  { key: '肩', text: '肩膀不舒服，先避开它' },
  { key: '肘', text: '手肘疼，先避开它' },
  { key: '腕', text: '手腕疼，先避开它' },
  { key: '胸', text: '胸肌拉伤，先避开它' },
  { key: '手臂', text: '手臂酸，先避开它' },
]

function pickBodyPart(part: { key: string; text: string }) {
  quickAction.value = 'injury_swap'
  text.value = part.text
}

function pickQuick(key: string, sample: string) {
  quickAction.value = key
  text.value = sample
}

/** 服务端预设 → 界面上的同一套卡片；没有服务端数据时退回内置 quick actions。 */
const quickActions = computed(() =>
  presets.value.length
    ? presets.value.map((item) => ({ key: item.key, label: item.label, text: item.example }))
    : QUICK_ACTIONS,
)

async function submit() {
  if (busy.value || !text.value.trim()) return
  busy.value = true
  error.value = ''
  try {
    request.value = await api.createPatchRequest(props.planId, text.value.trim(), quickAction.value)
  } catch (err) {
    error.value = describeError(err)
  } finally {
    busy.value = false
  }
}

async function revise() {
  if (!request.value || busy.value || !feedback.value.trim()) return
  busy.value = true
  error.value = ''
  try {
    request.value = await api.feedbackPatchRequest(request.value.request_id, feedback.value.trim())
    feedback.value = ''
  } catch (err) {
    error.value = describeError(err)
  } finally {
    busy.value = false
  }
}

async function apply() {
  if (!canApply.value || !request.value || busy.value) return
  busy.value = true
  error.value = ''
  confirming.value = true
  try {
    const result = await api.approvePatchRequest(request.value.request_id, '前端确认应用')
    uni.showToast({ title: `已提交 v${result.committed_version}`, icon: 'success' })
    emit('applied', result.committed_version)
  } catch (err) {
    error.value = describeError(err)
    confirming.value = false
  } finally {
    busy.value = false
  }
}

async function discard() {
  if (request.value && request.value.status === 'ready') {
    try {
      await api.rejectPatchRequest(request.value.request_id, '用户取消')
    } catch {
      // 取消失败不影响关闭抽屉：草案本来就可以丢弃，下次再生成。
    }
  }
  emit('close')
}

function describeError(err: unknown): string {
  if (err instanceof ApiError) return err.message
  if (err instanceof Error) return err.message
  return '请求失败'
}
</script>

<template>
  <view v-if="visible">
    <view class="sheet-mask" @click="emit('close')" />
    <view class="sheet">
      <view class="sheet-grip" />

      <view class="row">
        <view class="section-title">改计划</view>
        <text class="link" @click="discard">关闭</text>
      </view>

      <!-- 1. 输入 -->
      <template v-if="!request">
        <view class="sheet-title">想怎么改？</view>
        <view class="muted" style="margin-top: 6px">
          用一句话说就行，比如「膝盖疼，这周只能练两天」。系统只改该改的，不会重生成整份计划。
        </view>

        <view class="quick-row">
          <view
            v-for="item in quickActions"
            :key="item.key"
            class="chip"
            :class="{ active: quickAction === item.key }"
            @click="pickQuick(item.key, item.text)"
          >
            {{ item.label }}
          </view>
        </view>

        <view class="part-row">
          <text class="part-label">哪里不舒服</text>
          <view class="part-chips">
            <view v-for="part in BODY_PARTS" :key="part.key" class="chip" @click="pickBodyPart(part)">
              {{ part.key }}
            </view>
          </view>
        </view>

        <textarea
          v-model="text"
          class="input"
          maxlength="200"
          placeholder="例如：腰不舒服，想练练上肢"
        />

        <view v-if="error" class="error" style="margin-top: 12px">{{ error }}</view>

        <view class="btn-row">
          <view class="btn-sm primary" @click="submit">{{ busy ? '处理中…' : '看看会怎么改' }}</view>
        </view>
      </template>

      <!-- 2. 追问 -->
      <template v-else-if="request.status === 'clarifying' && !draft">
        <view class="sheet-title">还需要一点信息</view>
        <view class="notice" style="margin-top: 10px">{{ request.question }}</view>
        <view class="btn-row">
          <view class="btn-sm" @click="request = null">重新描述</view>
        </view>
      </template>

      <!-- 3. 被安全门拦下 -->
      <template v-else-if="request.status === 'invalid'">
        <view class="sheet-title">这个改法我没法直接做</view>
        <view class="error" style="margin-top: 10px">{{ request.question }}</view>
        <view class="muted" style="margin-top: 10px">
          安全门是确定性规则（受伤部位 / 可用天数 / 过敏原），模型说了不算。
        </view>
        <view class="btn-row">
          <view class="btn-sm" @click="request = null">换个说法</view>
        </view>
      </template>

      <!-- 4. 草案 -->
      <template v-else-if="draft">
        <view class="row" style="margin-top: 4px">
          <view class="sheet-title">草案</view>
          <view class="chip">第 {{ request.round }} 轮</view>
        </view>

        <view class="draft-summary">{{ draft.summary }}</view>

        <view class="section-title" style="margin-top: 16px">
          改动 {{ draft.operations.length }} 处
        </view>
        <view v-for="(op, index) in draft.operations" :key="index" class="op">
          <text class="op-text">{{ describeOperation(op) }}</text>
          <text v-if="op.reason" class="op-reason">{{ op.reason }}</text>
        </view>

        <view v-if="draft.risks.length" class="notice" style="margin-top: 12px">
          <text v-for="(risk, i) in draft.risks" :key="i">· {{ risk }}
</text>
        </view>

        <view v-if="draft.explanation" class="explain">
          <view class="section-title">为什么这么改</view>
          <view class="muted" style="margin-top: 6px">{{ draft.explanation }}</view>
        </view>

        <view v-if="error" class="error" style="margin-top: 12px">{{ error }}</view>

        <!-- 再改改：基于同一 base_version 重新规划 -->
        <view v-if="roundsLeft > 0" class="feedback">
          <input v-model="feedback" class="input" placeholder="不满意？说一句哪里不对" />
          <view class="btn-sm" @click="revise">{{ busy ? '…' : '再改改' }}</view>
        </view>
        <view v-else class="muted" style="margin-top: 12px">
          已经改了 3 轮，先按这版试一周，或者直接告诉我更具体的方向。
        </view>

        <view class="btn-row">
          <view class="btn-sm primary" @click="apply">{{ confirming ? '提交中…' : '确认应用这一版' }}</view>
          <view class="btn-sm" @click="discard">取消</view>
        </view>
        <view class="muted" style="margin-top: 8px; text-align: center">
          确认后才会生成新版本；不点确认，计划一个字节都不会变。
        </view>
      </template>
    </view>
  </view>
</template>

<style>
.part-row {
  margin-top: 12px;
}

.part-label {
  color: var(--muted);
  display: block;
  font-size: 11px;
  margin-bottom: 6px;
}

.part-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.row {
  align-items: center;
  display: flex;
  justify-content: space-between;
}
.link {
  color: var(--muted, #6b7a75);
  font-size: 13px;
}
.sheet-title {
  font-size: 21px;
  font-weight: 700;
  margin-top: 6px;
}
.quick-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 14px 0;
}
.quick-row .chip {
  border: 1px solid transparent;
}
.quick-row .chip.active {
  border-color: var(--primary, var(--primary));
  font-weight: 600;
}
.input {
  margin-top: 6px;
}
.draft-summary {
  background: var(--primary-soft, #e8f1ec);
  border-radius: 14px;
  font-size: 15px;
  line-height: 1.6;
  margin-top: 10px;
  padding: 12px 14px;
}
.op {
  border-bottom: 1px solid var(--line-soft);
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 10px 0;
}
.op-text {
  font-size: 14px;
  font-weight: 600;
}
.op-reason {
  color: var(--muted, #6b7a75);
  font-size: 12px;
}
.explain {
  margin-top: 14px;
}
.feedback {
  align-items: center;
  display: flex;
  gap: 8px;
  margin-top: 14px;
}
.feedback .input {
  margin-top: 0;
}
.btn.small {
  font-size: 13px;
  margin-top: 0;
  padding: 12px 14px;
  width: auto;
  white-space: nowrap;
}
</style>
