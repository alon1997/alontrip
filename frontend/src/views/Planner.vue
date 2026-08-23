<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRouter } from 'vue-router'
import BrandHeader from '../components/BrandHeader.vue'
import MapView from '../components/MapView.vue'
import {
  fetchCities,
  fetchPois,
  fetchLodgings,
  searchLodgings,
  fetchTransportHubs,
  fetchHealth,
  optimizeRoute,
} from '../api'
import { splitCityDays } from '../planner'

const router = useRouter()

const MAX_CITIES = 4
const DAY_OPTIONS = Array.from({ length: 13 }, (_, i) => i + 2) // 2..14
const DENSITY_CHOICES = [
  { id: 'none', label: 'none' },
  { id: 'few', label: 'few' },
]
const COUNTRY_ORDER = ['japan', 'china', 'korea']
const COUNTRY_LABELS = { japan: 'Japan', china: 'China', korea: 'Korea' }

const allCities = ref([])
const selectedCities = ref(['tokyo'])
const days = ref(5)
const hotelMode = ref('system_one')
const firstDayDensity = ref('few')
const lastDayDensity = ref('none')
const activeSpotCity = ref('tokyo')

const hubsByCity = reactive({})
const loadingHubsFor = reactive({})
const arrivalHubId = ref('')
const departureHubId = ref('')

const poisByCity = reactive({})
const loadingPoisFor = reactive({})
const poiSearch = reactive({})
const selectedPoiIds = ref([])

const lodgingsByCity = reactive({})
const loadingLodgingsFor = reactive({})
const lodgingSearchQuery = reactive({})
const customLodgingByDay = reactive({})
const lodgingSearchTimers = {}

const generating = ref(false)
const progressText = ref('')
const errorMsg = ref('')
const health = ref({ grouper: '', poi_provider: '' })

const PROGRESS_MESSAGES = ['Planning days…', 'Choosing hotels…', 'Fetching transit…']
let progressTimer = null

function cityName(id) {
  const c = allCities.value.find((x) => x.id === id)
  return c ? c.name_en : id
}

async function loadCities() {
  try {
    const data = await fetchCities()
    allCities.value = data.cities || []
  } catch (e) {
    errorMsg.value = `Failed to load cities: ${e.message}`
  }
}

// T-016：城市按国家分组展示，顺序固定 Japan/China/Korea，未知国家兜底放最后。
const groupedCities = computed(() => {
  const byCountry = {}
  for (const c of allCities.value) {
    const key = c.country || 'other'
    if (!byCountry[key]) byCountry[key] = []
    byCountry[key].push(c)
  }
  const order = [...COUNTRY_ORDER, ...Object.keys(byCountry).filter((k) => !COUNTRY_ORDER.includes(k))]
  return order.filter((k) => byCountry[k]?.length).map((k) => ({
    id: k,
    name: COUNTRY_LABELS[k] || k,
    cities: byCountry[k],
  }))
})

async function ensureCityHubs(city) {
  if (!city || hubsByCity[city] || loadingHubsFor[city]) return
  loadingHubsFor[city] = true
  try {
    const data = await fetchTransportHubs(city)
    hubsByCity[city] = data.hubs || []
  } catch {
    hubsByCity[city] = []
  } finally {
    loadingHubsFor[city] = false
  }
}

const firstCity = computed(() => selectedCities.value[0] || '')
const lastCity = computed(() => selectedCities.value[selectedCities.value.length - 1] || '')
const arrivalHubOptions = computed(() => hubsByCity[firstCity.value] || [])
const departureHubOptions = computed(() => hubsByCity[lastCity.value] || [])

// 只负责预取，不在这里清空当前选择——清空的时机交给 disabledReason 按「当前选择
// 是否还在候选列表里」判断，避开「城市变了但列表还没异步回来」那段时间窗口的竞态。
watch(firstCity, (city) => ensureCityHubs(city), { immediate: true })
watch(lastCity, (city) => ensureCityHubs(city), { immediate: true })

async function loadHealth() {
  try {
    health.value = await fetchHealth()
  } catch {
    // 健康检查失败不影响主流程，页脚只是留空
  }
}

async function ensureCityPois(city) {
  if (poisByCity[city] || loadingPoisFor[city]) return
  loadingPoisFor[city] = true
  try {
    const data = await fetchPois(city)
    poisByCity[city] = data.pois || []
  } catch (e) {
    poisByCity[city] = []
    errorMsg.value = `Failed to load spots for ${cityName(city)}: ${e.message}`
  } finally {
    loadingPoisFor[city] = false
  }
}

