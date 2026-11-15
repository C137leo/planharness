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
    const pages = typeof getCurrentPages === 'function' ? getCurrentPages() : []
    const route = pages.length ? pages[pages.length - 1].route || '' : ''
    if (route && !['pages/today/index', 'pages/plan/index', 'pages/progress/index', 'pages/profile/index'].includes(route)) return
    // A fail callback prevents uni's Promise adapter from producing an unhandled
    // rejection if the native page has no tabBar during navigation.
    const options = { animation: false, fail: () => {} }
    if (isDesktopViewport()) uni.hideTabBar(options)
    else uni.showTabBar(options)
  } catch {
    // 非 tab 页面 / 非 H5 环境忽略
  }
}
