<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'

defineProps({
  tagline: { type: String, default: 'East Asia · budget backpacking · public transit' },
})

const route = useRoute()
const logoSrc = `${import.meta.env.BASE_URL}logo.png`

// mode toggle: '/' is agent mode (default), '/classic' is the form tool.
// Segmented capsule makes it obvious there are exactly two switchable modes.
const isAgent = computed(() => route.name === 'agent')
</script>

<template>
  <header class="brand">
    <img class="brand-logo" :src="logoSrc" width="40" height="40" alt="AlonTrip" />
    <div class="brand-text">
      <h1>AlonTrip</h1>
      <p class="tagline">{{ tagline }}</p>
    </div>
    <nav class="mode-switch" aria-label="Planner mode">
      <RouterLink to="/" class="seg" :class="{ active: isAgent }">🤖 Agent</RouterLink>
      <RouterLink to="/classic" class="seg" :class="{ active: !isAgent }">🧭 Classic</RouterLink>
    </nav>
  </header>
</template>

<style scoped>
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand-text {
  flex: 1;
}
.mode-switch {
  margin-left: auto;
  display: inline-flex;
  background: var(--panel, #161b22);
  border: 1px solid var(--border, #2d333b);
  border-radius: 999px;
  padding: 3px;
  gap: 2px;
}
.seg {
  border-radius: 999px;
  padding: 5px 14px;
  font-size: 12px;
  text-decoration: none;
  color: var(--muted, #8b949e);
  white-space: nowrap;
}
.seg.active {
  background: var(--cyan, #06b6d4);
  color: #04252b;
  font-weight: 700;
}
.seg:not(.active):hover {
  color: var(--text, #e6edf3);
}
</style>
