<script setup>
// T-015 结果页地图：彩点(按天)+灰点(目录未选)+酒店端点+带箭头折线+城际虚线+
// 按天开关+几何微调(加灰点/删彩点)+ Update transit(只查变化段)+导出 PNG。
// 规划页的简单预览留在 MapView.vue；两边需求已分叉，拆成独立组件更好维护。
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import L from 'leaflet'
import { toPng } from 'html-to-image'
import { DAY_COLORS } from '../colors'
import { haversineKm } from '../geo'
import { transitLegs } from '../api'
import { money, toUsd } from '../money'

const props = defineProps({
  itinerary: { type: Array, required: true },
  poisByCity: { type: Object, required: true }, // { city: [poi, ...] } — full catalog for every city in the trip
  // T-016: at most two hub nodes ever exist in the whole trip — day 1's
  // start (arrival) and the last day's end (departure) — so these come in
  // as plain objects, not a per-city catalog like pois/lodgings.
  arrivalHub: { type: Object, default: null },
  departureHub: { type: Object, default: null },
})

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

const fmt = (min) => (min >= 60 ? `${Math.floor(min / 60)}h ${min % 60}m` : `${min} min`)

// ---------- 可编辑的行程副本（深拷贝，不改 sessionStorage 里的原始结果） ----------
const days = reactive(JSON.parse(JSON.stringify(props.itinerary)))
const dayVisible = reactive(Object.fromEntries(days.map((d) => [d.day, true])))
// legCache 的 key 见 edgeKey()；命中即代表这段已经有真实/估算的交通数据可画。
const legCache = reactive(new Map())
for (const day of days) {
  for (const leg of day.legs || []) {
    legCache.set(`${leg.from_kind}:${leg.from_id}=>${leg.to_kind}:${leg.to_id}`, leg.route)
  }
}

const poiById = computed(() => {
  const m = {}
  for (const city of Object.keys(props.poisByCity)) {
    for (const p of props.poisByCity[city]) m[p.id] = p
  }
  return m
})
const lodgingById = computed(() => {
  const m = {}
  for (const day of days) m[day.lodging.id] = day.lodging
  return m
})
// 每个酒店归属哪座城市：换城当晚已经住下一城（方案 6.1b E），所以用「当天」的
// 城市记它，换城日早上那家（属于上一城）会被上一天的记录覆盖到正确答案。
const lodgingCityMap = computed(() => {
  const m = {}
  for (const day of days) m[day.lodging.id] = day.city
  return m
})
// T-016: arrival/departure hub lookup — at most 2 entries, keyed by id like
// the poi/lodging maps above so cityOf/coordOf/nodeLabel stay uniform.
const hubById = computed(() => {
  const m = {}
  if (props.arrivalHub) m[props.arrivalHub.id] = props.arrivalHub
  if (props.departureHub) m[props.departureHub.id] = props.departureHub
  return m
})

function objOf(node) {
  if (node.kind === 'poi') return poiById.value[node.id]
  if (node.kind === 'hub') return hubById.value[node.id]
  return lodgingById.value[node.id]
}
function cityOf(node) {
  if (node.kind === 'poi') return poiById.value[node.id]?.city
  if (node.kind === 'hub') return hubById.value[node.id]?.city
  return lodgingCityMap.value[node.id]
}
function coordOf(node) {
  const obj = objOf(node)
  return obj ? { lat: obj.lat, lng: obj.lng } : null
}
function nodeLabel(node) {
  const obj = objOf(node)
  return obj ? obj.name_en || obj.name : node.id
}
function edgeKey(a, b) {
  return `${a.kind}:${a.id}=>${b.kind}:${b.id}`
}
function edgeIsIntercity(a, b) {
  return cityOf(a) !== cityOf(b)
}

