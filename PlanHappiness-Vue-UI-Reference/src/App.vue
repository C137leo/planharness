<script setup lang="ts">
import { ref } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { Activity, Bell, CalendarDays, ChartNoAxesCombined, House, MessageCircle, Settings, UserRound, X } from 'lucide-vue-next'
import AgentWorkspace from './components/AgentWorkspace.vue'

const route = useRoute()
const router = useRouter()
const agentOpen = ref(false)
const nav = [
  { path: '/today', label: '今天', icon: House },
  { path: '/plan', label: '计划', icon: CalendarDays },
  { path: '/progress', label: '进展', icon: ChartNoAxesCombined },
  { path: '/profile', label: '我的', icon: UserRound }
]
function openAgent() { agentOpen.value = true }
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark"><Activity :size="19" /></span><b>PlanHappiness</b></div>
      <nav class="nav-list">
        <button v-for="item in nav" :key="item.path" class="nav-item" :class="{ active: route.path === item.path }" @click="router.push(item.path)">
          <component :is="item.icon" :size="17" /> <span>{{ item.label }}</span>
        </button>
      </nav>
      <button class="nav-item settings"><Settings :size="17" /><span>设置</span></button>
    </aside>

    <main class="main-area">
      <header class="topbar">
        <div class="mobile-brand"><span class="brand-mark"><Activity :size="17" /></span>PlanHappiness</div>
        <div class="topbar-right"><Bell :size="18" /><img src="/assets/avatar.svg" alt="头像" /></div>
      </header>
      <RouterView />
    </main>

    <button class="floating-coach" aria-label="问问教练" @click="openAgent"><MessageCircle :size="21" /></button>
    <Transition name="drawer">
      <div v-if="agentOpen" class="agent-overlay" @click.self="agentOpen = false">
        <AgentWorkspace @close="agentOpen = false" />
      </div>
    </Transition>
  </div>
</template>
