<script setup>
// T-015 result map: colored dots (per day) + gray dots (in catalog, unselected)
// + hotel endpoints + arrowed polylines + dashed intercity legs + per-day
// toggles + geometry tweaks (add gray / remove colored) + Update transit
// (re-queries only changed legs) + PNG export.
// The planner's simple preview stays in MapView.vue; the two needs diverged,
// so separate components are easier to maintain.
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import L from 'leaflet'
import { DAY_COLORS } from '../colors'
import { haversineKm } from '../geo'
import { transitLegs } from '../api'
import { money, toUsd } from '../money'
import { sourceLabel } from '../transitSource'
import { clipVisit } from '../hours'
import { fmt, scheduleMeta } from '../scheduleText'
import { addDarkBasemap } from '../basemap'
import { cityDisplayName } from '../cityNames'
import { exportTripImages } from '../exportCards'

const props = defineProps({
  itinerary: { type: Array, required: true },
  poisByCity: { type: Object, required: true }, // { city: [poi, ...] } — full catalog for every city in the trip
  // T-016: at most two hub nodes ever exist in the whole trip — day 1's
  // start (arrival) and the last day's end (departure) — so these come in
  // as plain objects, not a per-city catalog like pois/lodgings.
  arrivalHub: { type: Object, default: null },
  departureHub: { type: Object, default: null },
  // T-034: minutes-from-midnight — day 1 sightseeing start (landing + 90)
  // and the last day's latest sightseeing end (takeoff − 120). Null when no
  // flight time was given; the 22:00 hard cap still applies either way.
  arrivalStartMin: { type: Number, default: null },
  departureCutoffMin: { type: Number, default: null },
})

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

// ---------- Editable copy of the itinerary (deep copy; the sessionStorage original stays untouched) ----------
const days = reactive(JSON.parse(JSON.stringify(props.itinerary)))
const dayVisible = reactive(Object.fromEntries(days.map((d) => [d.day, true])))
// legCache keys are edgeKey() strings; a hit means that leg already has real/estimated transit data to draw.
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
// Which city each hotel belongs to: on a city-change night you are already
// sleeping in the next city (plan 6.1b E), so record it under that day's
// city — the morning hotel (previous city) gets overwritten by the later
// day's entry, which is the correct answer.
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
// Gray dots: catalog spots of the selected cities not currently in any day —
// computed live rather than trusting the server's initial unselected_poi_ids,
// so the set tracks add/remove edits in real time.
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

function sourceBadge(route) {
  return sourceLabel(route?.data_source)
}

// Mirror backend schedule.py so "Update transit" / add-remove POI keep the
// clock honest instead of freezing the first optimize-route payload.
// T-034: arrival-day clock starts after landing; every day is capped at
// 22:00, and the departure day additionally at the flight cutoff.
function buildDaySchedule(day) {
  const chain = day.nodes
  const events = []
  const isFirst = day.day === 1
  const isLast = day.day === Math.max(...days.map((d) => d.day))
  let clock = isFirst && props.arrivalStartMin != null ? props.arrivalStartMin : 9 * 60
  let cap = 22 * 60
  if (isLast && props.departureCutoffMin != null) cap = Math.min(cap, props.departureCutoffMin)
  let lunchDone = false
  let dinnerDone = false
  const poiCount = chain.filter((n) => n.kind === 'poi').length
  const hasArrival = chain[0]?.kind === 'hub'
  // T-040 dinner rule (mirrors schedule.py): target 18:00; early finishers
  // wait until 18:00, late finishers eat right away (no 19:30 upper window).
  const dinnerEarliest = 18 * 60

  const addDinner = () => {
    if (clock + 90 > cap) {
      dinnerDone = true // past the day's cutoff — skip, don't late-night it
      return
    }
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
      data_source: route?.data_source || '',
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
      const clipped = clipVisit(clock, remain, poi?.opening_hours)
      clock = clipped.clock
      remain = clipped.remain
      // Hard day cap (22:00, or the departure-day flight cutoff):
      // never start or extend a visit past it.
      if (clock >= cap) remain = 0
      else remain = Math.min(remain, cap - clock)
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
      if (!dinnerDone && !morePois) {
        if (clock < dinnerEarliest) clock = dinnerEarliest
        addDinner() // skipped entirely when it would run past the cap
      }
    }
  }
  const endsAtHub = chain[chain.length - 1]?.kind === 'hub'
  if (poiCount && !dinnerDone && !endsAtHub) {
    if (clock < dinnerEarliest) clock = dinnerEarliest
    addDinner()
  }
  return events
}

