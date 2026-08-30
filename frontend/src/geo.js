// Small geometry helpers shared by the result-page map editor (T-015).
// Mirrors backend/app/services/transit.py's haversine so "nearest edge"
// insertion and distance-based decisions use the same notion of distance
// the server does — just enough precision for UI heuristics, not billing.

const EARTH_RADIUS_KM = 6371

export function haversineKm(a, b) {
  const toRad = (d) => (d * Math.PI) / 180
  const dLat = toRad(b.lat - a.lat)
  const dLng = toRad(b.lng - a.lng)
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLng / 2) ** 2
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(h))
}

// Initial compass bearing from a -> b, in degrees clockwise from north —
// used to rotate the little self-drawn arrowheads along each route segment
// (plan 6.1c: "Leaflet polyline decorator vs. self-drawn arrows" — we picked
// the latter to avoid pulling in a whole decorator plugin for one arrow shape).
export function bearingDeg(a, b) {
  const toRad = (d) => (d * Math.PI) / 180
  const toDeg = (r) => (r * 180) / Math.PI
  const lat1 = toRad(a.lat)
  const lat2 = toRad(b.lat)
  const dLng = toRad(b.lng - a.lng)
  const y = Math.sin(dLng) * Math.cos(lat2)
  const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLng)
  return (toDeg(Math.atan2(y, x)) + 360) % 360
}

export function midpoint(a, b) {
  return { lat: (a.lat + b.lat) / 2, lng: (a.lng + b.lng) / 2 }
}
