<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter, onBeforeRouteLeave } from 'vue-router'
import BrandHeader from '../components/BrandHeader.vue'
import ResultMap from '../components/ResultMap.vue'
import { fetchPois } from '../api'
import { money } from '../money'

const router = useRouter()
const result = ref(null)
const poisByCity = ref({})
const loadingPois = ref(true)
const resultMapRef = ref(null)

try {
  const raw = sessionStorage.getItem('alontrip.result')
  if (raw) result.value = JSON.parse(raw)
} catch {
  result.value = null
}

const daySummaries = computed(() => resultMapRef.value?.daySummaries ?? [])
const hasPending = computed(() => resultMapRef.value?.hasPending ?? false)
const pendingCount = computed(() => resultMapRef.value?.pendingCount ?? 0)
const updating = computed(() => resultMapRef.value?.updating ?? false)
const liveTotals = computed(() => {
  const days = daySummaries.value
  if (!days.length) return null
  return {
    minutes: days.reduce((a, d) => a + d.minutes, 0),
    cost: days.reduce((a, d) => a + d.cost, 0),
    transfers: days.reduce((a, d) => a + d.transfers, 0),
    currency: days[0].currency,
    allReal: days.every((d) => d.allReal),
  }
})

onMounted(async () => {
  if (!result.value) return
  loadingPois.value = true
  try {
    const entries = await Promise.all(
      (result.value.cities || []).map(async (city) => [city, (await fetchPois(city)).pois])
    )
    poisByCity.value = Object.fromEntries(entries)
  } catch {
    poisByCity.value = {}
  } finally {
    loadingPois.value = false
  }
})

function confirmLeaveIfDirty() {
  if (!hasPending.value) return true
  return window.confirm(
    `You have ${pendingCount.value} transit segment${pendingCount.value === 1 ? '' : 's'} not updated yet. Leave anyway?`
  )
}

onBeforeRouteLeave(() => confirmLeaveIfDirty())

function onBeforeUnload(e) {
  if (!hasPending.value) return
  e.preventDefault()
  e.returnValue = ''
}
onMounted(() => window.addEventListener('beforeunload', onBeforeUnload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', onBeforeUnload))

function backToPlan() {
  router.push('/')
}

async function onUpdateTransit() {
  await resultMapRef.value?.updateTransit()
}

async function onDownload() {
  await resultMapRef.value?.downloadPng()
}

const fmt = (min) => (min >= 60 ? `${Math.floor(min / 60)}h ${min % 60}m` : `${min} min`)

function ticketLine(ev) {
  if (ev.cost == null) return 'ticket unknown'
  if (ev.cost === 0) return 'free'
  return money(ev.cost, ev.currency)
}

function scheduleMeta(ev) {
  if (ev.kind === 'transit') {
    const bits = [ev.line_summary]
    if (ev.cost != null && ev.cost > 0) bits.push(money(ev.cost, ev.currency))
    bits.push(fmt(ev.duration_min))
    return bits.join(' · ')
  }
  if (ev.kind === 'visit') return `${ticketLine(ev)} · ${fmt(ev.duration_min)}`
  return fmt(ev.duration_min)
}
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <BrandHeader tagline="Your itinerary" />

      <div v-if="!result" class="empty-result">
        <p>No route yet — generate one from the planning page first.</p>
        <button class="optimize" @click="backToPlan">Back to plan</button>
      </div>

      <template v-else>
        <div class="results-header">
          <span>ITINERARY</span>
          <span class="muted">{{ result.grouper }} · {{ result.transit_provider }}</span>
        </div>

        <div v-if="liveTotals" class="trip-total">
          <div class="trip-total-row">
            <span class="trip-total-label">Total transit</span>
            <span class="trip-total-value">
              {{ fmt(liveTotals.minutes) }} ·
              {{ money(liveTotals.cost, liveTotals.currency) }}
            </span>
          </div>
          <div class="trip-total-note">
            {{ liveTotals.transfers }} transfer{{ liveTotals.transfers === 1 ? '' : 's' }}
            across {{ daySummaries.length }} day{{ daySummaries.length === 1 ? '' : 's' }}
            <template v-if="liveTotals.allReal">
              · <span class="badge-real">real timetable data</span>
            </template>
            <template v-else>
              · <span class="badge-est">partly estimated</span>
            </template>
          </div>
        </div>

        <div v-if="result.warnings && result.warnings.length" class="warning-banner">
          <div v-for="(w, i) in result.warnings" :key="i">{{ w }}</div>
        </div>

        <div v-if="hasPending" class="warning-banner pending-banner">
          {{ pendingCount }} segment{{ pendingCount === 1 ? '' : 's' }} changed and not queried yet.
        </div>

        <button class="optimize update-btn" :disabled="!hasPending || updating" @click="onUpdateTransit">
          {{ updating ? 'Updating…' : `Update transit${hasPending ? ` (${pendingCount})` : ''}` }}
        </button>

        <div v-for="day in daySummaries" :key="day.day" class="day-card">
          <div class="day-title">Day {{ day.day }} — {{ day.city }}</div>
          <ol class="day-schedule">
            <li
              v-for="(ev, i) in day.schedule"
              :key="i"
              class="sched-row"
              :class="'sched-' + ev.kind"
            >
              <span class="sched-time">{{ ev.start }}</span>
              <div class="sched-body">
                <div class="sched-title">{{ ev.title }}</div>
                <div class="sched-meta">{{ scheduleMeta(ev) }}</div>
              </div>
            </li>
          </ol>
          <div class="day-stats">
            {{ fmt(day.minutes) }} · {{ money(day.cost, day.currency) }}
            <template v-if="day.transfers">
              · {{ day.transfers }} transfer{{ day.transfers === 1 ? '' : 's' }}
            </template>
            <template v-else> · direct</template>
            <span v-if="day.pending" class="badge-est"> · {{ day.pending }} pending</span>
            <span v-else-if="!day.allReal" class="badge-est"> · estimated</span>
          </div>
        </div>

        <div class="result-actions">
          <button class="optimize" @click="onDownload">Download map</button>
          <button class="optimize back-btn" @click="backToPlan">Back to plan</button>
        </div>
      </template>
    </aside>
    <ResultMap
      v-if="result && !loadingPois"
      ref="resultMapRef"
      :itinerary="result.itinerary"
      :pois-by-city="poisByCity"
      :arrival-hub="result.arrival_hub"
      :departure-hub="result.departure_hub"
    />
    <div v-else-if="result" class="map-wrap result-map-placeholder">
      <div class="placeholder-note">Loading map data…</div>
    </div>
    <div v-else class="map-wrap result-map-placeholder">
      <div class="placeholder-note">No itinerary to show yet.</div>
    </div>
  </div>
</template>
