// Short labels for TransitRoute.data_source (must match backend transit.py).

const LABELS = {
  serpapi_live: 'SerpApi · live',
  serpapi_cache: 'SerpApi · cached',
  walk: 'walk',
  taxi: 'taxi',
  amap: 'Amap',
  estimate: 'estimated',
}

export function sourceLabel(source) {
  if (!source) return ''
  return LABELS[source] || ''
}
