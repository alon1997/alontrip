// Rich export (T-043): 1 full-trip route image + N day cards (itinerary left /
// day map right). Everything renders inside an offscreen stage with one-shot
// throwaway Leaflet instances; html-to-image grabs the PNG, then we
// immediately map.remove() + stage.remove() — the live visible map plays no
// part, and export success or failure never affects the page. Downloads run
// one by one (decided against adding jszip); a single failure doesn't abort
// the rest.
import L from 'leaflet'
import { toPng } from 'html-to-image'
import { addDarkBasemap } from './basemap'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const CARD_W = 1600
const CARD_H = 1000

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

function shorten(name, max = 18) {
  const text = String(name || '')
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

function slug(text) {
  return String(text).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'trip'
}

function el(tag, className, html) {
  const node = document.createElement(tag)
  if (className) node.className = className
  if (html != null) node.innerHTML = html
  return node
}

// Leaflet needs real layout, so display:none is out — hide the stage offscreen with fixed positioning.
function buildStage() {
  const stage = el('div', 'export-stage')
  stage.style.width = `${CARD_W}px`
  stage.style.height = `${CARD_H}px`
  document.body.appendChild(stage)
  return stage
}

function makeStaticMap(host, fit, maxZoom) {
  const map = L.map(host, {
    preferCanvas: true,
    zoomControl: false,
    dragging: false,
    scrollWheelZoom: false,
    doubleClickZoom: false,
    boxZoom: false,
    keyboard: false,
    touchZoom: false,
    zoomAnimation: false,
    inertia: false,
    zoomSnap: 0.25,
  })
  addDarkBasemap(map)
  if (fit.length) map.fitBounds(fit, { padding: [40, 40], maxZoom })
  else map.setView([35.68, 139.69], 12)
  return map
}

// Static version of renderDay: numbered pins (matching the itinerary row
// numbers on the left) + the same polyline styles. No arrows — the live
// version's projectedMidAndAngle closes over the live map's getZoom(), and
// the numbered pins already convey direction. labels=false is for the
// full-trip image (many days, names collide); day cards label POIs only —
// hotels/hubs go unlabeled (on city-change days the bounds span cities and
// hotel names get clipped by the card edge; icon + watermark legend are
// enough). POI name collisions are resolved by resolveExportLabelCollisions.
function drawStaticLayers(map, spec) {
  const group = L.layerGroup()
  for (const leg of spec.legs) {
    L.polyline([[leg.from.lat, leg.from.lng], [leg.to.lat, leg.to.lng]], {
      color: leg.color,
      weight: leg.pending ? 2.5 : 3.5,
      opacity: leg.pending ? 0.35 : 0.85,
      dashArray: leg.intercity ? '2 10' : leg.pending ? '5 7' : null,
      interactive: false,
    }).addTo(group)
  }
  for (const pin of spec.pins) {
    let marker
    if (pin.type === 'poi') {
      marker = L.marker([pin.lat, pin.lng], {
        icon: L.divIcon({
          className: 'export-pin-icon',
          html: `<div class="export-pin" style="background:${pin.color}">${pin.n}</div>`,
          iconSize: [22, 22],
          iconAnchor: [11, 11],
        }),
        interactive: false,
      })
      marker.options._rank = pin.n ?? 900
      if (spec.labels && pin.name) {
        marker.bindTooltip(escapeHtml(shorten(pin.name)), {
          permanent: true, direction: 'top', offset: [0, -10], className: 'map-label map-label-poi',
        })
      }
    } else if (pin.type === 'hub') {
      marker = L.marker([pin.lat, pin.lng], {
        icon: L.divIcon({
          className: 'hub-pin-icon',
          html: `<div class="hub-pin">${pin.symbol || '✈'}</div>`,
          iconSize: [26, 26],
          iconAnchor: [13, 22],
        }),
        interactive: false,
      })
    } else {
      marker = L.marker([pin.lat, pin.lng], {
        icon: L.divIcon({
          className: 'hotel-pin-icon',
          // Same little house as ResultMap's hotelIcon() (the style classes are global, so they apply to offscreen nodes too)
          html: '<div class="hotel-pin"><svg class="hotel-pin-glyph" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8 2.2 2.4 7.1v6.5h3.5V9.2h4.2v4.4h3.5V7.1L8 2.2z"/></svg></div>',
          iconSize: [26, 26],
          iconAnchor: [13, 22],
        }),
        interactive: false,
      })
    }
    marker.addTo(group)
  }
  group.addTo(map)
}

// Label collision avoidance for exported images: static images have no
// hover, so losing labels are simply hidden. Priority = number (1 before 2);
// when a city-change day's bounds squeeze same-group labels together, the
// lower number wins.
function resolveExportLabelCollisions(map) {
  const entries = []
  map.eachLayer((layer) => {
    const el = layer.getTooltip ? layer.getTooltip()?.getElement() : null
    if (!el) return
    entries.push({ el, rank: layer.options?._rank ?? 0 })
  })
  entries.sort((a, b) => a.rank - b.rank)
  const PAD = 4
  const kept = []
  for (const { el } of entries) {
    const r = el.getBoundingClientRect()
    const box = { l: r.left - PAD, t: r.top - PAD, r: r.right + PAD, b: r.bottom + PAD }
    if (kept.some((k) => box.l < k.r && box.r > k.l && box.t < k.b && box.b > k.t)) {
      el.classList.add('map-label--hidden')
    } else {
      kept.push(box)
    }
  }
}

// Simple polling of the in-flight tile count: event order between
// moveend/fitBounds and tileloadstart differs across zoom paths, so polling
// sidesteps ordering pitfalls; the 6s cap keeps one slow tile from hanging
// the wait forever. No layer.off() cleanup — that would strip Leaflet's own
// listeners too; map.remove() is enough.
async function waitForTiles(map, timeout = 6000) {
  let inflight = 0
  map.eachLayer((layer) => {
    if (!(layer instanceof L.TileLayer)) return
    layer.on('tileloadstart', () => { inflight += 1 })
    layer.on('tileload', () => { inflight -= 1 })
    layer.on('tileerror', () => { inflight -= 1 })
  })
  const start = Date.now()
  return new Promise((resolve) => {
    const poll = () => {
      if (inflight <= 0 || Date.now() - start > timeout) resolve()
      else setTimeout(poll, 50)
    }
    setTimeout(poll, 150) // let tileloadstart register first, so we don't bail before anything loads
  })
}

async function capture(node, mapHost, spec, maxZoom) {
  let map = null
  try {
    if (mapHost && spec) {
      map = makeStaticMap(mapHost, spec.fit || [], maxZoom)
      drawStaticLayers(map, spec)
      await waitForTiles(map)
      await sleep(700) // let tile decode/paint settle
      resolveExportLabelCollisions(map)
    }
    // Capture the inner static node (.export-card), not the stage itself: the
    // stage's position:fixed is inherited by the clone, and Chrome renders
    // nothing for fixed elements inside a foreignObject (the whole image
    // comes out as plain background). Same pattern as the old #map capture.
    return await toPng(node, { pixelRatio: 2, backgroundColor: '#0d1117' })
  } finally {
    if (map) map.remove()
  }
}

function download(dataUrl, filename) {
  const link = document.createElement('a')
  link.download = filename
  link.href = dataUrl
  document.body.appendChild(link)
  link.click()
  link.remove()
}

function buildCardHead(subtitle) {
  return el('div', 'export-card-head', `
    <span class="export-brand">AlonTrip</span>
    <span class="export-route">${escapeHtml(subtitle)}</span>
    <span class="export-url">alonuniverse.com/trip</span>`)
}

function buildWatermark() {
  return el('div', 'export-watermark', `
    <span>numbered pins match the itinerary &nbsp;·&nbsp; ⌂ hotel &nbsp;·&nbsp; ✈ arrival / departure</span>
    <span>alonuniverse.com/trip</span>`)
}

function buildDayCard(model, ds) {
  const card = el('div', 'export-card')
  card.appendChild(buildCardHead(ds.title))
  const body = el('div', 'export-card-body')
  const itin = el('div', 'export-itin')
  itin.appendChild(el('div', 'export-day-title',
    `<span class="export-day-dot" style="background:${ds.color}"></span>Day ${ds.day} — ${escapeHtml(ds.city)}`))
  const ol = el('ol', 'export-schedule')
  for (const ev of ds.schedule) {
    const num = ev.num != null
      ? `<span class="export-num" style="background:${ds.color}">${ev.num}</span>`
      : '<span class="export-num export-num--blank"></span>'
    ol.appendChild(el('li', `export-row export-row--${ev.kind}`, `
      <span class="export-row-time">${escapeHtml(ev.start)}</span>${num}
      <span class="export-row-body"><b>${escapeHtml(ev.title)}</b><i>${escapeHtml(ev.meta)}</i></span>`))
  }
  itin.appendChild(ol)
  itin.appendChild(el('div', 'export-day-stats', escapeHtml(ds.statsText)))
  body.appendChild(itin)
  const mapWrap = el('div', 'export-map')
  const mapHost = el('div', 'export-map-inner')
  mapWrap.appendChild(mapHost)
  body.appendChild(mapWrap)
  card.appendChild(body)
  card.appendChild(buildWatermark())
  return { card, mapHost }
}

function buildFullCard(model) {
  const card = el('div', 'export-card')
  card.appendChild(buildCardHead(model.tripTitle))
  const mapWrap = el('div', 'export-map export-map--full')
  const mapHost = el('div', 'export-map-inner')
  mapWrap.appendChild(mapHost)
  const legend = el('div', 'export-legend',
    model.days.map((ds) => `
      <div class="export-legend-row">
        <span class="export-day-dot" style="background:${ds.color}"></span>Day ${ds.day} — ${escapeHtml(ds.city)}
      </div>`).join(''))
  mapWrap.appendChild(legend)
  card.appendChild(mapWrap)
  card.appendChild(buildWatermark())
  return { card, mapHost }
}

// Pin set for the full-trip image: dedupe across days (hotels may be reused on consecutive nights, hubs exist only once).
function dedupePins(pins) {
  const seen = new Set()
  const out = []
  for (const pin of pins) {
    const key = `${pin.type}:${pin.lat.toFixed(5)},${pin.lng.toFixed(5)}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push(pin)
  }
  return out
}

export async function exportTripImages(model, onProgress) {
  const jobs = [
    { kind: 'full', filename: 'alontrip-route-all.png' },
    ...model.days.map((ds) => ({ kind: 'day', ds, filename: `alontrip-day-${ds.day}-${slug(ds.city)}.png` })),
  ]
  let ok = 0
  const failed = []
  for (let i = 0; i < jobs.length; i++) {
    const job = jobs[i]
    onProgress?.(i + 1, jobs.length)
    const stage = buildStage()
    try {
      let card = null
      let mapHost = null
      let spec = null
      let maxZoom = 15
      if (job.kind === 'day') {
        const built = buildDayCard(model, job.ds)
        stage.appendChild(built.card)
        card = built.card
        mapHost = built.mapHost
        spec = { ...job.ds, labels: true }
        maxZoom = 16
      } else {
        const built = buildFullCard(model)
        stage.appendChild(built.card)
        card = built.card
        mapHost = built.mapHost
        spec = {
          legs: model.days.flatMap((ds) => ds.legs),
          pins: dedupePins(model.days.flatMap((ds) => ds.pins)),
          fit: model.fit,
          labels: false, // names collide across many days; color + legend + numbers are enough
        }
      }
      const dataUrl = await capture(card, mapHost, spec, maxZoom)
      download(dataUrl, job.filename)
      ok += 1
    } catch (error) {
      failed.push({ label: job.filename, error })
    } finally {
      stage.remove()
    }
    if (i < jobs.length - 1) await sleep(400) // breather for the browser; steadier on rapid consecutive downloads
  }
  return { ok, failed }
}
