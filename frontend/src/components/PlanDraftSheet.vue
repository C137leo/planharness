<script setup lang="ts">
/**
 * 生成计划：一句话目标 → 模型调用 skill → 草案 → 改（≤3 轮）→ 确认。
 *
 * 和 PatchComposer 的区别：这里生成的是**整份新计划**（训练 + 食谱 + 摄入目标），
 * 那边改的是已生效计划里的几处。两者的共同点：草案都不落版本，必须用户确认。
 */
import { computed, ref, watch } from 'vue'
import { api, ApiError, type PlanDraft } from '../api'
import { actionLabel, slotLabel } from '../styles/copy'

const props = defineProps<{
  visible: boolean
  presetInstruction?: string
  hasPlan?: boolean
}>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'confirmed', version: number): void
}>()

const instruction = ref('')
const busy = ref(false)
const error = ref('')
const draft = ref<PlanDraft | null>(null)
const feedback = ref('')

const roundsLeft = computed(() => Math.max(0, 3 - ((draft.value?.round || 1) - 1)))
const proteinTarget = computed(() => {
  const macros = draft.value?.nutrition?.macros || {}
  const protein = macros.protein_g || {}
  return protein.high_g || protein.low_g || 0
})
const todayWeekday = computed(() => {
  const day = new Date().getDay()
  return day === 0 ? 7 : day
})
const todayMeals = computed(() =>
  (draft.value?.meals || []).filter((meal) => meal.day_index === todayWeekday.value),
)
const todayDay = computed(() =>
  (draft.value?.days || []).find((day) => day.day_index === todayWeekday.value),
)

watch(
  () => props.visible,
  (open) => {
    if (!open) return
    instruction.value = props.presetInstruction || ''
    draft.value = null
    feedback.value = ''
    error.value = ''
  },
)

async function generate() {
  if (busy.value || !instruction.value.trim()) return
  busy.value = true
  error.value = ''
  try {
    draft.value = await api.createPlanDraft(instruction.value.trim())
  } catch (err) {
    error.value = describeError(err)
  } finally {
    busy.value = false
  }
}

async function revise() {
  if (!draft.value || busy.value || !feedback.value.trim()) return
  busy.value = true
  error.value = ''
  try {
    draft.value = await api.feedbackPlanDraft(draft.value.draft_id, feedback.value.trim())
    feedback.value = ''
  } catch (err) {
    error.value = describeError(err)
  } finally {
    busy.value = false
  }
}

async function confirm() {
  if (!draft.value || busy.value || draft.value.status !== 'ready') return
  busy.value = true
  error.value = ''
  try {
    const result = await api.confirmPlanDraft(draft.value.draft_id, '前端确认')
    uni.showToast({ title: `已生效 v${result.committed_version}`, icon: 'success' })
    emit('confirmed', result.committed_version)
  } catch (err) {
    error.value = describeError(err)
  } finally {
    busy.value = false
  }
}

