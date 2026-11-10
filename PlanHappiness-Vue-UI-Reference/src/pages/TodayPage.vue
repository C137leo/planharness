<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { Bot, Clock3, MapPin, Dumbbell, Play, ArrowRight, Flame, CheckCircle2 } from 'lucide-vue-next'
import AgentWorkspace from '../components/AgentWorkspace.vue'
const router = useRouter()
const showInlineAgent = ref(true)
const started = ref(false)
const prompt = ref('')
function submit() { if (prompt.value.trim()) { showInlineAgent.value = true; prompt.value = '' } }
</script>

<template>
  <div class="page today-page">
    <section class="hero-copy">
      <span class="eyebrow">MONDAY · 11月09日</span>
      <h1>今天想怎么练？<br />直接告诉我</h1>
      <p>你想要的改变，藏在今天的每一次训练里。</p>
    </section>

    <section class="hero-composer">
      <form class="hero-input" @submit.prevent="submit">
        <input v-model="prompt" placeholder="例如：今天加班，只剩20分钟，不去健身房了…" />
        <button class="primary" type="submit"><Bot :size="17" /> 发送给教练</button>
      </form>
      <div class="quick-prompts"><button @click="prompt='只剩20分钟'">只剩20分钟</button><button @click="prompt='今天想休息'">今天想休息</button><button @click="prompt='器械不可用'">器械不可用</button></div>
    </section>

    <section class="coach-result glass-card">
      <div class="bot-avatar"><Bot :size="22" /></div>
      <div class="result-copy"><b>收到，今天按原计划进行训练</b><p>你的训练安排已结合近期状态生成，可以随时告诉我变化。</p><div class="chips"><span><MapPin :size="13" /> 健身房</span><span><Dumbbell :size="13" /> 常规器械</span><span><Clock3 :size="13" /> 约 45 分钟</span></div></div>
      <button class="primary start-button" @click="started = !started"><Play :size="16" /> {{ started ? '训练进行中' : '开始训练' }} <ArrowRight :size="16" /></button>
    </section>

    <section class="section-card">
      <div class="section-heading"><div><span class="section-kicker">TODAY'S WORKOUT</span><h2>今日训练任务</h2></div><button class="text-link" @click="router.push('/plan')">查看计划 <ArrowRight :size="15" /></button></div>
      <div class="workout-grid">
        <article class="exercise-card"><img src="/assets/exercise-pushup.svg" /><div><b>俯卧撑</b><small>3组 × 12次</small><em>约5分钟</em></div></article>
        <article class="exercise-card"><img src="/assets/exercise-plank.svg" /><div><b>平板支撑</b><small>3组 × 30秒</small><em>约5分钟</em></div></article>
        <article class="workout-progress"><span>本周概览</span><strong>2/3 <small>次</small></strong><div class="progress-ring">67%</div></article>
      </div>
    </section>

    <section class="bottom-grid">
      <div class="mini-card"><div class="section-heading"><h3>今日饮食建议</h3><Flame :size="18" class="orange-icon" /></div><p>优先保证蛋白质摄入，训练后补充一份均衡餐。</p><button class="text-link" @click="router.push('/profile')">查看详情 <ArrowRight :size="14" /></button></div>
      <div class="mini-card"><div class="section-heading"><h3>训练小提醒</h3><CheckCircle2 :size="18" class="green-icon" /></div><p>动作过程中保持稳定呼吸，疼痛时停止并告诉教练。</p></div>
    </section>
  </div>
</template>