async function ensureCityLodgings(city) {
  if (lodgingsByCity[city] || loadingLodgingsFor[city]) return
  loadingLodgingsFor[city] = true
  try {
    const data = await fetchLodgings(city)
    lodgingsByCity[city] = data.lodgings || []
  } catch (e) {
    lodgingsByCity[city] = []
    errorMsg.value = `Failed to load lodgings for ${cityName(city)}: ${e.message}`
  } finally {
    loadingLodgingsFor[city] = false
  }
}

function onLodgingSearchInput(city, value) {
  lodgingSearchQuery[city] = value
  clearTimeout(lodgingSearchTimers[city])
  if (value.trim().length < 2) return
  lodgingSearchTimers[city] = setTimeout(async () => {
    try {
      const data = await searchLodgings(city, value.trim())
      const existing = lodgingsByCity[city] || []
      const existingIds = new Set(existing.map((l) => l.id))
      const extra = (data.results || []).filter((l) => !existingIds.has(l.id))
      lodgingsByCity[city] = [...existing, ...extra]
    } catch {
      // 搜索失败静默：目录列表仍可用，不打断用户
    }
  }, 400)
}

const poiCityMap = computed(() => {
  const map = {}
  for (const city of Object.keys(poisByCity)) {
    for (const p of poisByCity[city] || []) map[p.id] = city
  }
  return map
})

const allPoisFlat = computed(() => selectedCities.value.flatMap((c) => poisByCity[c] || []))

function filteredPois(city) {
  const list = poisByCity[city] || []
  const q = (poiSearch[city] || '').trim().toLowerCase()
  if (!q) return list
  return list.filter((p) => p.name.toLowerCase().includes(q) || p.name_en.toLowerCase().includes(q))
}

function toggleCity(id) {
  const idx = selectedCities.value.indexOf(id)
  if (idx >= 0) {
    selectedCities.value.splice(idx, 1)
    selectedPoiIds.value = selectedPoiIds.value.filter((pid) => poiCityMap.value[pid] !== id)
  } else {
    if (selectedCities.value.length >= MAX_CITIES) return
    selectedCities.value.push(id)
    activeSpotCity.value = id
    ensureCityPois(id)
    if (hotelMode.value === 'custom') ensureCityLodgings(id)
  }
}

function moveCity(id, dir) {
  const idx = selectedCities.value.indexOf(id)
  const swap = idx + dir
  if (swap < 0 || swap >= selectedCities.value.length) return
  const arr = [...selectedCities.value]
  ;[arr[idx], arr[swap]] = [arr[swap], arr[idx]]
  selectedCities.value = arr
}

function togglePoi(id) {
  const i = selectedPoiIds.value.indexOf(id)
  if (i >= 0) selectedPoiIds.value.splice(i, 1)
  else selectedPoiIds.value.push(id)
}

function onMapTogglePoi(id) {
  const city = poiCityMap.value[id]
  if (city) activeSpotCity.value = city
  togglePoi(id)
}

const poiCountsByCity = computed(() => {
  const counts = {}
  for (const id of selectedPoiIds.value) {
    const city = poiCityMap.value[id]
    if (city) counts[city] = (counts[city] || 0) + 1
  }
  return counts
})

// 预览用：跟后端 `_split_city_days` 同一套最大余数法，只是让 custom 住法的
// 按天芯片能按城市分块显示；真正拍板仍由后端在 Generate 时算一遍。
const cityDaySplit = computed(() => splitCityDays(selectedCities.value, days.value, poiCountsByCity.value))

const softLimitExceeded = computed(() => selectedPoiIds.value.length > days.value * 5)

const disabledReason = computed(() => {
  if (!selectedCities.value.length) return 'Select at least one city.'
  if (selectedCities.value.length > MAX_CITIES) return 'at most 4 cities'
  if (days.value < selectedCities.value.length) return 'days must be >= number of cities'
  const emptyCity = selectedCities.value.find(
    (c) => !selectedPoiIds.value.some((id) => poiCityMap.value[id] === c)
  )
  if (emptyCity) return 'each city must have at least one selected poi'
  if (!arrivalHubOptions.value.some((h) => h.id === arrivalHubId.value)) return 'choose an arrival airport/station'
  if (!departureHubOptions.value.some((h) => h.id === departureHubId.value)) return 'choose a departure airport/station'
  if (hotelMode.value === 'custom') {
    for (let d = 1; d <= days.value; d++) {
      if (!customLodgingByDay[d]) return 'custom_stays must cover every day'
    }
  }
  return ''
})
const canGenerate = computed(() => !disabledReason.value && !generating.value)

