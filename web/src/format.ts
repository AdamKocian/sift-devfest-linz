// Number and time formatting. Mirrors server/server/fmt.py so UI and agent text agree.
// Units are joined with a non-breaking space so "4.1 M" never wraps between number and unit.
const NB = '\u00a0'

export function gib(value: number): string {
  if (value >= 100) return `${Math.round(value).toLocaleString('en-US')}${NB}GiB`
  if (value >= 10) return `${value.toFixed(1)}${NB}GiB`
  return `${value.toFixed(2)}${NB}GiB`
}

export function eur(value: number): string {
  return `€${Math.round(value).toLocaleString('en-US')}`
}

export function count(value: number): string {
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)}${NB}B`
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}${NB}M`
  if (value >= 1e4) return `${Math.round(value / 1e3)}${NB}k`
  return Math.round(value).toLocaleString('en-US')
}

export function pct(ratio: number, digits = 1): string {
  const v = ratio * 100
  if (v >= 99.95) return `100${NB}%`
  return `${v.toFixed(digits)}${NB}%`
}

export function signedPct(ratio: number): string {
  const v = Math.round(ratio * 100)
  const sign = v > 0 ? '+' : v < 0 ? '−' : '±'
  return `${sign}${Math.abs(v)}${NB}%`
}

export function tokens(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`
  return String(n)
}

export function ago(iso: string | null, now: number = Date.now()): string {
  if (!iso) return 'never'
  const seconds = Math.max(0, Math.round((now - Date.parse(iso)) / 1000))
  if (seconds < 60) return 'just now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} h ago`
  const days = Math.floor(hours / 24)
  return days === 1 ? '1 day ago' : `${days} days ago`
}

export function clock(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
}

export function dateTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  const time = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
  if (sameDay) return `Today ${time}`
  return `${d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })} ${time}`
}

export function longDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-GB', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  })
}

export function shortDay(isoDate: string): string {
  const d = new Date(`${isoDate}T00:00:00`)
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
}

export function duration(ms: number | null): string {
  if (ms == null) return ''
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(1)} s`
}

export function elapsed(fromIso: string, now: number = Date.now()): string {
  const s = Math.max(0, Math.floor((now - Date.parse(fromIso)) / 1000))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`
}

export const KIND_TAG: Record<string, string> = {
  log_source: 'Log',
  log_pattern: 'Pattern',
  span: 'Span',
  span_attribute: 'Attribute',
  metric: 'Metric',
  metric_dimension: 'Dimension',
}

export const KIND_LABEL: Record<string, string> = {
  log_source: 'Log source',
  log_pattern: 'Log pattern',
  span: 'Span',
  span_attribute: 'Span attribute',
  metric: 'Metric',
  metric_dimension: 'Metric dimension',
}

export const ACTION_LABEL: Record<string, string> = {
  drop: 'Drop',
  sample: 'Sample',
  drop_field: 'Remove field',
  logs_to_metric: 'Logs to metric',
  lower_level: 'Lower level',
  redact: 'Redact',
}

export const REF_LABEL: Record<string, [string, string]> = {
  query: ['query', 'queries'],
  dashboard: ['dashboard', 'dashboards'],
  notebook: ['notebook', 'notebooks'],
  workflow: ['workflow', 'workflows'],
  alert: ['alert', 'alerts'],
  slo: ['SLO', 'SLOs'],
}