async function discard() {
  if (draft.value && draft.value.status === 'ready') {
    try {
      await api.rejectPlanDraft(draft.value.draft_id, '用户取消')
    } catch {
      // 取消失败不影响关闭：草案本来就可以丢
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
        <view class="section-title">{{ hasPlan ? '换一份计划' : '生成计划' }}</view>
        <text class="link" @click="discard">关闭</text>
      </view>

      <!-- 输入目标 -->
      <template v-if="!draft">
        <view class="sheet-title">你想要什么</view>
        <view class="muted" style="margin-top: 6px">
          说清目标和限制就行，比如「想 3 个月瘦 8 公斤，每周 3 天，只有哑铃，不吃牛奶」。
          模型会调用教练 skill 生成训练和食谱，再给你确认。
        </view>
        <textarea
          v-model="instruction"
          class="input"
          maxlength="300"
          placeholder="例如：减脂，每周 3 天每次 40 分钟，家里只有哑铃，乳制品过敏"
        />
        <view v-if="error" class="error" style="margin-top: 12px">{{ error }}</view>
        <view class="btn-row">
          <view class="btn-sm primary" @click="generate">
            {{ busy ? '生成中…' : '生成草案' }}
          </view>
          <view class="btn-sm" @click="emit('close')">先不用</view>
        </view>
      </template>

      <!-- 没生成出来 -->
      <template v-else-if="draft.status !== 'ready'">
        <view class="sheet-title">这次没能生成</view>
        <view class="notice" style="margin-top: 10px">
          {{ draft.question || '信息不够，再补充一句目标或身体情况。' }}
        </view>
        <view v-if="draft.warnings.length" class="error" style="margin-top: 10px">
          <text v-for="(item, i) in draft.warnings" :key="i">· {{ item }}
</text>
        </view>
        <view class="btn-row">
          <view class="btn-sm" @click="draft = null">重新描述</view>
        </view>
      </template>

      <!-- 草案 -->
      <template v-else>
        <view class="row" style="margin-top: 4px">
          <view class="sheet-title">这份计划</view>
          <view class="chip">第 {{ draft.round }} 轮</view>
        </view>
        <view class="draft-summary">{{ draft.summary }}</view>

        <view class="stat-row">
          <view class="stat">
            <text class="stat-value">{{ draft.nutrition.target_kcal || '—' }}</text>
            <text class="stat-label">kcal / 天</text>
          </view>
          <view class="stat">
            <text class="stat-value">{{ proteinTarget || '—' }}</text>
            <text class="stat-label">蛋白 g 起</text>
          </view>
          <view class="stat">
            <text class="stat-value">{{ draft.days.length }}</text>
            <text class="stat-label">训练日 / 周</text>
          </view>
        </view>

        <view v-if="todayDay" class="block">
          <view class="section-title">今天练（第 {{ todayDay.day_index }} 天）</view>
          <view class="list">
            <view v-for="(action, i) in todayDay.actions" :key="i" class="list-row">
              <view class="list-main">
                <text class="list-title">{{ actionLabel(action) }}</text>
                <text class="list-meta">
                  {{ slotLabel(action.slot) }}
                  <template v-if="action.duration_min"> · {{ action.duration_min }} 分钟</template>
                </text>
              </view>
            </view>
          </view>
        </view>

        <view v-if="todayMeals.length" class="block">
          <view class="section-title">今天吃</view>
          <view v-for="meal in todayMeals" :key="meal.slot" class="meal">
            <view class="row">
              <text class="meal-title">{{ meal.title }}</text>
              <text class="list-value">{{ meal.kcal }} kcal</text>
            </view>
            <text class="list-meta">
              {{ meal.items.map((item) => `${item.name} ${item.grams}g`).join(' · ') }}
            </text>
          </view>
        </view>

        <view v-if="draft.cautions.length" class="notice" style="margin-top: 14px">
          <text v-for="(item, i) in draft.cautions" :key="i">· {{ item }}
</text>
        </view>
        <view v-if="draft.highlights.length" class="block">
          <view class="section-title">注意</view>
          <view v-for="(item, i) in draft.highlights" :key="i" class="list-row">
            <text class="list-title" style="font-weight: 400">{{ item }}</text>
          </view>
        </view>

        <view v-if="error" class="error" style="margin-top: 12px">{{ error }}</view>

        <view v-if="roundsLeft > 0" class="feedback">
          <input v-model="feedback" class="input" placeholder="哪里不满意？说一句" />
          <view class="btn-sm" @click="revise">{{ busy ? '…' : '再改改' }}</view>
        </view>
        <view v-else class="muted" style="margin-top: 12px">
          已经改了 3 轮，先按这版走两周，再根据体感调整。
        </view>

        <view class="btn-row">
          <view class="btn-sm primary" @click="confirm">
            {{ busy ? '提交中…' : '确认，按这个执行' }}
          </view>
          <view class="btn-sm" @click="discard">不要了</view>
        </view>
        <view class="muted" style="margin-top: 8px">
          确认后才会生成新版本；不确认的话，现有计划不受影响。
        </view>
      </template>
    </view>
  </view>
</template>

<style>
.link {
  color: var(--muted);
  font-size: 13px;
}
.sheet-title {
  font-size: 20px;
  font-weight: 700;
  margin-top: 6px;
}
.input {
  background: #faf9f6;
  border: 1px solid var(--line);
  border-radius: 14px;
  font-size: 15px;
  margin-top: 8px;
  padding: 11px 13px;
  width: 100%;
}
textarea.input {
  height: 84px;
  line-height: 1.5;
}
.draft-summary {
  background: var(--primary-soft);
  border-radius: 14px;
  font-size: 14px;
  line-height: 1.6;
  margin-top: 10px;
  padding: 12px 14px;
}
.block {
  margin-top: 16px;
}
.meal {
  border-top: 1px solid #f1ede4;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 0;
}
.meal-title {
  font-size: 14px;
  font-weight: 600;
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
</style>
