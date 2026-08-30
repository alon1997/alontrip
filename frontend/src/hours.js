// Mirror backend/app/services/hours.py so Update-transit clocks match optimize-route.

function parseClock(token) {
  const text = String(token || '').replace(/\u202f|\u00a0/g, ' ').trim()
  const match = text.match(/(\d{1,2})(?::(\d{2}))?\s*([AP]M)/i)
  if (!match) return null
  let hour = Number(match[1])
  const minute = Number(match[2] || 0)
  const ampm = match[3].toUpperCase()
  if (hour === 12) hour = ampm === 'AM' ? 0 : 12
  else if (ampm === 'PM') hour += 12
  return hour * 60 + minute
}

export function parseHours(text) {
  if (!text) return { opens: null, closes: null }
  const raw = String(text).replace(/\u202f|\u00a0/g, ' ')
  if (/24 hour/i.test(raw)) return { opens: 0, closes: 24 * 60 }
  const openMatch = raw.match(/Opens\s+(\d{1,2}(?::\d{2})?\s*[AP]M)/i)
  const closeMatch = raw.match(/Closes\s+(\d{1,2}(?::\d{2})?\s*[AP]M)/i)
  let opens = openMatch ? parseClock(openMatch[1]) : null
  let closes = closeMatch ? parseClock(closeMatch[1]) : null
  if (closes === 0) closes = 24 * 60
  return { opens, closes }
}

export function clipVisit(clock, remain, hoursText) {
  const { opens, closes } = parseHours(hoursText)
  if (opens != null && clock < opens) clock = opens
  if (closes != null && clock >= closes) return { clock, remain: 0 }
  if (closes != null) remain = Math.min(remain, closes - clock)
  return { clock, remain: Math.max(0, remain) }
}
