<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'

defineProps({
  tagline: {
    type: String,
    default: 'East Asia · budget backpacking · public transit',
  },
})

const route = useRoute()
const logoSrc = `${import.meta.env.BASE_URL}logo.png`

// '/' = agent (default), '/classic' = the form tool, '/how' = explainer
const isAgent = computed(() => route.name === 'agent')
const isHow = computed(() => route.name === 'how')
</script>

<template>
  <header class="appbar">
    <nav class="mode-switch" aria-label="Planner mode">
      <RouterLink to="/" class="seg" :class="{ active: isAgent }">Agent</RouterLink>
      <RouterLink to="/classic" class="seg" :class="{ active: !isAgent && !isHow }">Classic</RouterLink>
    </nav>

    <RouterLink to="/" class="brand center">
      <img class="brand-logo" :src="logoSrc" width="36" height="36" alt="AlonTrip" />
      <div class="brand-text">
        <h1>AlonTrip</h1>
        <p class="tagline">{{ tagline }}</p>
      </div>
    </RouterLink>

    <RouterLink to="/how" class="how-btn" :class="{ active: isHow }">
      <span class="how-glyph">✦</span> How it works
    </RouterLink>
  </header>
</template>

<style scoped>
.appbar {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  gap: 12px;
  width: 100%;
  /* the bar sits on its own surface: a shade above the page background so
     the two zones read as chrome vs content */
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 8px 14px;
}
/* left: mode capsule */
.mode-switch {
  justify-self: start;
  display: inline-flex;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 3px;
  gap: 2px;
}
.seg {
  border-radius: 999px;
  padding: 5px 14px;
  font-size: 12px;
  text-decoration: none;
  color: var(--muted);
  white-space: nowrap;
  letter-spacing: 0.02em;
}
.seg.active {
  background: var(--gold);
  color: #1c1400;
  font-weight: 650;
}
.seg:not(.active):hover {
  color: var(--text);
}

/* center: brand */
.brand.center {
  justify-self: center;
  display: flex;
  align-items: center;
  gap: 10px;
  text-decoration: none;
  color: inherit;
}
.brand-logo {
  border-radius: 8px;
}
.brand-text h1 {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.04em;
  line-height: 1.15;
}
.tagline {
  font-size: 10.5px;
  color: var(--muted);
  letter-spacing: 0.06em;
  white-space: nowrap;
}

/* right: how it works */
.how-btn {
  justify-self: end;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 5px 14px;
  font-size: 12px;
  color: var(--muted);
  text-decoration: none;
  white-space: nowrap;
  background: var(--bg);
  transition: border-color 0.15s, color 0.15s;
}
.how-btn:hover {
  border-color: var(--gold);
  color: var(--gold);
}
.how-btn.active {
  border-color: var(--gold);
  color: var(--gold);
}
.how-glyph {
  font-size: 10px;
  color: var(--gold);
}

@media (max-width: 900px) {
  .appbar { grid-template-columns: auto 1fr auto; }
  .brand.center { display: none; }
}
</style>