// Per-day summary (for the result sidebar): minutes/fare/transfers recomputed
// from the current legCache; unupdated legs are excluded and counted
// separately, so the sidebar can report "N segments not updated yet".
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

// ---------- Geometry tweaks v1: add/remove points only on same-city interior edges; the intercity leg stays locked ----------
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

// ---------- Rich export (T-043): 1 full-trip image + N day cards ----------
// This only assembles the plain data model exportCards.js needs (resolving
// coords/colors/numbers relies on this component's closures such as
// coordOf/legCache); DOM structure and temp-map lifecycle live in exportCards.
function buildExportModel() {
  const summaries = daySummaries.value
  const dayModels = days.map((day) => {
    const color = DAY_COLORS[(day.day - 1) % DAY_COLORS.length]
    const summary = summaries.find((s) => s.day === day.day)
    // Itinerary order of that day's POIs, shared by the numbered pins and the
    // card rows; the two visit rows split by lunch have the same title → same
    // number, which is correct.
    const poiOrder = new Map()
    day.nodes.forEach((node) => {
      if (node.kind === 'poi') poiOrder.set(nodeLabel(node), poiOrder.size + 1)
    })
    const schedule = (summary?.schedule || []).map((ev) => ({
      kind: ev.kind,
      start: ev.start,
      title: ev.title,
      meta: scheduleMeta(ev),
      num: ev.kind === 'visit' ? poiOrder.get(ev.title) ?? null : null,
    }))
    const statsBits = [
      fmt(summary?.minutes ?? 0),
      money(summary?.cost ?? 0, summary?.currency || 'USD'),
      summary?.transfers ? `${summary.transfers} transfer${summary.transfers === 1 ? '' : 's'}` : 'direct',
    ]
    let statsText = statsBits.join(' · ')
    if (summary?.pending) statsText += ` · ${summary.pending} pending`

    const pins = []
    const legs = []
    const fit = []
    const seenLodging = new Set()
    for (const node of day.nodes) {
      const coord = coordOf(node)
      if (!coord) continue
      fit.push([coord.lat, coord.lng])
      if (node.kind === 'poi') {
        pins.push({ lat: coord.lat, lng: coord.lng, color, n: poiOrder.get(nodeLabel(node)), type: 'poi', name: nodeLabel(node) })
      } else if (node.kind === 'lodging') {
        if (seenLodging.has(node.id)) continue
        seenLodging.add(node.id)
        pins.push({ lat: coord.lat, lng: coord.lng, color, n: null, type: 'hotel', name: lodgingById.value[node.id]?.name || node.id })
      } else {
        const hub = hubById.value[node.id]
        pins.push({ lat: coord.lat, lng: coord.lng, color, n: null, type: 'hub', symbol: hub?.kind === 'airport' ? '✈' : '🚉', name: hub ? hub.name_en || hub.name : node.id })
      }
    }
    for (let i = 0; i < day.nodes.length - 1; i++) {
      const a = day.nodes[i]
      const b = day.nodes[i + 1]
      const ca = coordOf(a)
      const cb = coordOf(b)
      if (!ca || !cb) continue
      legs.push({ from: ca, to: cb, intercity: edgeIsIntercity(a, b), pending: !legCache.has(edgeKey(a, b)), color })
    }
    return {
      day: day.day,
      city: cityDisplayName(day.city),
      color,
      title: `Day ${day.day} — ${cityDisplayName(day.city)}`,
      schedule,
      statsText,
      pins,
      legs,
      fit,
    }
  })
  const cityLine = [...new Set(dayModels.map((ds) => ds.city))].join(' → ')
  return {
    tripTitle: `${days.length} day${days.length === 1 ? '' : 's'} · ${cityLine}`,
    fit: dayModels.flatMap((ds) => ds.fit),
    days: dayModels,
  }
}

