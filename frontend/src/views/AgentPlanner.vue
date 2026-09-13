<script setup>
import { ref, nextTick, onMounted, onBeforeUnmount } from 'vue'
import BrandHeader from '../components/BrandHeader.vue'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

// Agent mode (default view), terminal-styled (Claude-Code-like):
// - left: the conversation (fixed height, scrolls; never stretches the page)
// - right: the map (always visible; it does not share space with the plan)
// - the itinerary renders INSIDE the chat as a structured plan block with
//   engine-authoritative times and server-computed provenance badges.
// Tool-call spam is collapsed: consecutive identical tools collapse into one
// line with a ×N counter and a human-readable argument label.

const DAY_COLORS = ['#e3b341', '#06b6d4', '#3fb950', '#f85149', '#bc8cff', '#ff7b72']
const SPINNER = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
// session id is MUTABLE: an aborted/corrupted strands session cannot be
// reused (otel context corruption kills follow-up streams silently), so a
// retry moves to a fresh session id.
const sessionId = ref('web-' + Math.random().toString(36).slice(2, 8))

const messages = ref([]) // {role:'user'|'agent'|'tool'|'plan'|'decision'|'error'|'note', ...}
const input = ref('')
const busy = ref(false)
const thinking = ref(false)
const plan = ref(null)
const info = ref({ app: 'TripAgent', version: '…', model: '…', framework: 'strands-agents', tools: 8 })
const toolCalls = ref(0)
const elapsed = ref(0)
let timer = null
let spinTimer = null
let spinFrame = 0
const spinner = ref(SPINNER[0])

const logEl = ref(null)
const planBlockEl = ref(null)
const mapEl = ref(null)
let map = null
let markers = []
const spotIndex = {}

function scrollLog() {
  nextTick(() => { if (logEl.value) logEl.scrollTop = logEl.scrollHeight })
}
function push(node) {
  messages.value.push(node)
  scrollLog()
}

async function loadCity(city) {
  if (spotIndex.__city === city) return
  const r = await fetch(`/api/trip/agent/spots/${encodeURIComponent(city)}`)
  const spots = await r.json()
  spots.forEach(s => { spotIndex[s.id] = s })
  spotIndex.__city = city
}

function renderPlanOnMap() {
  if (!map || !plan.value) return
  markers.forEach(m => map.removeLayer(m))
  markers = []
  const pts = []
  plan.value.days.forEach((d, i) => d.spots.forEach(s => {
    const meta = spotIndex[s.id]
    if (!meta) return
    const m = L.circleMarker([meta.lat, meta.lng], {
      radius: 7, color: DAY_COLORS[i % DAY_COLORS.length], fillOpacity: 0.9, weight: 2,
    }).addTo(map).bindTooltip(s.name_en)
    markers.push(m)
    pts.push([meta.lat, meta.lng])
  }))
  if (pts.length) map.fitBounds(pts, { padding: [30, 30] })
  map.invalidateSize()
}

// tool-call line: collapse repeats, human label instead of raw JSON
let lastTool = null // {msgIndex, name, count}
function toolLine(name, preview) {
  toolCalls.value += 1
  const label = toolLabel(name, preview)
  if (lastTool && messages.value[lastTool.msgIndex]?.name === name) {
    const m = messages.value[lastTool.msgIndex]
    m.count = (m.count || 1) + 1
    m.label = label
    scrollLog()
    return
  }
  push({ role: 'tool', name, label, count: 1 })
  lastTool = { msgIndex: messages.value.length - 1, name }
}

function toolLabel(name, preview) {
  if (name === 'search_pois' || name === 'spot_detail') {
    const m = preview.match(/"city"\s*:\s*"([^"]+)"/)
    return m ? `${name}(${m[1]})` : name
  }
  if (name === 'draft_day_plan') {
    const city = preview.match(/"city"\s*:\s*"([^"]+)"/)?.[1] || ''
    const days = preview.match(/"days"\s*:\s*(\d+)/)?.[1] || ''
    return `draft_day_plan(${city}, ${days}d)`
  }
  if (name === 'replay_clock') {
    const d = preview.match(/"day"\s*:\s*(\d+)/)?.[1]
    return d ? `replay_clock(day ${d})` : 'replay_clock()'
  }
  if (name === 'transit_route') return 'transit_route()'
  if (name === 'trip_budget') return 'trip_budget()'
  if (name === 'list_cities') return 'list_cities()'
  if (name === 'ask_traveller') return 'ask_traveller()'
  return name
}

