<script setup>
// Planner map: just a "checked = lit, unchecked = gray" preview (plan 7.2).
// The final route is drawn on the result page's ResultMap.vue — the two needs
// diverged (no arrows/editing/per-day toggles here), so separate components
// stay easier to read.
// Dots are clickable: selecting/deselecting shares the same selectedIds as
// the sidebar list.
import { ref, onMounted, watch } from 'vue'
import L from 'leaflet'
import { addDarkBasemap } from '../basemap'

const props = defineProps({
  pois: { type: Array, default: () => [] },
  selectedIds: { type: Array, default: () => [] },
})

const emit = defineEmits(['toggle-poi'])

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

const mapEl = ref(null)
let map = null
let poiLayer = null

onMounted(() => {
  map = L.map(mapEl.value)
  // Tiles come straight from the CDN in the browser (Esri dark basemap +
  // reference labels, no key needed), never through our own server
  addDarkBasemap(map)
  map.setView([35.68, 139.69], 12) // default Tokyo view
  poiLayer = L.layerGroup().addTo(map)
  renderPois(true)
})

// Spot set changes (city switch) → full redraw + refit; selection changes → restyle only
watch(() => props.pois, () => renderPois(true), { deep: true })
watch(() => props.selectedIds, () => renderPois(false), { deep: true })

function renderPois(fit) {
  if (!map) return
  poiLayer.clearLayers()
  const bounds = []
  for (const p of props.pois) {
    const selected = props.selectedIds.includes(p.id)
    const marker = L.circleMarker([p.lat, p.lng], {
      radius: selected ? 8 : 5,
      color: selected ? '#ffd166' : '#8b949e',
      weight: 2,
      fillColor: selected ? '#ffd166' : '#58a6ff',
      fillOpacity: 0.9,
      className: selected ? 'poi-dot poi-dot-on' : 'poi-dot',
    })
    marker.bindTooltip(`${escapeHtml(p.name_en)} · ★ ${p.rating.toFixed(1)}`, { direction: 'top' })
    marker.on('click', (e) => {
      L.DomEvent.stopPropagation(e)
      emit('toggle-poi', p.id)
    })
    marker.addTo(poiLayer)
    bounds.push([p.lat, p.lng])
  }
  if (fit && bounds.length) {
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 })
  }
}
</script>

<template>
  <div class="map-wrap">
    <div id="map" ref="mapEl"></div>
  </div>
</template>
