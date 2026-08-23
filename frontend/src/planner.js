// Frontend-only preview of the backend's city→day allocation (方案 6.1b A /
// backend/app/services/planner.py `_split_city_days`). Used purely so the
// custom-hotel day chips can be grouped under the right city *before*
// Generate is clicked — the backend is still the source of truth and
// re-derives this itself when the request lands.
export function splitCityDays(cities, days, poiCounts) {
  const n = cities.length
  if (n === 0) return {}
  const extra = days - n
  const weights = cities.map((c) => Math.max(poiCounts[c] || 0, 1))
  const total = weights.reduce((a, b) => a + b, 0)
  let allocations = new Array(n).fill(1)

  if (extra > 0) {
    const rawShares = weights.map((w) => (extra * w) / total)
    const floorShares = rawShares.map((s) => Math.floor(s))
    const remainder = extra - floorShares.reduce((a, b) => a + b, 0)
    const order = rawShares
      .map((s, i) => ({ i, frac: s - floorShares[i] }))
      .sort((a, b) => b.frac - a.frac)
      .map((x) => x.i)
    for (let k = 0; k < remainder; k++) {
      floorShares[order[k]] += 1
    }
    allocations = floorShares.map((s) => 1 + s)
  }

  const result = {}
  let cursor = 1
  cities.forEach((city, idx) => {
    const count = allocations[idx]
    result[city] = Array.from({ length: count }, (_, k) => cursor + k)
    cursor += count
  })
  return result
}
