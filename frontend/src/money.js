/** Display currency is always USD (D-019). Backend already converts; this
 *  also converts leftover catalog JPY/CNY/KRW/HKD on the client. */
const TO_USD = {
  USD: 1,
  JPY: 1 / 150,
  CNY: 1 / 7.2,
  KRW: 1 / 1350,
  HKD: 1 / 7.8,
}

export function toUsd(amount, currency) {
  if (amount == null) return null
  const n = Number(amount)
  if (!Number.isFinite(n)) return null
  const rate = TO_USD[(currency || 'USD').toUpperCase()]
  if (rate == null) return Math.round(n * 100) / 100
  return Math.round(n * rate * 100) / 100
}

export function money(amount, currency = 'USD') {
  const n = toUsd(amount, currency)
  if (n == null) return 'price unknown'
  if (n === 0) return 'free'
  return n < 10 ? `$${n.toFixed(2)}` : `$${Math.round(n)}`
}
