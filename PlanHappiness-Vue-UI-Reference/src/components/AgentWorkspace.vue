<script setup lang="ts">
import { computed, ref } from 'vue'
import { Bot, X, Send, Sparkles, Dumbbell, Clock3, MapPin, Check, ArrowRight } from 'lucide-vue-next'

const emit = defineEmits<{ close: [] }>()
const input = ref('')
const messages = ref([
  { role: 'assistant', text: '你好！我是你的 AI 健身教练。告诉我今天的时间、状态或遇到的问题，我来帮你调整。' }
])
const adjusted = ref(false)
const suggestions = ['今天只有20分钟', '今天想休息', '器械不可用']
const latest = computed(() => messages.value[messages.value.length - 1])

function send(text = input.value) {
  const value = text.trim()
  if (!value) return
  messages.value.push({ role: 'user', text: value })
  input.value = ''
  // Demo behavior only. Replace this section with your real Agent API.
  const isShort = /20|加班|时间不够|半小时/.test(value)
  window.setTimeout(() => {
    messages.value.push({
      role: 'assistant',
      text: isShort
        ? '收到。我会把今天的训练调整为短时居家版本：缩短时长、移除健身房器械，并保留你的长期目标。'
        : '明白，我会结合你的长期目标、今天的安排和训练记录来处理。这次先生成一份调整建议供你确认。'
    })
    adjusted.value = true
  }, 350)
}
</script>

<template>
  <section class="agent-workspace">
    <header class="agent-header">
      <div class="agent-title">
        <span class="bot-avatar"><Bot :size="21" /></span>
        <div><b>你的 AI 健身教练</b><small>统一教练 · 共享对话与记忆</small></div>
      </div>
      <button class="icon-button" @click="emit('close')"><X :size="19" /></button>
    </header>
    <div class="agent-context"><Sparkles :size="15" /> 当前上下文：{{ locationLabel }}</div>
    <div class="chat-list">
      <div v-for="(msg, i) in messages" :key="i" class="message" :class="msg.role">
        <span v-if="msg.role === 'assistant'" class="mini-bot"><Bot :size="15" /></span>
        <p>{{ msg.text }}</p>
      </div>
      <div v-if="adjusted" class="adjustment-card">
        <div class="adjustment-head"><b>今日训练调整建议</b><span class="status-pill">待确认</span></div>
        <div class="adjustment-meta"><span><Clock3 :size="14" /> 20 分钟</span><span><MapPin :size="14" /> 居家</span><span><Dumbbell :size="14" /> 无器械</span></div>
        <div class="adjustment-row"><span>动态热身</span><b>3 分钟</b></div>
        <div class="adjustment-row"><span>徒手深蹲 / 椅子坐站</span><b>3 组</b></div>
        <div class="adjustment-row"><span>墙壁俯卧撑</span><b>3 组</b></div>
        <p class="muted tiny">仅调整今天，长期目标和原计划保持不变。</p>
        <button class="primary full" @click="adjusted = false"><Check :size="16" /> 确认今日调整</button>
      </div>
    </div>
    <div class="composer">
      <div class="suggestion-row"><button v-for="s in suggestions" :key="s" @click="send(s)">{{ s }}</button></div>
      <form class="composer-box" @submit.prevent="send()">
        <input v-model="input" placeholder="告诉教练你的变化或遇到的问题…" />
        <button class="send-button" type="submit" aria-label="发送"><Send :size="17" /></button>
      </form>
      <small>Enter 发送 · 示例界面使用本地模拟回复，接入真实 Agent API 后可替换</small>
    </div>
  </section>
</template>

<script lang="ts">
export default { computed: { locationLabel() { return '当前页面 / 今日训练' } } }
</script>