const exporting = ref(false)
const exportProgress = ref('')

async function exportImages() {
  if (exporting.value) return
  exporting.value = true
  exportProgress.value = ''
  try {
    const { ok, failed } = await exportTripImages(buildExportModel(), (i, total) => {
      exportProgress.value = `${i}/${total}`
    })
    if (ok === 0) {
      window.alert('Download failed — the map tiles likely blocked this by CORS. Please take a screenshot instead.')
    } else if (failed.length) {
      window.alert(`Exported ${ok} image${ok === 1 ? '' : 's'}; ${failed.length} failed (${failed.map((f) => f.label).join(', ')}). Click export again to retry.`)
    }
  } finally {
    exporting.value = false
  }
}

defineExpose({ daySummaries, dayVisible, hasPending, pendingCount: computed(() => pendingEdges.value.length), updating, updateTransit, exportImages, exporting, exportProgress })

// ---------- Leaflet ----------
const mapEl = ref(null)
let map = null
const grayLayer = L.layerGroup()
const hotelLayer = L.layerGroup()
const hubLayer = L.layerGroup()
const dayLayers = {}
let legendControl = null

function summaryLine(r) {
  const src = sourceBadge(r)
  const srcBit = src ? ` · ${src}` : ''
  if ((r.legs || []).some((l) => l.travel_mode === 'taxi') || r.estimated) {
    return `No public transit · taxi ${fmt(r.total_duration_min)} · ${money(r.total_cost, r.currency)}${srcBit}`
  }
  const lines = (r.legs || []).filter((l) => l.travel_mode === 'transit' && l.line_name)
  const names = lines.map((l) => l.line_name).join(' → ') || 'Transit'
  return `${names} · ${fmt(r.total_duration_min)} · ${money(r.total_cost, r.currency)}${srcBit}`
}

function routeDetail(r, fromLabel, toLabel) {
  const head = `<div class="rp-head">${escapeHtml(fromLabel)} → ${escapeHtml(toLabel)}</div>`
  if (r.estimated) {
    const src = sourceBadge(r)
    const srcBit = src ? ` · ${src}` : ''
    return `${head}
      <div class="rp-est">No public transit found. Taxi estimate:</div>
      <div class="rp-total">${fmt(r.total_duration_min)} · ${money(r.total_cost, r.currency)}${srcBit}</div>`
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
        permanent: true, direction: 'right', offset: [12, -6], className: 'map-label map-label-hotel',
      })
      marker.bindPopup(
        `<div class="rp-head">${escapeHtml(lodging.name)}</div><div class="poi-popup-hint">${escapeHtml(lodging.area || '')}</div><div class="poi-popup-hint">Fixed — hotels can't be moved or removed here.</div>`
      )
      marker.options._rank = 1000000 + day.day
      bindLabelReveal(marker)
      marker.addTo(hotelLayer)
    }
  }
}

function hotelIcon() {
  return L.divIcon({
    className: 'hotel-pin-icon',
    html: '<div class="hotel-pin"><svg class="hotel-pin-glyph" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8 2.2 2.4 7.1v6.5h3.5V9.2h4.2v4.4h3.5V7.1L8 2.2z"/></svg></div>',
    iconSize: [26, 26],
    iconAnchor: [13, 22],
  })
}

