import { createSSRApp } from 'vue'
import App from './App.vue'

export function createApp() {
  const app = createSSRApp(App)

  // 渲染期异常兜底：一个 undefined.length 就会让整页变白，
  // 这里至少把原因打出来并给用户一个可操作的提示，而不是白屏。
  app.config.errorHandler = (err, _instance, info) => {
    // eslint-disable-next-line no-console
    console.error('[planharness] render error:', info, err)
    try {
      uni.showToast({ title: '页面出了点问题，请刷新重试', icon: 'none', duration: 2500 })
    } catch {
      // 非 UI 环境（测试）忽略
    }
  }

  return { app }
}
