import { createRouter, createWebHistory } from 'vue-router'
import AgentPlanner from '../views/AgentPlanner.vue'
import Planner from '../views/Planner.vue'
import Result from '../views/Result.vue'

// base comes from vite.config.js's `base: '/trip/'`; reading the same value
// here keeps routing consistent when deployed under the alonuniverse.com/trip subpath.
// Default ('/') is AGENT mode (conversational); '/classic' is the form tool.
const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', name: 'agent', component: AgentPlanner },
    { path: '/classic', name: 'planner', component: Planner },
    { path: '/result', name: 'result', component: Result },
  ],
})

export default router
