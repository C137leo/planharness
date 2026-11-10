<script setup lang="ts">
import { ref } from 'vue'
import { Flame, Lightbulb, ArrowRight, CheckCircle2, CircleAlert } from 'lucide-vue-next'
const showCoachInsight = ref(true)
</script>

<template>
  <div class="page progress-page">
    <div class="page-heading"><span class="eyebrow">YOUR JOURNEY</span><h1>训练进展</h1><p>每一次坚持，都是更好的自己</p></div>
    <div class="stats-grid">
      <section class="stat-card"><div class="progress-circle">78%</div><div><small>本周完成率</small><strong>7 / 9 次</strong><em>↑ +12%</em></div></section>
      <section class="stat-card"><div class="stat-icon flame"><Flame /></div><div><small>本周训练时长</small><strong>175 分钟</strong><em>↑ +20%</em></div></section>
      <section v-if="showCoachInsight" class="insight-card"><div class="insight-title"><Lightbulb /> 教练发现</div><b>最近两次训练未完成，建议下周降低训练负担。</b><button class="primary small" @click="showCoachInsight = false">查看调整建议 <ArrowRight :size="14" /></button></section>
    </div>
    <div class="progress-layout">
      <section class="section-card chart-card"><h2>训练趋势</h2><p class="muted tiny">本周训练完成情况</p><div class="chart"><div class="chart-y"><span>3</span><span>2</span><span>1</span><span>0</span></div><svg viewBox="0 0 620 230" role="img" aria-label="训练趋势折线图"><defs><linearGradient id="area" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#f05a28" stop-opacity=".25"/><stop offset="1" stop-color="#f05a28" stop-opacity="0"/></linearGradient></defs><path d="M20 180 L110 145 L200 100 L290 103 L380 92 L470 62 L560 82 L560 210 L20 210 Z" fill="url(#area)"/><path d="M20 180 L110 145 L200 100 L290 103 L380 92 L470 62 L560 82" fill="none" stroke="#ed5727" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/><g fill="#ed5727"><circle cx="20" cy="180" r="6"/><circle cx="110" cy="145" r="6"/><circle cx="200" cy="100" r="6"/><circle cx="290" cy="103" r="6"/><circle cx="380" cy="92" r="6"/><circle cx="470" cy="62" r="6"/><circle cx="560" cy="82" r="6"/></g></svg><div class="chart-x"><span>周一</span><span>周二</span><span>周三</span><span>周四</span><span>周五</span><span>周六</span><span>周日</span></div></div></section>
      <section class="section-card history-card"><h2>最近训练记录</h2><article><img src="/assets/exercise-pushup.svg"/><div><b>11月08日 周六</b><small>上肢力量训练 · 28分钟</small></div><span class="done">已完成</span></article><article><img src="/assets/exercise-plank.svg"/><div><b>11月06日 周四</b><small>全身训练 · 22分钟</small></div><span class="missed">未完成</span></article><article><img src="/assets/exercise-pushup.svg"/><div><b>11月04日 周二</b><small>力量训练 · 35分钟</small></div><span class="done">已完成</span></article></section>
    </div>
  </div>
</template>