let sseBuf = ''
let streaming = null

function handleAgentEvent(ev) {
  if (ev.type === 'text') {
    thinking.value = false
    if (!streaming) { streaming = { role: 'agent', text: '' }; push(streaming) }
    streaming.text += ev.delta
    scrollLog()
  } else if (ev.type === 'tool') {
    thinking.value = false
    streaming = null
    toolLine(ev.name, ev.input_preview || '')
  } else if (ev.type === 'decision') {
    streaming = null
    push({ role: 'decision', interruptId: ev.interrupt_id, question: ev.question, options: ev.options, context: ev.context })
  } else if (ev.type === 'plan') {
    streaming = null
    thinking.value = false
    plan.value = ev.plan
    const notes = ev.engine_notes || []
    push({ role: 'plan', plan: ev.plan, provenance: ev.provenance || null, dayCosts: ev.day_costs || [], engineNotes: notes })
    loadCity(ev.plan.days[0].city).then(renderPlanOnMap)
  } else if (ev.type === 'error') {
    streaming = null
    push({ role: 'error', text: ev.message })
  } else if (ev.type === 'done' && ev.state === 'waiting_for_decision') {
    streaming = null
  }
}

async function readSSE(res) {
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  sseBuf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    sseBuf += dec.decode(value, { stream: true })
    const parts = sseBuf.split('\n\n')
    sseBuf = parts.pop()
    for (const p of parts) {
      try { handleAgentEvent(JSON.parse(p.replace(/^data:\s*/, ''))) } catch { /* skip bad frame */ }
    }
  }
}

let retryDepth = 0

async function send(text, isRetry = false) {
  if (!text.trim() || busy.value) return
  if (!isRetry) retryDepth = 0
  busy.value = true
  thinking.value = true
  elapsed.value = 0
  timer = setInterval(() => { elapsed.value += 1 }, 1000)
  spinTimer = setInterval(() => { spinFrame = (spinFrame + 1) % SPINNER.length; spinner.value = SPINNER[spinFrame] }, 90)
  push({ role: 'user', text })
  const res = await fetch('/api/trip/agent/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: text, session_id: sessionId.value }),
  })
  await readSSE(res)
  clearInterval(timer); clearInterval(spinTimer)
  // T-A5: if the stream ended without any outcome (aborted/corrupted
  // session), retry ONCE on a fresh session instead of going silent.
  const sawOutcome = messages.value.some(m => ['plan', 'decision', 'error'].includes(m.role))
  if (!sawOutcome && retryDepth < 1) {
    retryDepth += 1
    push({ role: 'note', text: 'the last stream ended early — retrying on a fresh session…' })
    sessionId.value = sessionId.value + '-r'
    busy.value = false
    return send(text, true)
  }
  busy.value = false
  thinking.value = false
}

async function resume(interruptId, answer) {
  busy.value = true
  push({ role: 'note', text: '↳ ' + answer })
  elapsed.value = 0
  timer = setInterval(() => { elapsed.value += 1 }, 1000)
  spinTimer = setInterval(() => { spinFrame = (spinFrame + 1) % SPINNER.length; spinner.value = SPINNER[spinFrame] }, 90)
  const res = await fetch('/api/trip/agent/resume', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId.value, interrupt_id: interruptId, response: answer }),
  })
  await readSSE(res)
  clearInterval(timer); clearInterval(spinTimer)
  busy.value = false
}

function onSubmit() {
  const t = input.value.trim()
  if (!t) return
  input.value = ''
  autogrow()
  send(t)
}
const inputEl = ref(null)
function autogrow() {
  // terminal-style input that grows with the sentence instead of scrolling sideways
  nextTick(() => {
    const el = inputEl.value
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 140) + 'px'
  })
}
function pickOption(d, o) {
  d.picked = true
  resume(d.interruptId, o)
}
function fmtElapsed(s) {
  return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0')
}
function onResize() {
  if (map) map.invalidateSize()
}

