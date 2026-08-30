// Backend API base: the env var wins (overridable at build time); local dev defaults to localhost:5003
// Empty string means same origin (production). Only fall back when the
// variable is unset (local `npm run dev` without a .env).
const rawBase = import.meta.env.VITE_API_BASE_URL
const API_BASE = (rawBase === undefined ? 'http://localhost:5003' : String(rawBase)).replace(/\/$/, '')

async function request(path, options) {
  const res = await fetch(`${API_BASE}${path}`, options)
  if (!res.ok) {
    let detail = ''
    try {
      detail = (await res.json()).detail || ''
    } catch {
      // non-JSON error body, ignore
    }
    // T-013's 10s generation cooldown (stops rapid clicks from burning
    // SerpApi quota) used to surface a raw "HTTP 429: slow down"; shown in
    // plain English instead. The 400 freeze detail text stays as-is.
    if (res.status === 429) {
      throw new Error('Cooldown — the last generation was just requested. Wait about 10 seconds and try again.')
    }
    // T-046: Nginx cuts off slow generations at 60s. The backend actually
    // keeps running and writes its cache, so a retry almost always hits it —
    // tell the user this directly.
    if (res.status === 504) {
      throw new Error('The generation ran long and the gateway cut it off. Click Generate again — the transit lookups are already cached, so the retry finishes fast.')
    }
    throw new Error(`HTTP ${res.status}${detail ? `: ${detail}` : ''}`)
  }
  return res.json()
}

export function fetchHealth() {
  return request('/api/trip/health')
}

export function fetchCities() {
  return request('/api/trip/cities')
}

export function fetchPois(city) {
  return request(`/api/trip/pois?city=${encodeURIComponent(city)}`)
}

export function searchPois(city, q) {
  return request(`/api/trip/pois/search?city=${encodeURIComponent(city)}&q=${encodeURIComponent(q)}`)
}

export function fetchLodgings(city) {
  return request(`/api/trip/lodgings?city=${encodeURIComponent(city)}`)
}

export function searchLodgings(city, q) {
  return request(`/api/trip/lodgings/search?city=${encodeURIComponent(city)}&q=${encodeURIComponent(q)}`)
}

// T-016: arrival/departure airport & station catalog (queried per city)
export function fetchTransportHubs(city) {
  return request(`/api/trip/transport-hubs?city=${encodeURIComponent(city)}`)
}

// 2.6: new optimize-route contract (multi-city + days + hotel mode + custom hotels)
// T-016 addition: arrival/departure anchors + first/last day spot density
// T-034 addition: landing/takeoff times (HH:MM, submitted together with the matching hub)
export function optimizeRoute({
  cities,
  days,
  poiIds,
  hotelMode,
  customStays,
  arrivalHubId,
  departureHubId,
  arrivalTime,
  departureTime,
  firstDayDensity,
  lastDayDensity,
}) {
  return request('/api/trip/optimize-route', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      cities,
      days,
      poi_ids: poiIds,
      hotel_mode: hotelMode,
      custom_stays: customStays || [],
      arrival_hub_id: arrivalHubId || null,
      departure_hub_id: departureHubId || null,
      arrival_time: arrivalTime || null,
      departure_time: departureTime || null,
      first_day_density: firstDayDensity || 'few',
      last_day_density: lastDayDensity || 'none',
    }),
  })
}

// 2.7: result-page geometry tweaks re-query only changed legs, no DeepSeek call (T-015)
export function transitLegs(pairs) {
  return request('/api/trip/transit-legs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pairs }),
  })
}
