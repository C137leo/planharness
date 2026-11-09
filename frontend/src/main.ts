import { createSSRApp } from 'vue'
import App from './App.vue'

export function createApp() {
  const app = createSSRApp(App)

  // 渲染期异常兜底：一个 undefined.length 就会让整页变白，
  // 这里至少把原因打出来并给用户一个可操作的提示，而不是白屏。
  app.config.errorHandler = (err, _instance, info) => {
    // eslint-disable-next-line no-console
    console.error('[planharness] render error:', info, err)

    // uni-app H5 已知问题：<textarea> 内部的 resize-sensor 在页面激活瞬间
    // 可能拿到 null 的 rootRef（切 tab 时卸载/重建表单容易触发）。
    // 它不影响功能，只是会在控制台和 UI 上制造噪音，这里只记录不弹窗。
    const raw = err instanceof Error ? err.message : String(err)
    if (raw.includes('firstElementChild') || raw.includes('resize-sensor')) {
      return
    }

    // 只有 dev 才把完整堆栈报到后端；生产没有 /_debug/client-error，
    // 之前无条件下报会在控制台制造一个无意义的 404。
    if (import.meta.env.DEV) {
      try {
        const message = err instanceof Error ? err.message : String(err)
        const stack = err instanceof Error ? (err.stack || '') : ''
        const page = typeof getCurrentPages === 'function'
          ? (getCurrentPages() as Array<{ route?: string }>).slice(-1)[0]?.route || ''
          : ''
        uni.request({
          url: `${(import.meta.env.VITE_API_BASE || '/api/v1').replace(/\/$/, '')}/_debug/client-error`,
          method: 'POST',
          data: { message, stack, page, info },
          fail: () => {},
        })
      } catch {
        // 上报失败不影响页面
      }
    }
    try {
      // 带上具体原因，出问题时截一张图就能定位，不用再来回猜
      const reason = err instanceof Error ? err.message : String(err)
      uni.showToast({
        title: `页面出错：${reason.slice(0, 40)}`,
        icon: 'none',
        duration: 4000,
      })
    } catch {
      // 非 UI 环境（测试）忽略
    }
  }

  return { app }
}
