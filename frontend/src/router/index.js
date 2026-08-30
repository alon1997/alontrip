import { createRouter, createWebHistory } from 'vue-router'
import Planner from '../views/Planner.vue'
import Result from '../views/Result.vue'

// base comes from vite.config.js's `base: '/trip/'`; reading the same value
// here keeps routing consistent when deployed under the alonuniverse.com/trip subpath.
const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', name: 'planner', component: Planner },
    { path: '/result', name: 'result', component: Result },
  ],
})

export default router
