<script setup>
// 规划页地图：只做「已勾亮、未勾灰」的预览（方案 7.2）。最终路线画在结果页
// 的 ResultMap.vue —— 两边需求已分叉（这里不需要箭头/编辑/按天开关），拆开更好读。
// 圆点可点：选中/取消跟左侧列表共用同一套 selectedIds。
import { ref, onMounted, watch } from 'vue'
import L from 'leaflet'

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
  // 瓦片由浏览器直连 CDN（OSM 数据，CARTO 暗色渲染），不经过本项目服务器
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 19,
  }).addTo(map)
  map.setView([35.68, 139.69], 12) // 默认东京视野
  poiLayer = L.layerGroup().addTo(map)
  renderPois(true)
})

// 景点集合变化（切城市）→ 重绘并重定位；勾选变化 → 只重绘样式
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
