# PlanHappiness UI Reference (Vue 3 + TypeScript)

这是根据用户提供的四屏 UI 参考图制作的可运行前端原型，包含「今天 / 计划 / 进展 / 我的」四个页面，以及共享的全局 Agent 对话抽屉。

## 技术栈
- Vue 3 + TypeScript
- Vite
- Vue Router
- lucide-vue-next
- 原生 CSS（响应式布局）

## 启动
```bash
npm install
npm run dev
```
打开终端输出的本地地址，通常是 http://localhost:5173。

## 构建
```bash
npm run build
```

## 目录
```text
src/
  components/
    AgentWorkspace.vue   # 全站共享的 Agent 对话工作区
  pages/
    TodayPage.vue        # 今天
    PlanPage.vue         # 计划
    ProgressPage.vue     # 进展
    ProfilePage.vue      # 我的
  App.vue                # 全局导航、路由、Agent 抽屉
  main.ts
  styles.css
public/
  assets/                # SVG 素材
```

## 交互说明
- 左侧导航切换四个页面。
- 右下角橙色教练按钮可从任意页面打开同一个 AgentWorkspace。
- 今天页输入框、计划页方案卡片、我的页档案编辑均有基础交互。
- Agent 回复目前是本地模拟逻辑，方便预览 UI；接入项目时请替换 `src/components/AgentWorkspace.vue` 中 `send()` 的模拟回复部分为真实 API。
- 计划调整确认、训练开始等目前为原型状态，不会写入后端。

## 接入现有项目建议
先复用 `AgentWorkspace.vue` 和 `App.vue` 的全局挂载方式，再将模拟数据替换为现有 API / Pinia store。不要把本原型的演示逻辑当成生产级训练算法或真实记忆服务。
