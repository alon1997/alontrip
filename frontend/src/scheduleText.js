// Shared text formatters for the day schedule — one source for the live
// sidebar (Result.vue), the map component (ResultMap.vue) and the export
// cards (exportCards.js), so all three render identical meta lines.
import { money } from './money'
import { sourceLabel } from './transitSource'

export const fmt = (min) => (min >= 60 ? `${Math.floor(min / 60)}h ${min % 60}m` : `${min} min`)

export function ticketLine(ev) {
  if (ev.cost == null) return 'ticket unknown'
  if (ev.cost === 0) return 'free'
  return money(ev.cost, ev.currency)
}

export function scheduleMeta(ev) {
  if (ev.kind === 'transit') {
    const bits = [ev.line_summary]
    if (ev.cost != null && ev.cost > 0) bits.push(money(ev.cost, ev.currency))
    bits.push(fmt(ev.duration_min))
    const src = sourceLabel(ev.data_source)
    if (src) bits.push(src)
    return bits.join(' · ')
  }
  if (ev.kind === 'visit') return `${ticketLine(ev)} · ${fmt(ev.duration_min)}`
  return fmt(ev.duration_min)
}
