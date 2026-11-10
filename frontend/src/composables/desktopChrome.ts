/** 桌面端壳层：隐藏原生底部 tabBar，改用左侧导航。移动端保留原 tabBar。 */

export const DESKTOP_BREAKPOINT = 920

export function isDesktopViewport(): boolean {
  try {
    if (typeof window !== 'undefined' && window.innerWidth) {
      return window.innerWidth >= DESKTOP_BREAKPOINT
    }
    return Number(uni.getSystemInfoSync().windowWidth || 0) >= DESKTOP_BREAKPOINT
  } catch {
    return false
  }
}

export function syncDesktopChrome(): void {
  try {
    if (isDesktopViewport()) uni.hideTabBar({ animation: false })
    else uni.showTabBar({ animation: false })
  } catch {
    // 非 tab 页面 / 非 H5 环境忽略
  }
}
