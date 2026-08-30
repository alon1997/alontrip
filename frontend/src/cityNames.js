// City id → display name. The itinerary API returns day.city / cities as ids
// ("tokyo"); showing the raw id in sidebar Day titles and export cards would
// read "Day 1 — tokyo". The city list is frozen for the hackathon, so this is
// a static map (kept in sync with the backend CITIES name_en).
const CITY_NAMES = {
  tokyo: 'Tokyo',
  kyoto: 'Kyoto',
  osaka: 'Osaka',
  beijing: 'Beijing',
  shanghai: 'Shanghai',
  hongkong: 'Hong Kong',
  seoul: 'Seoul',
  busan: 'Busan',
  jeju: 'Jeju',
}

export const cityDisplayName = (id) => CITY_NAMES[id] || id
