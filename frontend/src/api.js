// 后端接口基址：环境变量优先（构建时可覆盖），开发默认指向本地 5003
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
      // 非 JSON 错误体，忽略
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

// T-016：到达/离开的机场/车站目录（按城市查）
export function fetchTransportHubs(city) {
  return request(`/api/trip/transport-hubs?city=${encodeURIComponent(city)}`)
}

// 2.6：新 optimize-route 契约（多城 + 天数 + 住法 + 自选酒店）
// T-016 扩展：到达/离开锚点 + 首末日景点密度
export function optimizeRoute({
  cities,
  days,
  poiIds,
  hotelMode,
  customStays,
  arrivalHubId,
  departureHubId,
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
      first_day_density: firstDayDensity || 'few',
      last_day_density: lastDayDensity || 'none',
    }),
  })
}

// 2.7：结果页几何微调只重查变化段，不跑 DeepSeek（T-015）
export function transitLegs(pairs) {
  return request('/api/trip/transit-legs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pairs }),
  })
}