const currentEdges = computed(() => {
  const list = []
  for (const day of days) {
    for (let i = 0; i < day.nodes.length - 1; i++) {
      const a = day.nodes[i]
      const b = day.nodes[i + 1]
      list.push({ day: day.day, color: DAY_COLORS[(day.day - 1) % DAY_COLORS.length], i, a, b, key: edgeKey(a, b), intercity: edgeIsIntercity(a, b) })
    }
  }
  return list
})
const pendingEdges = computed(() => currentEdges.value.filter((e) => !legCache.has(e.key)))
const usedPoiIds = computed(() => {
  const s = new Set()
  for (const day of days) for (const n of day.nodes) if (n.kind === 'poi') s.add(n.id)
  return s
})
// 灰点：已选城市目录里、当前不在任何一天行程内的点——现算而不是只信服务器
// 首次返回的 unselected_poi_ids，这样加/删点之后灰点集合会跟着实时变化。
const grayPois = computed(() => {
  const used = usedPoiIds.value
  const list = []
  for (const city of Object.keys(props.poisByCity)) {
    for (const p of props.poisByCity[city]) if (!used.has(p.id)) list.push(p)
  }
  return list
})

function clockStr(minutes) {
  const m = ((Math.floor(minutes) % (24 * 60)) + (24 * 60)) % (24 * 60)
  return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`
}

function lineSummary(route) {
  if (!route) return 'Transit (pending)'
  if ((route.legs || []).some((leg) => leg.travel_mode === 'taxi')) {
    return 'No public transit · taxi'
  }
  if (route.estimated) {
    const walking = (route.legs || []).some((leg) => leg.travel_mode === 'walking')
    return walking && route.total_cost === 0 ? 'Walk' : 'No public transit · taxi'
  }
  const names = (route.legs || [])
    .filter((leg) => leg.travel_mode === 'transit' && leg.line_name)
    .map((leg) => leg.line_name)
  if (names.length) return names.join(' → ')
  if ((route.legs || []).some((leg) => leg.travel_mode === 'walking')) return 'Walk'
  return 'No public transit · taxi'
}

// Mirror backend schedule.py so "Update transit" / add-remove POI keep the
// clock honest instead of freezing the first optimize-route payload.
function buildDaySchedule(day) {
  const chain = day.nodes
  const events = []
  let clock = 9 * 60
  let lunchDone = false
  let dinnerDone = false
  const poiCount = chain.filter((n) => n.kind === 'poi').length
  const isFirst = day.day === 1
  const hasArrival = chain[0]?.kind === 'hub'
  const dinnerEarliest = 17 * 60 + 30
  const dinnerLatest = 19 * 60 + 30
  const dinnerBeforeHome = 17 * 60 + 20

  const addDinner = () => {
    events.push({
      kind: 'dinner',
      start: clockStr(clock),
      end: clockStr(clock + 90),
      duration_min: 90,
      title: 'Dinner',
    })
    clock += 90
    dinnerDone = true
  }

  for (let i = 0; i < chain.length - 1; i++) {
    const from = chain[i]
    const to = chain[i + 1]
    if (edgeIsIntercity(from, to) && clock < 16 * 60) clock = 16 * 60

    const route = legCache.get(edgeKey(from, to))
    const dur = route ? route.total_duration_min : 15
    const estimated = route ? !!route.estimated : true
    const modes = (route?.legs || []).map((leg) => leg.travel_mode)
    const walkOnly = modes.length > 0 && modes.every((m) => m === 'walking')
    const transitCost = walkOnly ? 0 : (route ? route.total_cost : null)
    events.push({
      kind: 'transit',
      start: clockStr(clock),
      end: clockStr(clock + dur),
      duration_min: dur,
      title: `${nodeLabel(from)} → ${nodeLabel(to)}`,
      line_summary: lineSummary(route),
      cost: transitCost,
      currency: 'USD',
      estimated,
    })
    clock += dur

    const stillHasPoi = chain.slice(i + 2).some((n) => n.kind === 'poi')
    if (isFirst && hasArrival && to.kind === 'lodging' && stillHasPoi) {
      events.push({
        kind: 'checkin',
        start: clockStr(clock),
        end: clockStr(clock + 20),
        duration_min: 20,
        title: 'Hotel check-in / drop bags',
      })
      clock += 20
    }

    if (to.kind === 'poi') {
      const poi = poiById.value[to.id]
      let remain = poi?.suggested_duration_min ?? 90
      const ticket = toUsd(poi?.ticket_price ?? null, poi?.ticket_currency)
      const tcur = ticket == null ? null : 'USD'
      const visitName = poi ? (poi.name_en || poi.name) : to.id
      const lunchStart = 11 * 60 + 30
      const lunchNoon = 12 * 60
      const lunchEnd = 14 * 60
      const morePois = chain.slice(i + 2).some((n) => n.kind === 'poi')

      while (remain > 0) {
        if (!lunchDone && clock < lunchEnd && clock + remain >= lunchStart) {
          const before = Math.max(0, Math.min(remain, lunchNoon - clock))
          const after = remain - before
          if (before > 15 && after > 15) {
            events.push({
              kind: 'visit',
              start: clockStr(clock),
              end: clockStr(clock + before),
              duration_min: before,
              title: visitName,
              cost: ticket,
              currency: tcur,
            })
            clock += before
            remain -= before
          } else if (after <= 15) {
            events.push({
              kind: 'visit',
              start: clockStr(clock),
              end: clockStr(clock + remain),
              duration_min: remain,
              title: visitName,
              cost: ticket,
              currency: tcur,
            })
            clock += remain
            remain = 0
          }
          events.push({
            kind: 'lunch',
            start: clockStr(clock),
            end: clockStr(clock + 60),
            duration_min: 60,
            title: 'Lunch',
          })
          clock += 60
          lunchDone = true
          continue
        }
        events.push({
          kind: 'visit',
          start: clockStr(clock),
          end: clockStr(clock + remain),
          duration_min: remain,
          title: visitName,
          cost: ticket,
          currency: tcur,
        })
        clock += remain
        remain = 0
      }
      if (!dinnerDone && !morePois && clock >= dinnerBeforeHome && clock <= dinnerLatest) {
        addDinner()
      }
    }
  }
  if (poiCount && !dinnerDone) {
    if (clock < dinnerEarliest) clock = dinnerEarliest
    addDinner()
  }
  return events
}

// 每天汇总（给结果页侧栏用）：分钟/票价/换乘按当前 legCache 现算，未更新的
// 段不计入且单独计数，侧栏据此提示「还有 N 段没更新」。
const daySummaries = computed(() =>
  days.map((day) => {
    const edges = currentEdges.value.filter((e) => e.day === day.day)
    let minutes = 0, cost = 0, transfers = 0, allReal = true, pending = 0
    let currency = day.currency || 'USD'
    for (const e of edges) {
      const route = legCache.get(e.key)
      if (!route) { pending += 1; allReal = false; continue }
      minutes += route.total_duration_min
      cost += route.total_cost
      transfers += route.transfer_count
      currency = route.currency
      if (route.estimated) allReal = false
    }
    return {
      day: day.day,
      city: day.city,
      lodgingName: day.lodging.name,
      // T-016: day 1's start / the last day's end may be a hub, not the
      // night's lodging — nodeLabel() resolves whichever kind is really
      // there instead of the sidebar always assuming a hotel loop.
      startLabel: nodeLabel(day.nodes[0]),
      endLabel: nodeLabel(day.nodes[day.nodes.length - 1]),
      startIsHub: day.nodes[0].kind === 'hub',
      endIsHub: day.nodes[day.nodes.length - 1].kind === 'hub',
      poiNames: day.nodes.filter((n) => n.kind === 'poi').map(nodeLabel),
      schedule: buildDaySchedule(day),
      minutes, cost, currency, transfers, allReal, pending,
    }
  })
)

const hasPending = computed(() => pendingEdges.value.length > 0)
const updating = ref(false)

// ---------- 几何微调 v1：只允许在「同城」的内部边上加/删点，城际那一段锁死 ----------
function addPoiToDay(poi, day) {
  const interior = []
  for (let i = 0; i < day.nodes.length - 1; i++) {
    if (!edgeIsIntercity(day.nodes[i], day.nodes[i + 1])) interior.push(i)
  }
  if (!interior.length) return
  let best = { i: interior[0], cost: Infinity }
  for (const i of interior) {
    const a = coordOf(day.nodes[i])
    const b = coordOf(day.nodes[i + 1])
    if (!a || !b) continue
    const cost = haversineKm(a, poi) + haversineKm(poi, b) - haversineKm(a, b)
    if (cost < best.cost) best = { i, cost }
  }
  day.nodes.splice(best.i + 1, 0, { kind: 'poi', id: poi.id })
  day.poi_ids = day.nodes.filter((n) => n.kind === 'poi').map((n) => n.id)
  map.closePopup()
  renderAll()
}

function removeNode(day, nodeIndex) {
  const node = day.nodes[nodeIndex]
  if (node.kind !== 'poi') return
  const prev = day.nodes[nodeIndex - 1]
  const next = day.nodes[nodeIndex + 1]
  if (edgeIsIntercity(prev, node) || edgeIsIntercity(node, next)) {
    window.alert("Can't remove the first/last stop of a transfer day — it anchors the intercity leg.")
    return
  }
  day.nodes.splice(nodeIndex, 1)
  day.poi_ids = day.nodes.filter((n) => n.kind === 'poi').map((n) => n.id)
  map.closePopup()
  renderAll()
}

async function updateTransit() {
  const pending = pendingEdges.value
  if (!pending.length) return
  updating.value = true
  try {
    const pairs = pending.map((e) => {
      const a = coordOf(e.a)
      const b = coordOf(e.b)
      return {
        from: { id: e.a.id, kind: e.a.kind, lat: a.lat, lng: a.lng },
        to: { id: e.b.id, kind: e.b.kind, lat: b.lat, lng: b.lng },
        intercity: e.intercity,
      }
    })
    const res = await transitLegs(pairs)
    for (const leg of res.legs) {
      legCache.set(`${leg.from_kind}:${leg.from_id}=>${leg.to_kind}:${leg.to_id}`, leg.route)
    }
    renderAll()
  } finally {
    updating.value = false
  }
}

async function downloadPng() {
  try {
    const dataUrl = await toPng(mapEl.value, { cacheBust: true, pixelRatio: 2 })
    const link = document.createElement('a')
    link.download = 'alontrip-route.png'
    link.href = dataUrl
    link.click()
  } catch {
    window.alert('Download failed — the map tiles likely blocked this by CORS. Please take a screenshot instead.')
  }
}

defineExpose({ daySummaries, dayVisible, hasPending, pendingCount: computed(() => pendingEdges.value.length), updating, updateTransit, downloadPng })

// ---------- Leaflet ----------
const mapEl = ref(null)
let map = null
const grayLayer = L.layerGroup()
const hotelLayer = L.layerGroup()
const hubLayer = L.layerGroup()
const dayLayers = {}
let legendControl = null

function summaryLine(r) {
  if ((r.legs || []).some((l) => l.travel_mode === 'taxi') || r.estimated) {
    return `No public transit · taxi ${fmt(r.total_duration_min)} · ${money(r.total_cost, r.currency)}`
  }
  const lines = (r.legs || []).filter((l) => l.travel_mode === 'transit' && l.line_name)
  const names = lines.map((l) => l.line_name).join(' → ') || 'Transit'
  return `${names} · ${fmt(r.total_duration_min)} · ${money(r.total_cost, r.currency)}`
}

function routeDetail(r, fromLabel, toLabel) {
  const head = `<div class="rp-head">${escapeHtml(fromLabel)} → ${escapeHtml(toLabel)}</div>`
  if (r.estimated) {
    return `${head}
      <div class="rp-est">No public transit found. Taxi estimate:</div>
      <div class="rp-total">${fmt(r.total_duration_min)} · ${money(r.total_cost, r.currency)}</div>`
  }
  const legs = (r.legs || [])
    .map((l) => {
      if (l.travel_mode === 'walking') return `<div class="rp-walk">Walk ${l.duration_min} min</div>`
      const stops = l.stop_count ? ` · ${l.stop_count} stop${l.stop_count > 1 ? 's' : ''}` : ''
      const times = l.start_time && l.end_time ? `${escapeHtml(l.start_time)} – ${escapeHtml(l.end_time)}` : ''
      return `<div class="rp-leg">
          <div class="rp-line">${escapeHtml(l.line_name || 'Transit')}</div>
          <div class="rp-stops">${escapeHtml(l.from_stop)} → ${escapeHtml(l.to_stop)}${stops}</div>
          ${times ? `<div class="rp-time">${times}</div>` : ''}
        </div>`
    })
    .join('')
  const transfers = r.transfer_count ? ` · ${r.transfer_count} transfer${r.transfer_count > 1 ? 's' : ''}` : ' · direct'
  const freq = r.frequency ? ` · ${escapeHtml(r.frequency)}` : ''
  const total = `<div class="rp-total">${fmt(r.total_duration_min)} · ${money(r.total_cost, r.currency)}${transfers}${freq}</div>`
  const alts = (r.alternatives || []).filter((a) => a.duration_min <= r.total_duration_min * 1.6).slice(0, 3)
  let compare = ''
  if (alts.length) {
    const rows = alts
      .map((a) => {
        const dMin = a.duration_min - r.total_duration_min
        const dCost = Math.round(a.cost - r.total_cost)
        const delta = dCost === 0 ? 'same fare' : dCost > 0 ? `+${money(dCost, r.currency)}` : `saves ${money(-dCost, r.currency)}`
        const timeDelta = dMin === 0 ? 'same time' : dMin > 0 ? `+${dMin} min` : `${dMin} min`
        const tone = dCost < 0 ? 'is-cheaper' : dCost > 0 ? 'is-pricier' : ''
        return `<div class="rp-alt">
            <span class="rp-alt-line">${escapeHtml(a.line_summary || 'Transit')}</span>
            <span class="rp-alt-delta ${tone}">${timeDelta} · ${delta}</span>
          </div>`
      })
      .join('')
    compare = `<div class="rp-alts"><div class="rp-alts-head">Other options</div>${rows}</div>`
  }
  return head + legs + total + compare
}

function buildGrayPopup(poi) {
  const el = document.createElement('div')
  el.className = 'poi-popup'
  const title = document.createElement('div')
  title.className = 'rp-head'
  title.textContent = `${poi.name_en} · ★ ${poi.rating?.toFixed?.(1) ?? '–'}`
  el.appendChild(title)
  const hint = document.createElement('div')
  hint.className = 'poi-popup-hint'
  hint.textContent = 'Not in your trip. Add it to a day:'
  el.appendChild(hint)
  const candidates = days.filter((d) => d.city === poi.city)
  if (!candidates.length) {
    const none = document.createElement('div')
    none.className = 'poi-popup-hint'
    none.textContent = '(no day in this city)'
    el.appendChild(none)
  }
  for (const day of candidates) {
    const btn = document.createElement('button')
    btn.className = 'poi-popup-btn'
    btn.textContent = `Add to Day ${day.day}`
    btn.addEventListener('click', () => addPoiToDay(poi, day))
    el.appendChild(btn)
  }
  return el
}

function buildPoiPopup(day, nodeIndex, poi) {
  const el = document.createElement('div')
  el.className = 'poi-popup'
  const title = document.createElement('div')
  title.className = 'rp-head'
  title.textContent = `${poi.name_en} · Day ${day.day}`
  el.appendChild(title)
  const btn = document.createElement('button')
  btn.className = 'poi-popup-btn poi-popup-btn-danger'
  btn.textContent = 'Remove from this day'
  btn.addEventListener('click', () => removeNode(day, nodeIndex))
  el.appendChild(btn)
  return el
}

function arrowIcon(color, angle) {
  // Bake rotation into the HTML. requestAnimationFrame after addTo() often
  // misses once clearLayers() has recycled the pane (T-021).
  const deg = ((angle % 360) + 360) % 360
  return L.divIcon({
    className: 'route-arrow-icon',
    html: `<div class="route-arrow" style="color:${color};transform:rotate(${deg}deg);transform-origin:50% 50%"></div>`,
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  })
}

// Leaflet draws polylines as straight segments in projected (Web Mercator)
// pixel space, not as great-circle arcs in raw lat/lng — so the naive
// lat/lng-average midpoint + spherical bearing (haversine-style) can land
// visibly off the rendered line for long segments (e.g. intercity legs).
// Projecting both endpoints, averaging in pixel space, and un-projecting
// back keeps the arrow exactly on the line at any zoom (the projection is
// a fixed scale per zoom, so the choice of zoom used here doesn't matter —
// midpoint and angle come out the same either way).
function projectedMidAndAngle(ca, cb) {
  const zoom = map.getZoom()
  const pa = map.project([ca.lat, ca.lng], zoom)
  const pb = map.project([cb.lat, cb.lng], zoom)
  const mid = map.unproject(pa.add(pb).divideBy(2), zoom)
  const dx = pb.x - pa.x
  const dy = pb.y - pa.y
  const angle = (Math.atan2(dx, -dy) * 180) / Math.PI // 0 = up/north on screen, clockwise+
  return { mid, angle: (angle + 360) % 360 }
}

function renderGray() {
  grayLayer.clearLayers()
  for (const poi of grayPois.value) {
    const marker = L.circleMarker([poi.lat, poi.lng], {
      radius: 5, color: '#8b949e', weight: 1.5, fillColor: '#6e7681', fillOpacity: 0.75,
    })
    marker.bindTooltip(`${escapeHtml(poi.name_en)} (not in trip)`, { direction: 'top' })
    marker.bindPopup(buildGrayPopup(poi))
    marker.addTo(grayLayer)
  }
}

function renderHotels() {
  hotelLayer.clearLayers()
  const seen = new Set()
  for (const day of days) {
    for (const node of day.nodes) {
      if (node.kind !== 'lodging' || seen.has(node.id)) continue
      seen.add(node.id)
      const lodging = lodgingById.value[node.id]
      if (!lodging) continue
      const marker = L.marker([lodging.lat, lodging.lng], { icon: hotelIcon() })
      marker.bindTooltip(shortLabel(lodging.name), {
        permanent: true, direction: 'right', offset: [10, -6], className: 'map-label map-label-hotel',
      })
      marker.bindPopup(
        `<div class="rp-head">${escapeHtml(lodging.name)}</div><div class="poi-popup-hint">${escapeHtml(lodging.area || '')}</div><div class="poi-popup-hint">Fixed — hotels can't be moved or removed here.</div>`
      )
      marker.addTo(hotelLayer)
    }
  }
}

