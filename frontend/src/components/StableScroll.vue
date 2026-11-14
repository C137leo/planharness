<script setup lang="ts">
import { nextTick, onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { forwardBoundaryWheel } from '../styles/scrollChain'
const props = defineProps<{ scrollY?: boolean; scrollTop?: number; chainToParent?: boolean }>()
const element = ref<unknown>(null)
let alive = true
let lastTop = 0
let wheelElement: HTMLElement | null = null
function handoff(event: WheelEvent) {
  if (props.chainToParent && wheelElement && (event.target as HTMLElement)?.closest?.('[data-stable-scroll]') === wheelElement) {
    forwardBoundaryWheel(wheelElement, event)
  }
}
onMounted(() => {
  // #ifdef H5
  const value = element.value as { $el?: HTMLElement } | HTMLElement | null
  wheelElement = value && ('$el' in value ? value.$el : value) as HTMLElement | null
  wheelElement?.addEventListener('wheel', handoff, { passive: false })
  // #endif
})
function restore(top = lastTop) {
  // H5 tab activation may run after a conditional child has already unmounted.
  const value = element.value as { $el?: HTMLElement } | HTMLElement | null
  const el = value && ('$el' in value ? value.$el : value) as HTMLElement | null
  if (alive && el) el.scrollTop = top
}
function remember(event: Event) { lastTop = (event.target as HTMLElement).scrollTop }
watch(() => props.scrollTop, async value => { await nextTick(); if (value != null) restore(value) })
onActivated(async () => { await nextTick(); restore() })
onBeforeUnmount(() => { alive = false; wheelElement?.removeEventListener('wheel', handoff); wheelElement = null })
</script>
<template>
  <!-- #ifdef H5 -->
  <view ref="element" class="stable-scroll" :class="{ 'chain-to-parent': chainToParent }" data-stable-scroll @scroll="remember"><slot /></view>
  <!-- #endif -->
  <!-- #ifndef H5 -->
  <scroll-view :scroll-y="scrollY" :scroll-top="scrollTop"><slot /></scroll-view>
  <!-- #endif -->
</template>
<style scoped>
.stable-scroll { display: block; overflow-y: auto; overflow-x: hidden; min-height: 0; overscroll-behavior-y: contain; -webkit-overflow-scrolling: touch; }
.stable-scroll.chain-to-parent { overscroll-behavior-y: auto; }
</style>
