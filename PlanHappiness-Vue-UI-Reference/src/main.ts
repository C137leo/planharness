import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import TodayPage from './pages/TodayPage.vue'
import PlanPage from './pages/PlanPage.vue'
import ProgressPage from './pages/ProgressPage.vue'
import ProfilePage from './pages/ProfilePage.vue'
import './styles.css'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/today' },
    { path: '/today', component: TodayPage },
    { path: '/plan', component: PlanPage },
    { path: '/progress', component: ProgressPage },
    { path: '/profile', component: ProfilePage }
  ]
})

createApp(App).use(router).mount('#app')