function hotelIcon() {
  return L.divIcon({ className: 'hotel-pin-icon', html: '<div class="hotel-pin">⌂</div>', iconSize: [22, 22], iconAnchor: [11, 20] })
}

// T-016: the arrival/departure hub, if the traveller picked one — at most
// two markers total, so no per-day loop like renderHotels()/renderDay()
// need; each is a fixed anchor (never editable, same as a hotel).
function renderHubs() {
  hubLayer.clearLayers()
  for (const hub of [props.arrivalHub, props.departureHub]) {
    if (!hub) continue
    const symbol = hub.kind === 'airport' ? '✈' : '🚉'
    const marker = L.marker([hub.lat, hub.lng], { icon: hubIcon(symbol) })
    marker.bindTooltip(shortLabel(hub.name_en || hub.name), {
      permanent: true, direction: 'right', offset: [12, -6], className: 'map-label map-label-hub',
    })
    marker.bindPopup(
      `<div class="rp-head">${escapeHtml(hub.name_en || hub.name)}</div><div class="poi-popup-hint">${hub.kind}</div><div class="poi-popup-hint">Fixed — the trip's arrival/departure anchor.</div>`
    )
    marker.addTo(hubLayer)
  }
}

function hubIcon(symbol) {
  return L.divIcon({ className: 'hub-pin-icon', html: `<div class="hub-pin">${symbol}</div>`, iconSize: [26, 26], iconAnchor: [13, 22] })
}