onMounted(async () => {
  map = L.map(mapEl.value).setView([35.0, 135.76], 12)
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    className: 'dark-tiles',
    attribution: '&copy; OpenStreetMap contributors | spots: AlonTrip catalog',
  }).addTo(map)
  window.addEventListener('resize', onResize)
  fetch('/api/trip/agent/info').then(r => r.json()).then(j => { info.value = j }).catch(() => {})
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  clearInterval(timer); clearInterval(spinTimer)
})
</script>

<template>
  <div class="agent-page">
    <BrandHeader tagline="agent mode · talk to plan · it watches the trip afterwards" />

    <div class="agent-grid">
      <!-- ============ left: terminal conversation ============ -->
      <section class="term">
        <div class="term-head">
          <span class="logo-sym">◆</span>
          <span class="app">TRIPAGENT</span>
          <span class="chip ver">v{{ info.version }}</span>
          <span class="chip">✻ {{ info.model }}</span>
          <span class="chip">⚒ {{ info.tools }} tools</span>
          <span class="chip dim">⌘ {{ sessionId }}</span>
        </div>

        <div ref="logEl" class="log">
          <div class="line note">
            ── TripAgent · East Asia transit concierge · the agent works, you
            keep the pen ──
          </div>
          <div class="line note try">
            ❯ try: “4 days 3 nights in Tokyo, Tokyo Disneyland is a must.”
          </div>

          <template v-for="(m, i) in messages" :key="i">
            <div v-if="m.role === 'user'" class="line user"><span class="ps">❯</span>{{ m.text }}</div>

            <div v-else-if="m.role === 'agent'" class="line agent">{{ m.text }}</div>

            <div v-else-if="m.role === 'tool'" class="line tool">
              <span class="bullet">⏺</span> <span class="tname">{{ m.name }}</span>
              <span v-if="(m.count || 1) > 1" class="count">×{{ m.count }}</span>
              <span class="prev"> ⎿ {{ m.label }}</span>
            </div>

            <div v-else-if="m.role === 'note'" class="line note">{{ m.text }}</div>

            <div v-else-if="m.role === 'error'" class="line err">✗ {{ m.text }}</div>

            <div v-else-if="m.role === 'decision'" class="decision">
              <div class="d-head">⏸ DECISION REQUIRED</div>
              <div class="d-q">{{ m.question }}</div>
              <div v-if="m.context" class="d-ctx">{{ m.context }}</div>
              <button
                v-for="o in m.options" :key="o" :disabled="m.picked || busy"
                @click="pickOption(m, o)"
              ><span class="opt-n">›</span> {{ o }}</button>
            </div>

            <div v-else-if="m.role === 'plan'" ref="planBlockEl" class="plan-block">
              <div class="p-head">┌─ ITINERARY ────────────────────────</div>
              <div class="prov" :class="{ bad: m.provenance?.spots_invented || m.provenance?.duplicate_spot_ids?.length }">
                <span class="ok">✓</span> {{ m.provenance?.spots_catalog_verified }}/{{ m.provenance?.spots_total }} spots catalog-verified
                <template v-for="(v, k) in m.provenance?.transit_legs || {}" :key="k">
                  <span v-if="v"> · {{ v }} {{ k }}</span>
                </template>
                · {{ m.provenance?.live_api_calls ?? 0 }} live API calls
              </div>
              <div v-for="(d, i) in m.plan.days" :key="d.day" class="p-day">
                <div class="d-line" :style="{ color: DAY_COLORS[i % DAY_COLORS.length] }">
                  ├─ Day {{ d.day }} · {{ d.city }}<span v-if="d.hotel"> · {{ d.hotel }}</span>
                  <span v-if="m.dayCosts.find(c => c.day === d.day)"> · ≈ ${{ m.dayCosts.find(c => c.day === d.day).total_usd }}</span>
                </div>
                <div v-if="!d.spots.length" class="d-spot dim">(transit / free day)</div>
                <div v-for="s in d.spots" :key="s.id" class="d-spot">
                  <span class="t">{{ s.start }}–{{ s.end }}</span> {{ s.name_en }}
                  <span v-if="s.note" class="s-note">· {{ s.note }}</span>
                </div>
                <div
                  v-for="c in [m.dayCosts.find(c => c.day === d.day)]"
                  v-show="c && (c.transit_usd || c.tickets_usd)"
                  class="d-spot cost"
                >│ ${{ c?.transit_usd }} transit + ${{ c?.tickets_usd }} tickets</div>
              </div>
              <div v-for="w in m.plan.warnings || []" :key="w" class="warn">⚠ {{ w }}</div>
              <div v-for="n in m.engineNotes || []" :key="n" class="warn">ℹ {{ n }}</div>
              <div v-if="m.plan.total_usd" class="warn total">└─ Trip total ≈ ${{ m.plan.total_usd }}</div>
            </div>
          </template>

          <div v-if="thinking" class="line think">
            {{ spinner }} planning<span class="dots"><i>.</i><i>.</i><i>.</i></span>
            <span class="dim">⏱ {{ fmtElapsed(elapsed) }}</span>
          </div>
        </div>

        <div class="statusline">
          <span class="st">✻ {{ info.model }}</span>
          <span class="st">⚒ {{ info.framework }}</span>
          <span class="st" :class="{ ok: toolCalls }">⚒ {{ toolCalls }} calls</span>
          <span class="st" :class="{ ok: busy }">⏱ {{ fmtElapsed(elapsed) }}</span>
          <span class="st dim2">⛨ {{ sessionId }}</span>
        </div>

        <form class="composer" @submit.prevent="onSubmit">
          <span class="ps">❯</span>
          <textarea
            ref="inputEl"
            v-model="input"
            :disabled="busy"
            rows="1"
            autocomplete="off"
            placeholder="Describe your trip…  (Enter to send, Shift+Enter for a new line)"
            @keydown.enter.exact.prevent="onSubmit"
            @input="autogrow"
          />
          <button class="send" :disabled="busy">Send</button>
        </form>
      </section>

      <!-- ============ right: map only ============ -->
      <section class="right">
        <div ref="mapEl" class="map" />
      </section>
    </div>
  </div>
