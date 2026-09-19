import { inject, onUnmounted, provide, reactive } from 'vue'
import { api } from '../api'

const KEY = Symbol('poll')
const BUSY_MS = 3000
const IDLE_MS = 15000

export function providePoll({ gpu = () => false, onFinish = () => {} } = {}) {
  let timer = null
  let inflight = null
  let again = null
  let running = null
  let alive = true

  const state = reactive({
    overview: null, worker: null, gpu: null, reachable: true, finished: 0, refresh,
  })

  const busy = () =>
    !!state.overview?.jobs.some(j => j.state === 'running') ||
    !!state.worker?.workers.some(w => !w.stale && (w.job_id || state.overview?.jobs.length))

  function schedule() {
    clearTimeout(timer)
    if (alive && document.visibilityState !== 'hidden') {
      timer = setTimeout(refresh, busy() ? BUSY_MS : IDLE_MS)
    }
  }

  async function read() {
    try {
      const [o, w] = await Promise.all([api('/api/overview'), api('/api/scan/worker')])
      state.overview = o
      state.worker = w
      state.reachable = true
      const now = new Set(o.jobs.map(j => j.id))
      if (running) {
        const gone = [...running].filter(id => !now.has(id))
        gone.forEach(onFinish)
        if (gone.length) state.finished++
      }
      running = now
    } catch { state.reachable = false }
    if (gpu()) {
      try { state.gpu = await api('/api/gpu') } catch { state.gpu = null }
    }
  }

  function refresh() {
    clearTimeout(timer)
    if (inflight) return (again ||= inflight.then(() => { again = null; return refresh() }))
    inflight = read().finally(() => { inflight = null; schedule() })
    return inflight
  }

  const onVisible = () =>
    document.visibilityState === 'hidden' ? clearTimeout(timer) : refresh()
  document.addEventListener('visibilitychange', onVisible)
  onUnmounted(() => {
    alive = false
    clearTimeout(timer)
    document.removeEventListener('visibilitychange', onVisible)
  })

  refresh()
  provide(KEY, state)
  return state
}

export function scannerState(poll) {
  if (!poll.reachable) return { tone: 'bad', text: '無法連線後端' }
  const w = poll.worker
  if (!w) return { tone: '', text: '載入中' }
  const live = w.workers.filter(x => !x.stale)
  if (live.some(x => x.job_id)) return { tone: 'go live', text: '執行中' }
  if (live.length) return { tone: 'go', text: '閒置' }
  if (w.workers.length) return { tone: 'warn', text: '無回應' }
  return { tone: '', text: '未啟動' }
}

export const usePoll = () => inject(KEY)