// Permanent tooltips stay on-screen for every marker, so keep them short —
// full names still show in the popup/hover title on click.
function shortLabel(name, max = 22) {
  const text = String(name || '')
  return escapeHtml(text.length > max ? `${text.slice(0, max - 1)}…` : text)
}

function renderDay(day) {
  const group = dayLayers[day.day]
  group.clearLayers()
  const color = DAY_COLORS[(day.day - 1) % DAY_COLORS.length]
  const bounds = []
  day.nodes.forEach((node, i) => {
    if (node.kind !== 'poi') return
    const poi = poiById.value[node.id]
    if (!poi) return
    bounds.push([poi.lat, poi.lng])
    const marker = L.circleMarker([poi.lat, poi.lng], {
      radius: 8, color, weight: 2, fillColor: color, fillOpacity: 0.9,
    })
    // Permanent label (not just hover) so the itinerary reads at a glance —
    // the dot's own color already says which day, so the label just needs
    // the name; full name + day still shows in the click popup.
    marker.bindTooltip(shortLabel(poi.name_en), {
      permanent: true, direction: 'top', offset: [0, -8], className: 'map-label map-label-poi',
    })
    marker.bindPopup(buildPoiPopup(day, i, poi))
    marker.addTo(group)
  })
  for (let i = 0; i < day.nodes.length - 1; i++) {
    const a = day.nodes[i]
    const b = day.nodes[i + 1]
    const ca = coordOf(a)
    const cb = coordOf(b)
    if (!ca || !cb) continue
    bounds.push([ca.lat, ca.lng], [cb.lat, cb.lng])
    const key = edgeKey(a, b)
    const route = legCache.get(key)
    const intercity = edgeIsIntercity(a, b)
    const pending = !route
    const line = L.polyline([[ca.lat, ca.lng], [cb.lat, cb.lng]], {
      color,
      weight: pending ? 2.5 : 3.5,
      opacity: pending ? 0.35 : 0.85,
      dashArray: intercity ? '2 10' : pending ? '5 7' : null,
    })
    if (pending) {
      line.bindTooltip('Not updated — click "Update transit"', { sticky: true })
    } else {
      line.bindTooltip(`${intercity ? 'Intercity · ' : ''}${summaryLine(route)}`, { sticky: true })
      line.bindPopup(routeDetail(route, nodeLabel(a), nodeLabel(b)), { className: 'route-popup', maxWidth: 320 })
    }
    line.addTo(group)
    const { mid, angle: brg } = projectedMidAndAngle(ca, cb)
    const arrowMarker = L.marker([mid.lat, mid.lng], {
      icon: arrowIcon(color, brg),
      interactive: false,
      opacity: pending ? 0.35 : 1,
    })
    arrowMarker.addTo(group)
  }
  return bounds
}