</template>

<style scoped>
.agent-page {
  height: 100vh;
  display: flex;
  flex-direction: column;
  padding: 12px 16px 12px;
  gap: 10px;
  overflow: hidden;
}
.agent-grid {
  flex: 1;
  display: grid;
  grid-template-columns: minmax(430px, 520px) 1fr;
  gap: 14px;
  min-height: 0; /* CRITICAL: lets children scroll instead of stretching the page */
}

/* ---------- terminal panel: fixed height, internal scroll ---------- */
.term {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #0a0d12;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}
.term-head {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 8px 12px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  font-size: 12px;
}
.logo-sym { color: var(--cyan); font-weight: 700; }
.app { color: var(--text); font-weight: 700; letter-spacing: 1px; }
.chip {
  color: var(--cyan);
  background: rgba(6, 182, 212, 0.08);
  border: 1px solid rgba(6, 182, 212, 0.25);
  border-radius: 4px;
  padding: 1px 7px;
  font-size: 11px;
}
.chip.ver { color: var(--gold); border-color: rgba(227, 179, 65, 0.35); background: rgba(227, 179, 65, 0.07); }
.chip.dim { color: var(--muted); border-color: var(--border); background: transparent; }

.log {
  flex: 1;
  min-height: 0; /* the whole scroll contract lives on this line */
  overflow-y: auto;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 9px;
  font-size: 13.5px;
  line-height: 1.55;
}
.line { white-space: pre-wrap; word-break: break-word; }
.line.user { color: var(--gold); }
.line.user .ps { color: var(--gold); margin-right: 8px; font-weight: 700; }
.line.agent { color: var(--text); }
.line.note { color: var(--muted); font-size: 12px; }
.line.try { color: var(--muted); }
.line.tool { font-size: 12.5px; }
.bullet { color: var(--cyan); }
.tname { color: var(--cyan); font-weight: 600; }
.count { color: var(--muted); font-size: 11px; }
.prev { color: var(--muted); opacity: 0.7; }
.line.err { color: var(--red, #f85149); }
.line.think { color: var(--muted); }
.dim, .dim2 { color: var(--muted); opacity: 0.7; }

.decision {
  border: 1px solid var(--gold);
  border-radius: 6px;
  padding: 10px 12px;
  background: rgba(227, 179, 65, 0.04);
}
.d-head { color: var(--gold); font-size: 11px; letter-spacing: 1.5px; margin-bottom: 6px; }
.d-q { color: var(--text); margin-bottom: 4px; }
.d-ctx { color: var(--muted); font-size: 12px; margin-bottom: 6px; }
.decision button {
  display: block; width: 100%; text-align: left; margin: 4px 0;
  background: transparent; color: var(--cyan); border: 1px solid var(--border);
  border-radius: 4px; padding: 6px 10px; cursor: pointer; font: inherit; font-size: 13px;
}
.decision button:hover { border-color: var(--cyan); background: rgba(6, 182, 212, 0.06); }
.decision button:disabled { opacity: 0.4; cursor: default; }
.opt-n { color: var(--muted); margin-right: 6px; }

.dots i { animation: blink 1.4s infinite; font-style: normal; }
.dots i:nth-child(2) { animation-delay: 0.2s; }
.dots i:nth-child(3) { animation-delay: 0.4s; }
@keyframes blink { 0%, 60%, 100% { opacity: 0.2; } 30% { opacity: 1; } }

/* statusline */
.statusline {
  display: flex; gap: 14px; flex-wrap: wrap;
  padding: 5px 12px;
  border-top: 1px solid var(--border);
  background: var(--panel);
  font-size: 11.5px;
  color: var(--muted);
}
.st.ok { color: var(--green, #3fb950); }

.composer {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px;
  border-top: 1px solid var(--border);
  background: var(--panel);
}
.composer .ps { color: var(--gold); font-weight: 700; }
.composer textarea {
  flex: 1; background: transparent; color: var(--text);
  border: 0; outline: none; font: inherit; font-size: 13.5px;
  resize: none; overflow-y: auto; min-height: 20px; max-height: 140px;
  line-height: 1.5;
}
.send {
  background: transparent; border: 1px solid var(--border); color: var(--cyan);
  border-radius: 4px; padding: 5px 12px; cursor: pointer; font: inherit; font-size: 12px;
}
.send:hover:not(:disabled) { border-color: var(--cyan); }
.send:disabled { opacity: 0.4; cursor: default; }

/* ---------- right: map fills the column ---------- */
.right { min-height: 0; }
.map {
  height: 100%;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}

/* ---------- plan block (lives in the chat flow) ---------- */
.plan-block {
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
  padding: 10px 12px;
  font-size: 13px;
  white-space: normal;
}
.p-head { color: var(--muted); font-size: 11.5px; letter-spacing: 1px; margin-bottom: 8px; }
.prov {
  color: var(--green, #3fb950);
  border: 1px solid rgba(63, 185, 80, 0.3);
  background: rgba(63, 185, 80, 0.05);
  border-radius: 4px; padding: 4px 9px; margin-bottom: 10px; font-size: 12px;
}
.prov.bad { color: var(--red, #f85149); border-color: rgba(248, 81, 73, 0.4); background: rgba(248, 81, 73, 0.05); }
.prov .ok { color: var(--green, #3fb950); }
.p-day { margin-bottom: 10px; }
.d-line { font-weight: 600; margin-bottom: 3px; }
.d-spot { padding-left: 16px; padding: 1px 0 1px 16px; }
.d-spot .t { color: var(--muted); margin-right: 8px; }
.d-spot.cost { color: var(--muted); font-size: 12px; }
.s-note { color: var(--muted); font-size: 12px; }
.warn { color: var(--gold); font-size: 12px; padding-left: 12px; }
.total { padding-left: 0; font-size: 13px; }

@media (max-width: 1150px) {
  .agent-grid { grid-template-columns: 1fr; }
  .term { height: 60vh; }
  .map { height: 50vh; }
}
</style>
