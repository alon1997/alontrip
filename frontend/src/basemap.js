// Dark basemap: CARTO dark_all now enforces an API key (verified 2026-08-28:
// keyless requests return a 1970B "API KEY REQUIRED" placeholder tile, even
// with an alonuniverse.com Referer), turning every map into a wall of
// watermarks. Switched to Esri World Dark Gray Canvas (free, keyless, dark,
// stylistically continuous): Base supplies the terrain, Reference supplies
// place-name labels (transparent PNG overlay).
// Note Esri uses {z}/{y}/{x} order, maxZoom 16.
import L from 'leaflet'

const ESRI_BASE_URL =
  'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}'
const ESRI_REFERENCE_URL =
  'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}'

const LAYER_OPTS = {
  maxZoom: 16,
  crossOrigin: true, // required for html-to-image to draw tiles into a canvas
}

export function addDarkBasemap(map) {
  L.tileLayer(ESRI_BASE_URL, {
    ...LAYER_OPTS,
    attribution: 'Tiles &copy; Esri &mdash; Source: Esri, USGS, NOAA',
  }).addTo(map)
  L.tileLayer(ESRI_REFERENCE_URL, { ...LAYER_OPTS, opacity: 0.85 }).addTo(map)
}
