<script setup lang="ts">
import { ref } from 'vue'
import { CalendarDays, ChevronDown, ArrowRight, CheckCircle2, Bot, Clock3, X } from 'lucide-vue-next'
const selected = ref(0)
const panelOpen = ref(true)
const confirmed = ref(false)
const days = [
  { day: '周一', date: '11/09', type: '休息' }, { day: '周二', date: '11/10', type: '训练' },
  { day: '周三', date: '11/11', type: '休息' }, { day: '周四', date: '11/12', type: '训练' },
  { day: '周五', date: '11/13', type: '休息' }, { day: '周六', date: '11/14', type: '训练' },
  { day: '周日', date: '11/15', type: '休息' }
]
</script>

<template>
  <div class="page plan-page">
    <div class="page-heading"><span class="eyebrow">MY TRAINING PLAN</span><h1>我的训练计划</h1><p>科学规划，稳步进步</p></div>
    <div class="week-strip">
      <button v-for="(d, i) in days" :key="d.date" :class="{ selected: selected === i }" @click="selected = i"><b>{{ d.day }}</b><strong>{{ d.date }}</strong><small>{{ d.type }}</small></button>
    </div>
    <div class="plan-layout">
      <section class="section-card long-plan">
        <div class="section-heading"><div><h2>长期计划</h2><p>每周3次训练 · 每次45分钟 · 以减脂塑形为目标</p></div><span class="soft-tag">训练周期：8周⌄</span></div>
        <div class="plan-visual"><img src="/assets/fitness-equipment.svg" /><div><h3>训练安排</h3><p><CheckCircle2 /> 周二 — 力量训练</p><p><CheckCircle2 /> 周四 — 有氧训练</p><p><CheckCircle2 /> 周六 — 全身训练</p></div></div>
        <div class="today-adjustment"><b>⚡ 今日临时调整</b><p>今天加班，只剩20分钟，已调整为短时居家训练。</p><span>仅影响今天，不改变长期计划。</span></div>
      </section>
      <aside class="section-card plan-chat">
        <div class="section-heading"><div><span class="section-kicker">AI COACH</span><h2>和教练调整计划</h2></div><Bot class="orange-icon" /></div>
        <div class="bubble">这周只能练两天，每次30分钟</div>
        <h3>调整方案</h3>
        <div class="compare"><div><small>原计划</small><b>周二、四、六<br/>每次45分钟</b></div><ArrowRight /><div class="new-plan"><small>新方案</small><b>周二、六<br/>每次30分钟</b></div></div>
        <ul class="check-list"><li>保持减脂目标不变</li><li>调整为更轻松的安排</li></ul>
        <button class="primary full" @click="confirmed = !confirmed">{{ confirmed ? '已确认调整' : '确认调整' }}</button>
        <p class="tiny muted">长期计划变更将在确认后生成新版本。</p>
      </aside>
    </div>
    <section class="section-card calendar-card"><div class="section-heading"><h2>未来训练安排</h2><button class="text-link">查看全部 <ArrowRight :size="14" /></button></div><div class="calendar-list"><div v-for="(d,i) in days" :key="d.date"><b>{{ d.day }} {{ d.date }}</b><span>{{ d.type === '训练' ? (i === 1 ? '上肢力量' : i === 3 ? '有氧训练' : '全身训练') : '恢复与休息' }}</span><small>{{ d.type === '训练' ? '约45分钟' : '—' }}</small></div></div></section>
  </div>
</template>
