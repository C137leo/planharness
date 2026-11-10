<script setup lang="ts">
/**
 * 知识卡片：动作要领 / 安全 / 恢复 / 营养。
 *
 * 答案来自本地知识库检索，**必须带引用**；检索不到就明确说没有依据。
 */
import { ref, watch } from 'vue'
import { api, ApiError, type KnowledgeAnswer, type KnowledgeCitation } from '../api'
import { exerciseSearchTerm, openExerciseDbTerm } from '../styles/exerciseDb'

const props = defineProps<{
  visible: boolean
  question: string
  bodyPart?: string
}>()
const emit = defineEmits<{ (e: 'close'): void }>()

const loading = ref(false)
const error = ref('')
const data = ref<KnowledgeAnswer | null>(null)

async function load() {
  if (!props.question) return
  loading.value = true
  error.value = ''
  try {
    data.value = await api.askKnowledge(props.question, props.bodyPart)
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

/** 动作类条目统一换成动作库搜索入口；恢复/营养等通用条目才保留原文来源。 */
function citationExerciseTerm(item: KnowledgeCitation): string {
  const title = item.title || ''
  if (/冷身|热身|恢复|酸痛|频率|每周|蛋白质|热量|缺口|补水/.test(title)) return ''
  return exerciseSearchTerm(title) || ''
}

function isSpecificSource(url: string): boolean {
  if (!url) return false
  try {
    const path = new URL(url).pathname.replace(/\/+$/, '')
    return path.length > 0 && path !== '/live-well/exercise'
  } catch {
    return false
  }
}

function openSource(url: string) {
  const globalOpen = (globalThis as unknown as { open?: (u: string, t?: string) => unknown }).open
  if (typeof globalOpen === 'function') {
    globalOpen(url, '_blank')
    return
  }
  uni.setClipboardData({ data: url })
}
</script>

<template>
  <view v-if="visible">
    <view class="sheet-mask" @click="emit('close')" />
    <view class="sheet">
      <view class="row">
        <view class="section-title">知识 · 运动参考</view>
        <text class="link" @click="emit('close')">关闭</text>
      </view>

      <view v-if="loading" class="muted" style="margin-top: 14px">正在检索知识库…</view>
      <view v-else-if="error" class="error" style="margin-top: 14px">{{ error }}</view>

      <template v-else-if="data">
        <view class="knowledge-answer">{{ data.answer }}</view>

        <view v-if="data.citations.length" class="block">
          <view class="section-title">依据</view>
          <view v-for="item in data.citations" :key="item.id" class="list-row">
            <view class="list-main">
              <text class="list-title" style="font-weight: 500">{{ item.title }}</text>
              <text class="list-meta">{{ item.source_title }}</text>
            </view>
            <text
              v-if="citationExerciseTerm(item)"
              class="link exercise-db-link"
              @click="openExerciseDbTerm(citationExerciseTerm(item))"
            >
              动作库 · {{ citationExerciseTerm(item) }} →
            </text>
            <text
              v-else-if="isSpecificSource(item.source_url)"
              class="link"
              @click="openSource(item.source_url)"
            >来源 →</text>
            <text v-else class="source-static">来源：{{ item.source_title }}</text>
          </view>
        </view>
        <view v-else class="muted" style="margin-top: 10px">
          这条没有检索到可引用的依据，所以不做具体建议。
        </view>
      </template>
    </view>
  </view>
</template>

<style>
.knowledge-answer {
  background: var(--primary-soft);
  border-radius: 14px;
  font-size: 14px;
  line-height: 1.75;
  margin-top: 12px;
  padding: 14px;
  white-space: pre-line;
}
.exercise-db-link {
  font-weight: 600;
}
.source-static {
  color: #ad998b;
  font-size: 10px;
  text-align: right;
}
</style>
