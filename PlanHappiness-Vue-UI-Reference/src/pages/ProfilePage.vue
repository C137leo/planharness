<script setup lang="ts">
import { ref } from 'vue'
import { Pencil, Star, Leaf, Clock3, Zap, ChevronRight, Plus } from 'lucide-vue-next'
const memories = ref([
  { title: '长期偏好', icon: Leaf, tone: 'green', items: ['通常午休训练', '不喜欢深蹲'] },
  { title: '近期状态', icon: Clock3, tone: 'blue', items: ['最近工作较忙', '连续2次未完成'] },
  { title: '临时要求', icon: Zap, tone: 'orange', items: ['今天只有20分钟'] }
])
const editing = ref(false)
</script>

<template>
  <div class="page profile-page">
    <section class="profile-hero">
      <img src="/assets/avatar.svg" alt="用户头像" />
      <div><h1>Leo <button class="edit-icon" @click="editing = !editing"><Pencil :size="14" /></button></h1><p>坚持运动，遇见更好的自己 💪</p></div>
      <div class="streak"><Star /><div><small>我的运动天数</small><strong>128 <small>天</small></strong></div></div>
    </section>
    <div class="profile-layout">
      <section class="section-card archive-card">
        <div class="section-heading"><h2>我的健身档案</h2><button class="text-link" @click="editing = !editing">{{ editing ? '完成' : '编辑' }}</button></div>
        <div v-if="!editing" class="profile-fields"><div><span>健身目标</span><b>减脂塑形</b></div><div><span>训练经验</span><b>初级</b></div><div><span>训练时间</span><b>午休（12:00–14:00）</b></div><div><span>训练地点</span><b>健身房 / 家庭</b></div><div><span>可用器械</span><b>哑铃、瑜伽垫</b></div></div>
        <div v-else class="edit-fields"><label>健身目标<input value="减脂塑形"/></label><label>训练时间<input value="午休（12:00–14:00）"/></label><label>训练地点<input value="健身房 / 家庭"/></label></div>
      </section>
      <section class="section-card memory-card">
        <div class="section-heading"><div><h2>教练记忆</h2><p>你可以查看、修改或删除教练记住的内容</p></div><button class="text-link">查看全部 <ChevronRight :size="14" /></button></div>
        <div class="memory-grid"><article v-for="m in memories" :key="m.title" :class="['memory-tile',m.tone]"><component :is="m.icon" :size="20"/><b>{{ m.title }}</b><ul><li v-for="item in m.items" :key="item">{{ item }}</li></ul><button class="memory-edit">管理记忆 <ChevronRight :size="12" /></button></article></div>
      </section>
    </div>
    <section class="section-card profile-bottom"><div><b>和教练聊聊我的健身目标</b><p class="muted tiny">告诉教练你的新目标、偏好或生活变化。</p></div><button class="primary">打开教练 <Plus :size="15" /></button></section>
  </div>
</template>