function renderLegend() {
  if (legendControl) { legendControl.remove(); legendControl = null }
  legendControl = L.control({ position: 'bottomleft' })
  legendControl.onAdd = () => {
    const div = L.DomUtil.create('div', 'map-legend')
    L.DomEvent.disableClickPropagation(div)
    for (const day of days) {
      const color = DAY_COLORS[(day.day - 1) % DAY_COLORS.length]
      const row = document.createElement('label')
      row.className = 'legend-row'
      const cb = document.createElement('input')
      cb.type = 'checkbox'
      cb.checked = dayVisible[day.day]
      cb.addEventListener('change', () => {
        dayVisible[day.day] = cb.checked
        applyVisibility()
      })
      row.appendChild(cb)
      const dot = document.createElement('span')
      dot.className = 'day-dot'
      dot.style.background = color
      row.appendChild(dot)
      row.appendChild(document.createTextNode(`Day ${day.day} — ${day.city}`))
      div.appendChild(row)
    }
    const note = document.createElement('div')
    note.className = 'legend-note'
    note.innerHTML = '<span class="hotel-pin legend-icon">⌂</span> hotel &nbsp; <span class="hub-pin legend-icon">✈</span> arrival/departure &nbsp; <span class="day-dot" style="background:#6e7681"></span> not in trip'
    div.appendChild(note)
    return div
  }
  legendControl.addTo(map)
}

