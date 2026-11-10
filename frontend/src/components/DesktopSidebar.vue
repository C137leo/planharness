<script setup lang="ts">
/**
 * 桌面端左侧导航壳层。
 * 底部原生 tabBar 在桌面端隐藏，移动端仍由 pages.json 的 tabBar 承担导航。
 */
import { onMounted, onUnmounted } from 'vue'
import { DESKTOP_BREAKPOINT, syncDesktopChrome } from '../composables/desktopChrome'

const props = defineProps<{ active?: 'today' | 'plan' | 'progress' | 'profile' | '' }>()

const items = [
  { key: 'today', label: '今天', icon: '◉', url: '/pages/today/index' },
  { key: 'plan', label: '计划', icon: '▤', url: '/pages/plan/index' },
  { key: 'progress', label: '进展', icon: '▥', url: '/pages/progress/index' },
  { key: 'profile', label: '我的', icon: '◍', url: '/pages/profile/index' },
] as const

function go(item: { key: string; url: string }) {
  if (item.key === props.active) return
  uni.switchTab({ url: item.url })
}

function onResize() {
  syncDesktopChrome()
}

function goProfile() {
  uni.switchTab({ url: '/pages/profile/index' })
}

function goConsole() {
  uni.navigateTo({ url: '/pages/console/index' })
}

onMounted(() => {
  syncDesktopChrome()
  if (typeof window !== 'undefined') window.addEventListener('resize', onResize)
})

onUnmounted(() => {
  if (typeof window !== 'undefined') window.removeEventListener('resize', onResize)
})
</script>

<template>
  <view class="desktop-sidebar">
    <view class="sidebar-brand">
      <view class="sidebar-logo">⌁</view>
      <text class="sidebar-brand-text">Workout Plan Harness</text>
    </view>

    <view class="sidebar-label">我的教练</view>
    <view class="sidebar-nav">
      <view
        v-for="item in items"
        :key="item.key"
        class="sidebar-item"
        :class="{ active: active === item.key }"
        @click="go(item)"
      >
        <text class="sidebar-icon">{{ item.icon }}</text>
        <text class="sidebar-item-text">{{ item.label }}</text>
      </view>
    </view>

    <view class="sidebar-bottom">
      <view class="sidebar-tip">
        <text class="sidebar-tip-title">同一套记忆</text>
        <text class="sidebar-tip-text">今天 · 计划 · 进展 · 我的共享同一个教练。</text>
      </view>
      <view class="sidebar-item muted-item" @click="goConsole">
        <text class="sidebar-icon">▤</text>
        <text class="sidebar-item-text">Harness Console</text>
      </view>
      <view class="sidebar-item muted-item" @click="goProfile">
        <text class="sidebar-icon">⚙</text>
        <text class="sidebar-item-text">设置与档案</text>
      </view>
    </view>
  </view>
</template>

<style>
.desktop-sidebar {
  background: rgba(255, 252, 248, 0.96);
  border-right: 1px solid var(--line, #f0ded2);
  bottom: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  left: 0;
  padding: 26px 14px 18px;
  position: fixed;
  top: 0;
  width: 220px;
  z-index: 900;
}

.sidebar-brand {
  align-items: center;
  display: flex;
  gap: 8px;
  padding: 0 8px 18px;
}

.sidebar-logo {
  align-items: center;
  background: var(--primary, #e2552b);
  border-radius: 10px;
  color: #fff;
  display: flex;
  font-size: 18px;
  font-weight: 800;
  height: 30px;
  justify-content: center;
  width: 30px;
}

.sidebar-brand-text {
  color: var(--ink, #2b1a12);
  font-size: 13px;
  font-weight: 800;
  letter-spacing: -0.02em;
  line-height: 1.2;
}

.sidebar-label {
  color: var(--muted, #8a7466);
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.12em;
  padding: 0 10px;
  text-transform: uppercase;
}

.sidebar-nav {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.sidebar-item {
  align-items: center;
  border-radius: 11px;
  color: var(--ink-soft, #5a4438);
  display: flex;
  gap: 10px;
  padding: 10px 11px;
}

.sidebar-item.active {
  background: var(--primary-soft, #fceae1);
  color: var(--primary-deep, #c2410c);
  font-weight: 700;
}

.sidebar-icon {
  font-size: 14px;
  width: 18px;
}

.sidebar-item-text {
  font-size: 13px;
}

.sidebar-bottom {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: auto;
}

.sidebar-tip {
  background: var(--line-soft, #f7ebe2);
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 10px;
}

.sidebar-tip-title {
  color: var(--ink, #2b1a12);
  font-size: 11px;
  font-weight: 700;
}

.sidebar-tip-text {
  color: var(--muted, #8a7466);
  font-size: 9px;
  line-height: 1.5;
}

.muted-item {
  color: var(--muted, #8a7466);
}

@media (max-width: 919px) {
  .desktop-sidebar {
    display: none;
  }
}
</style>
