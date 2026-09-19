import { api } from '../api'

const cache = new Map()

export function concepts(kind) {
  if (!cache.has(kind)) {
    const p = api(`/api/concepts?kind=${kind}`)
    p.catch(() => cache.delete(kind))
    cache.set(kind, p)
  }
  return cache.get(kind)
}

export const forget = kind => (kind ? cache.delete(kind) : cache.clear())

export const FIELD = {
  artist: '作者', character: '角色', series: '系列', tag: '標籤',
  rating: '分級', color_mode: '色彩', work_type: '類型', language: '語言',
}

export const ENUM = new Set(['rating', 'color_mode', 'work_type'])

export const label = v => v?.display_zh || v?.slug || ''