function startProgress() {
  let i = 0
  progressText.value = PROGRESS_MESSAGES[0]
  progressTimer = setInterval(() => {
    i = (i + 1) % PROGRESS_MESSAGES.length
    progressText.value = PROGRESS_MESSAGES[i]
  }, 2200)
}
function stopProgress() {
  if (progressTimer) clearInterval(progressTimer)
  progressTimer = null
  progressText.value = ''
}

function buildCustomStays() {
  const byLodging = {}
  for (let d = 1; d <= days.value; d++) {
    const lid = customLodgingByDay[d]
    if (!lid) continue
    if (!byLodging[lid]) byLodging[lid] = []
    byLodging[lid].push(d)
  }
  return Object.entries(byLodging).map(([lodging_id, dayList]) => ({ lodging_id, days: dayList }))
}

async function onGenerate() {
  if (!canGenerate.value) return
  generating.value = true
  errorMsg.value = ''
  startProgress()
  try {
    const result = await optimizeRoute({
      cities: selectedCities.value,
      days: days.value,
      poiIds: selectedPoiIds.value,
      hotelMode: hotelMode.value,
      customStays: hotelMode.value === 'custom' ? buildCustomStays() : [],
      arrivalHubId: arrivalHubId.value,
      departureHubId: departureHubId.value,
      firstDayDensity: firstDayDensity.value,
      lastDayDensity: lastDayDensity.value,
    })
    sessionStorage.setItem('alontrip.result', JSON.stringify(result))
    sessionStorage.setItem(
      'alontrip.planner',
      JSON.stringify({
        selectedCities: selectedCities.value,
        days: days.value,
        selectedPoiIds: selectedPoiIds.value,
        hotelMode: hotelMode.value,
        customLodgingByDay: { ...customLodgingByDay },
        arrivalHubId: arrivalHubId.value,
        departureHubId: departureHubId.value,
        firstDayDensity: firstDayDensity.value,
        lastDayDensity: lastDayDensity.value,
      })
    )
    router.push('/result')
  } catch (e) {
    errorMsg.value = `Generate failed: ${e.message}`
  } finally {
    generating.value = false
    stopProgress()
  }
}

async function loadSample() {
  errorMsg.value = ''
  selectedCities.value = ['tokyo', 'kyoto']
  days.value = 5
  hotelMode.value = 'system_one'
  firstDayDensity.value = 'few'
  lastDayDensity.value = 'none'
  activeSpotCity.value = 'tokyo'
  await Promise.all([ensureCityPois('tokyo'), ensureCityPois('kyoto'), ensureCityHubs('tokyo'), ensureCityHubs('kyoto')])
  selectedPoiIds.value = ['senso-ji', 'shibuya-crossing', 'ginza', 'fushimi-inari', 'kinkaku-ji']
  arrivalHubId.value = 'haneda-airport'
  departureHubId.value = 'kyoto-station'
}

function restoreFromSession() {
  const raw = sessionStorage.getItem('alontrip.planner')
  if (!raw) return false
  try {
    const saved = JSON.parse(raw)
    selectedCities.value = saved.selectedCities || ['tokyo']
    days.value = saved.days || 5
    selectedPoiIds.value = saved.selectedPoiIds || []
    hotelMode.value = saved.hotelMode || 'system_one'
    firstDayDensity.value = saved.firstDayDensity || (saved.edgeDensity === 'first_none_last_few' ? 'none' : 'few')
    lastDayDensity.value = saved.lastDayDensity || (saved.edgeDensity === 'first_few_last_few' ? 'few' : 'none')
    activeSpotCity.value = (saved.selectedCities || ['tokyo'])[0] || 'tokyo'
    Object.assign(customLodgingByDay, saved.customLodgingByDay || {})
    for (const c of selectedCities.value) {
      ensureCityPois(c)
      if (hotelMode.value === 'custom') ensureCityLodgings(c)
    }
    Promise.all([ensureCityHubs(selectedCities.value[0]), ensureCityHubs(selectedCities.value[selectedCities.value.length - 1])]).then(() => {
      arrivalHubId.value = saved.arrivalHubId || ''
      departureHubId.value = saved.departureHubId || ''
    })
    return true
  } catch {
    return false
  }
}

watch(hotelMode, (mode) => {
  if (mode === 'custom') {
    for (const c of selectedCities.value) ensureCityLodgings(c)
  }
})

watch(selectedCities, (cities) => {
  if (!cities.includes(activeSpotCity.value)) {
    activeSpotCity.value = cities[0] || ''
  }
})

