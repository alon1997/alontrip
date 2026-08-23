<script setup>
defineProps({
  tagline: { type: String, default: 'East Asia · budget backpacking · public transit' },
})

const logoSrc = `${import.meta.env.BASE_URL}logo.png`
</script>

<template>
  <header class="brand">
    <img class="brand-logo" :src="logoSrc" width="40" height="40" alt="AlonTrip" />
    <div class="brand-text">
      <h1>AlonTrip</h1>
      <p class="tagline">{{ tagline }}</p>
    </div>
  </header>
</template>
