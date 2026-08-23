import { createRouter, createWebHistory } from 'vue-router'
import Planner from '../views/Planner.vue'
import Result from '../views/Result.vue'

// base 由 vite.config.js 的 `base: '/trip/'` 决定：这里读同一个值，
// 部署在 alonuniverse.com/trip 子路径下时路由不跟基座打架。
const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', name: 'planner', component: Planner },
    { path: '/result', name: 'result', component: Result },
  ],
})

export default router
