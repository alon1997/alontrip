// Basemap history: CARTO keyless watermarked (08-28) -> Esri Canvas (dead
// since 09-12, API-key gated) -> AutoNavi/Gaode road tiles (fast from CN and
// abroad, crisp, no key). Labels on the tiles are in local language - that is
// map data, not page UI.
import L from 'leaflet'

const GAODE_URL =
  'https://tile.openstreetmap.org/{z}/{x}/{y}.png'

const LAYER_OPTS = {
  maxZoom: 19,
}

export function addDarkBasemap(map) {
  L.tileLayer(GAODE_URL, {
    ...LAYER_OPTS,
    attribution: '&copy; OpenStreetMap contributors',
  }).addTo(map)
}
