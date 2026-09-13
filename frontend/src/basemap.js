// Dark basemap history (both keyless):
// - 2026-08-28: CARTO dark_all keyless → "API KEY REQUIRED" watermark tiles
//   → switched to Esri World Dark Gray Canvas.
// - 2026-09-12: Esri started failing outright (0 tiles load from CN browsers;
//   service behind API-key gating now), AND a re-probe showed CARTO keyless
//   still watermarks every tile. Only OSM raster loads clean (keyless).
// Current approach: OSM raster + a CSS invert/hue filter on the tile pane
// (`.dark-tiles` in style.css) renders it as a dark map. Works everywhere,
// no key, no watermarks.
import L from 'leaflet'

const OSM_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'

const LAYER_OPTS = {
  maxZoom: 19,
  crossOrigin: true, // required for html-to-image to draw tiles into a canvas
  className: 'dark-tiles',
}

export function addDarkBasemap(map) {
  L.tileLayer(OSM_URL, {
    ...LAYER_OPTS,
    attribution: '&copy; OpenStreetMap contributors',
  }).addTo(map)
}