onMounted(() => {
  loadCities()
  loadHealth()
  if (!restoreFromSession()) {
    ensureCityPois('tokyo')
  }
})

onBeforeUnmount(() => {
  stopProgress()
})
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="brand-block">
        <BrandHeader />
      </div>

      <div v-if="errorMsg" class="error-banner">
        <span>{{ errorMsg }}</span>
        <button class="close" aria-label="Dismiss" @click="errorMsg = ''">×</button>
      </div>

      <h2 class="plan-heading">Build itinerary</h2>

      <section class="plan-section">
        <div class="plan-section-head">
          <span class="plan-step">1</span>
          <div>
            <div class="field-label">Cities &amp; stations</div>
            <p class="section-hint">Pick up to {{ MAX_CITIES }}. Order is the travel order.</p>
          </div>
        </div>
        <div v-for="group in groupedCities" :key="group.id" class="country-group">
          <div class="country-label">{{ group.name }}</div>
          <div class="city-grid">
            <button
              v-for="c in group.cities"
              :key="c.id"
              class="city-chip"
              :class="{ active: selectedCities.includes(c.id) }"
              :disabled="!selectedCities.includes(c.id) && selectedCities.length >= MAX_CITIES"
              @click="toggleCity(c.id)"
            >
              {{ c.name_en }}
            </button>
          </div>
        </div>
        <ol v-if="selectedCities.length" class="city-order">
          <li v-for="(c, i) in selectedCities" :key="c">
            <span class="city-order-num">{{ i + 1 }}</span>
            <span class="city-order-name">{{ cityName(c) }}</span>
            <span class="city-order-controls">
              <button :disabled="i === 0" @click="moveCity(c, -1)">↑</button>
              <button :disabled="i === selectedCities.length - 1" @click="moveCity(c, 1)">↓</button>
            </span>
          </li>
        </ol>
        <div v-if="selectedCities.length" class="hub-picker-group">
          <div class="hub-picker">
            <div class="field-label">Arrive at ({{ cityName(firstCity) }})</div>
            <select class="select" v-model="arrivalHubId">
              <option value="" disabled>
                {{ loadingHubsFor[firstCity] ? 'Loading…' : 'Choose an airport/station' }}
              </option>
              <option v-for="h in arrivalHubOptions" :key="h.id" :value="h.id">
                {{ h.name_en }} ({{ h.kind }})
              </option>
            </select>
          </div>
          <div class="hub-picker">
            <div class="field-label">Leave from ({{ cityName(lastCity) }})</div>
            <select class="select" v-model="departureHubId">
              <option value="" disabled>
                {{ loadingHubsFor[lastCity] ? 'Loading…' : 'Choose an airport/station' }}
              </option>
              <option v-for="h in departureHubOptions" :key="h.id" :value="h.id">
                {{ h.name_en }} ({{ h.kind }})
              </option>
            </select>
          </div>
        </div>
      </section>

      <section class="plan-section">
        <div class="plan-section-head">
          <span class="plan-step">2</span>
          <div>
            <div class="field-label">Days</div>
            <p class="section-hint">At least one day per city.</p>
          </div>
        </div>
        <div class="day-group">
          <button
            v-for="d in DAY_OPTIONS"
            :key="d"
            class="day-btn"
            :class="{ active: days === d }"
            @click="days = d"
          >
            {{ d }}
          </button>
        </div>
      </section>

      <section class="plan-section">
        <div class="plan-section-head">
          <span class="plan-step">3</span>
          <div>
            <div class="field-label">Spots</div>
            <p class="section-hint">Check a name below, or click a dot on the map.</p>
          </div>
        </div>
        <div v-if="selectedCities.length" class="spot-tabs">
          <button
            v-for="city in selectedCities"
            :key="city"
            class="spot-tab"
            :class="{ active: activeSpotCity === city }"
            @click="activeSpotCity = city"
          >
            {{ cityName(city) }}
            <span class="muted">{{ poiCountsByCity[city] || 0 }}</span>
          </button>
        </div>
        <template v-if="activeSpotCity">
          <input
            class="search"
            type="text"
            placeholder="Filter spots…"
            v-model="poiSearch[activeSpotCity]"
          />
          <ul class="poi-list">
            <li v-if="loadingPoisFor[activeSpotCity]" class="poi-item">
              <label class="muted">Loading…</label>
            </li>
            <li v-else-if="!filteredPois(activeSpotCity).length" class="poi-item">
              <label class="muted">No spots found.</label>
            </li>
            <li v-for="p in filteredPois(activeSpotCity)" :key="p.id" class="poi-item">
              <label>
                <input
                  type="checkbox"
                  :checked="selectedPoiIds.includes(p.id)"
                  @change="togglePoi(p.id)"
                />
                <span class="poi-name">{{ p.name_en }}</span>
                <span class="poi-rating">★ {{ p.rating.toFixed(1) }}</span>
                <span class="poi-duration">{{ p.suggested_duration_min }}m</span>
              </label>
            </li>
          </ul>
        </template>
        <div v-if="softLimitExceeded" class="warning-banner">
          {{ selectedPoiIds.length }} spots for {{ days }} days may feel crowded (backpackers usually
          do 3–5/day) — you can still generate.
        </div>
        <div class="field-label">First / Last day</div>
        <p class="section-hint">Arrival / departure day stays light.<br>none = 0 spots, few = as few as reasonable.</p>
        <div class="density-row">
          <span class="density-row-name">First day</span>
          <div class="density-choice">
            <label
              v-for="opt in DENSITY_CHOICES"
              :key="'first-' + opt.id"
              class="hotel-mode-option"
              :class="{ active: firstDayDensity === opt.id }"
            >
              <input type="radio" :value="opt.id" v-model="firstDayDensity" />
              <span>{{ opt.label }}</span>
            </label>
          </div>
        </div>
        <div class="density-row">
          <span class="density-row-name">Last day</span>
          <div class="density-choice">
            <label
              v-for="opt in DENSITY_CHOICES"
              :key="'last-' + opt.id"
              class="hotel-mode-option"
              :class="{ active: lastDayDensity === opt.id }"
            >
              <input type="radio" :value="opt.id" v-model="lastDayDensity" />
              <span>{{ opt.label }}</span>
            </label>
          </div>
        </div>
      </section>

      <section class="plan-section">
        <div class="plan-section-head">
          <span class="plan-step">4</span>
          <div>
            <div class="field-label">Hotel mode</div>
            <p class="section-hint">One hotel per city is the default.</p>
          </div>
        </div>
        <div class="hotel-mode-group">
          <label class="hotel-mode-option" :class="{ active: hotelMode === 'system_one' }">
            <input type="radio" value="system_one" v-model="hotelMode" />
            <span>System picks (one hotel per city)</span>
          </label>
          <label class="hotel-mode-option" :class="{ active: hotelMode === 'system_multi' }">
            <input type="radio" value="system_multi" v-model="hotelMode" />
            <span>System picks (may switch hotels)</span>
          </label>
          <label class="hotel-mode-option" :class="{ active: hotelMode === 'custom' }">
            <input type="radio" value="custom" v-model="hotelMode" />
            <span>Choose my own</span>
          </label>
        </div>
        <template v-if="hotelMode === 'custom'">
          <div v-for="(dayList, city) in cityDaySplit" :key="city" class="custom-city-block">
            <div class="field-label">{{ cityName(city) }} — day {{ dayList[0] }}–{{ dayList[dayList.length - 1] }}</div>
            <input
              class="search"
              type="text"
              placeholder="Search lodgings…"
              :value="lodgingSearchQuery[city]"
              @input="onLodgingSearchInput(city, $event.target.value)"
            />
            <div class="day-chips">
              <div v-for="d in dayList" :key="d" class="day-chip">
                <span class="day-chip-label">Day {{ d }}</span>
                <select
                  class="select"
                  :value="customLodgingByDay[d] || ''"
                  @change="customLodgingByDay[d] = $event.target.value"
                >
                  <option value="" disabled>
                    {{ loadingLodgingsFor[city] ? 'Loading…' : 'Choose a hotel' }}
                  </option>
                  <option v-for="l in lodgingsByCity[city] || []" :key="l.id" :value="l.id">
                    {{ l.name }}
                  </option>
                </select>
              </div>
            </div>
          </div>
        </template>
      </section>

      <div class="sidebar-footer">
        <button class="optimize" :disabled="!canGenerate" @click="onGenerate">
          {{ generating ? progressText || 'Generating…' : 'Generate route' }}
        </button>
        <div v-if="!canGenerate && !generating" class="disabled-reason">{{ disabledReason }}</div>
        <button class="sample-btn" :disabled="generating" @click="loadSample">
          Load Tokyo + Kyoto sample
        </button>
        <div class="health-footer">
          grouper: {{ health.grouper || '…' }} · poi_provider: {{ health.poi_provider || '…' }}
        </div>
      </div>
    </aside>
    <MapView :pois="allPoisFlat" :selected-ids="selectedPoiIds" @toggle-poi="onMapTogglePoi" />
  </div>
</template>
