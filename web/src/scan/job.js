import { n } from '../admin/format'

export const TONE = { running: 'blue', done: 'green', failed: 'red', cancelled: 'orange' }

export const live = j => j.state === 'running' || j.state === 'queued'

export const stateOf = j => (live(j) && j.cancel_requested ? 'stopping' : j.state)

export const short = p => (p ? String(p).split(/[\\/]/).filter(Boolean).slice(-2).join('\\') : '')

export function where(j, profiles) {
  if (j.kind === 'derive') return profiles.find(p => p.id === j.model_profile_id)?.name || '—'
  if (j.paths) return j.paths.length === 1 ? short(j.paths[0]) : `${n(j.paths.length)} 項`
  return j.collection || short(j.path) || '全部收藏庫'
}

const seen = {}

export function note(jobs) {
  for (const j of jobs) {
    if (j.state === 'running' && !seen[j.id]) seen[j.id] = { t: Date.now(), done: j.done }
  }
}

export function rate(j) {
  const s = seen[j.id]
  if (!s) return null
  const min = (Date.now() - s.t) / 60000
  return min > 0.1 && j.done > s.done ? (j.done - s.done) / min : null
}

export function left(j) {
  const r = rate(j)
  if (!r || !j.total) return ''
  const m = (j.total - j.done) / r
  return m < 90 ? `${Math.max(1, Math.round(m))} 分鐘` : `${(m / 60).toFixed(1)} 小時`
}
