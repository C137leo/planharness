import { computed, onMounted, onUnmounted, ref } from 'vue'
import { clampWorkspaceHeight, workspaceHeightBounds } from '../styles/workspaceHeight'

export function useWorkspaceResize(mode: () => string | undefined) {
  const key = () => `planharness_coach_height:${mode() === 'inline' ? 'inline' : 'modal'}`
  const nativeViewport = ref(800)
  const viewport = () => typeof window === 'undefined' ? nativeViewport.value : (window.visualViewport?.height || window.innerHeight)
  function readNativeViewport() {
    try {
      const info = typeof uni.getWindowInfo === 'function' ? uni.getWindowInfo() : uni.getSystemInfoSync()
      if (Number.isFinite(info.windowHeight) && info.windowHeight > 0) nativeViewport.value = info.windowHeight
    } catch { /* 宿主未提供窗口信息时保留最近有效值 */ }
  }
  function nativeResize(event: { size?: { windowHeight?: number } }) {
    const value = event.size?.windowHeight
    if (typeof value === 'number' && Number.isFinite(value) && value > 0) nativeViewport.value = value
    else readNativeViewport()
    resizeViewport()
  }
  const height = ref(600)
  const resizing = ref(false)
  let startY = 0
  let startHeight = 0
  let pointer: number | null = null
  const persist = () => { try { uni.setStorageSync(key(), height.value) } catch { /* 缓存不可用仍可调整 */ } }
  const style = computed(() => ({ height: `${height.value}px`, minHeight: '0', maxHeight: `${workspaceHeightBounds(viewport()).max}px` }))
  function resizeViewport() { height.value = clampWorkspaceHeight(height.value, viewport()) }
  function move(event: PointerEvent) {
    if (event.pointerId !== pointer) return
    event.preventDefault()
    // 弹层居中，顶边拖动距离对应整体高度变化的两倍。
    height.value = clampWorkspaceHeight(startHeight + (startY - event.clientY) * (mode() === 'inline' ? -1 : 2), viewport())
  }
  function stop() {
    if (resizing.value) persist()
    resizing.value = false
    pointer = null
    if (typeof window === 'undefined') return
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', stop)
    window.removeEventListener('pointercancel', stop)
    window.removeEventListener('blur', stop)
  }
  function start(event: PointerEvent) {
    if (event.button != null && event.button !== 0) return
    stop()
    event.preventDefault()
    pointer = event.pointerId
    startY = event.clientY
    startHeight = height.value
    resizing.value = true
    if (typeof window === 'undefined') return
    window.addEventListener('pointermove', move, { passive: false })
    window.addEventListener('pointerup', stop)
    window.addEventListener('pointercancel', stop)
    window.addEventListener('blur', stop)
  }
  function keyboard(event: KeyboardEvent) {
    if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const bounds = workspaceHeightBounds(viewport())
    height.value = clampWorkspaceHeight(event.key === 'Home' ? bounds.min : event.key === 'End' ? bounds.max : height.value + (event.key === 'ArrowUp' ? 40 : -40), viewport())
    persist()
  }
  onMounted(() => {
    if (typeof window === 'undefined') readNativeViewport()
    let saved: unknown
    try { saved = uni.getStorageSync(key()) } catch { /* 使用默认高度 */ }
    height.value = clampWorkspaceHeight(saved, viewport(), mode() === 'inline' ? 600 : viewport() * .86)
    if (typeof window === 'undefined') {
      if (typeof uni.onWindowResize === 'function') uni.onWindowResize(nativeResize)
      return
    }
    window.addEventListener('resize', resizeViewport)
    window.visualViewport?.addEventListener('resize', resizeViewport)
  })
  onUnmounted(() => {
    stop()
    if (typeof window === 'undefined') {
      if (typeof uni.offWindowResize === 'function') uni.offWindowResize(nativeResize)
      return
    }
    window.removeEventListener('resize', resizeViewport)
    window.visualViewport?.removeEventListener('resize', resizeViewport)
  })
  function touchStart(event: any) {
    if (typeof window !== 'undefined') return
    const touch = event.touches?.[0]
    if (touch) start({ pointerId: -1, clientY: touch.clientY ?? touch.pageY, preventDefault() {} } as PointerEvent)
  }
  function touchMove(event: any) {
    if (typeof window !== 'undefined') return
    const touch = event.touches?.[0]
    if (touch && pointer === -1) move({ pointerId: -1, clientY: touch.clientY ?? touch.pageY, preventDefault() {} } as PointerEvent)
  }
  return { height, resizing, style, start, stop, keyboard, touchStart, touchMove }
}