function applyVisibility() {
  for (const day of days) {
    const group = dayLayers[day.day]
    if (!group) continue
    if (dayVisible[day.day]) { if (!map.hasLayer(group)) group.addTo(map) }
    else if (map.hasLayer(group)) map.removeLayer(group)
  }
}

let firstRender = true
function renderAll() {
  if (!map) return
  renderGray()
  renderHotels()
  renderHubs()
  let allBounds = []
  for (const hub of [props.arrivalHub, props.departureHub]) {
    if (hub) allBounds.push([hub.lat, hub.lng])
  }
  for (const day of days) {
    if (!dayLayers[day.day]) dayLayers[day.day] = L.layerGroup()
    allBounds = allBounds.concat(renderDay(day))
  }
  applyVisibility()
  renderLegend()
  if (firstRender && allBounds.length) {
    map.fitBounds(allBounds, { padding: [50, 50], maxZoom: 15 })
    firstRender = false
  }
}

function applyGrayZoomVisibility() {
  const zoom = map.getZoom()
  if (zoom < 11) { if (map.hasLayer(grayLayer)) map.removeLayer(grayLayer) }
  else if (!map.hasLayer(grayLayer)) grayLayer.addTo(map)
}

onMounted(() => {
  map = L.map(mapEl.value, { preferCanvas: true })
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 19,
    crossOrigin: true,
  }).addTo(map)
  map.setView([35.68, 139.69], 12)
  hotelLayer.addTo(map)
  hubLayer.addTo(map)
  map.on('zoomend', applyGrayZoomVisibility)
  renderAll()
  applyGrayZoomVisibility()
})

onBeforeUnmount(() => {
  if (map) map.remove()
})

watch(dayVisible, applyVisibility, { deep: true })
</script>

<template>
  <div class="map-wrap">
    <div id="map" ref="mapEl"></div>
  </div>
</template>