// T-016: the arrival/departure hub, if the traveller picked one — at most
// two markers total, so no per-day loop like renderHotels()/renderDay()
// need; each is a fixed anchor (never editable, same as a hotel).
function renderHubs() {
  hubLayer.clearLayers()
  for (const [hi, hub] of [props.arrivalHub, props.departureHub].entries()) {
    if (!hub) continue
    const symbol = hub.kind === 'airport' ? '✈' : '🚉'
    const marker = L.marker([hub.lat, hub.lng], { icon: hubIcon(symbol) })
    marker.bindTooltip(shortLabel(hub.name_en || hub.name), {
      permanent: true, direction: 'right', offset: [12, -6], className: 'map-label map-label-hub',
    })
    marker.bindPopup(
      `<div class="rp-head">${escapeHtml(hub.name_en || hub.name)}</div><div class="poi-popup-hint">${hub.kind}</div><div class="poi-popup-hint">Fixed — the trip's arrival/departure anchor.</div>`
    )
    marker.options._rank = 2000000 + hi
    bindLabelReveal(marker)
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
    marker.options._rank = day.day * 1000 + i
    bindLabelReveal(marker)
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
      row.appendChild(document.createTextNode(`Day ${day.day} — ${cityDisplayName(day.city)}`))
      div.appendChild(row)
    }
    const note = document.createElement('div')
    note.className = 'legend-note'
    note.innerHTML = '<span class="hotel-pin legend-icon"><svg class="hotel-pin-glyph" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8 2.2 2.4 7.1v6.5h3.5V9.2h4.2v4.4h3.5V7.1L8 2.2z"/></svg></span> hotel &nbsp; <span class="hub-pin legend-icon">✈</span> arrival/departure &nbsp; <span class="day-dot" style="background:#6e7681"></span> not in trip'
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
  resolveLabelCollisions()
}

// ---------- Permanent-label collision avoidance: greedy keep by screen rects ----------
// With many spots, permanent tooltips overlap. Greedily keep the labels that
// fit, by priority (hub > hotel > itinerary order, via the _rank set in the
// three render functions); losers are hidden entirely (.map-label--hidden),
// and .map-label--peek temporarily reveals them while their marker is
// hovered. Each pass strips all hidden classes before rebuilding the set, so
// re-renders (clearLayers swapping out tooltip elements) leave no dangling
// references. Pan/zoom needs no special handling: the pane transforms as a
// whole, labels keep their relative positions, and rects are final by the
// time zoomend/moveend fires.
let hiddenLabels = new Set()

function bindLabelReveal(marker) {
  marker.on('mouseover', () => {
    const el = marker.getTooltip()?.getElement()
    if (el && hiddenLabels.has(el)) el.classList.add('map-label--peek')
  })
  marker.on('mouseout', () => {
    marker.getTooltip()?.getElement()?.classList.remove('map-label--peek')
  })
}

function resolveLabelCollisions() {
  if (!map || document.hidden) return
  const entries = []
  const scan = (group) => {
    if (!group || !map.hasLayer(group)) return
    group.eachLayer((layer) => {
      // Only permanent tooltips have a rendered element; hover/sticky ones return null from getElement().
      const el = layer.getTooltip ? layer.getTooltip()?.getElement() : null
      if (!el) return
      entries.push({ el, rank: layer.options?._rank ?? 0 })
    })
  }
  scan(hubLayer)
  scan(hotelLayer)
  for (const day of days) {
    if (dayVisible[day.day]) scan(dayLayers[day.day])
  }
  entries.sort((a, b) => a.rank - b.rank)
  const PAD = 4
  const kept = []
  const nextHidden = new Set()
  for (const { el } of entries) {
    el.classList.remove('map-label--hidden', 'map-label--peek')
    const r = el.getBoundingClientRect()
    if (!r.width) continue
    const box = { l: r.left - PAD, t: r.top - PAD, r: r.right + PAD, b: r.bottom + PAD }
    if (kept.some((k) => box.l < k.r && box.r > k.l && box.t < k.b && box.b > k.t)) {
      el.classList.add('map-label--hidden')
      nextHidden.add(el)
    } else {
      kept.push(box)
    }
  }
  hiddenLabels = nextHidden
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
  resolveLabelCollisions()
}

function applyGrayZoomVisibility() {
  const zoom = map.getZoom()
  if (zoom < 11) { if (map.hasLayer(grayLayer)) map.removeLayer(grayLayer) }
  else if (!map.hasLayer(grayLayer)) grayLayer.addTo(map)
}

onMounted(() => {
  map = L.map(mapEl.value, { preferCanvas: true })
  addDarkBasemap(map)
  map.setView([35.68, 139.69], 12)
  hotelLayer.addTo(map)
  hubLayer.addTo(map)
  map.on('zoomend', applyGrayZoomVisibility)
  map.on('zoomend moveend resize', resolveLabelCollisions)
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
