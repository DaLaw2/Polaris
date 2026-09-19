import { reactive } from 'vue'
import { explain } from './errors'

const toasts = reactive([])
let seq = 0

function dismiss(id) {
  const i = toasts.findIndex(t => t.id === id)
  if (i >= 0) toasts.splice(i, 1)
}

function show(text, { tone = 'info', action = null, timeout } = {}) {
  const t = { id: ++seq, text, tone, action }
  toasts.push(t)
  while (toasts.length > 4) toasts.shift()
  const ms = timeout ?? (tone === 'bad' ? 0 : action ? 6000 : 3000)
  if (ms) setTimeout(() => dismiss(t.id), ms)
  return t.id
}

async function fire(t) {
  dismiss(t.id)
  try { await t.action.run() } catch (e) { show(explain(e), { tone: 'bad' }) }
}

export function useToast() {
  return {
    toasts,
    show,
    dismiss,
    fire,
    ok: (text, opts) => show(text, { ...opts, tone: 'ok' }),
    bad: (e, opts) => show(typeof e === 'string' ? e : explain(e), { ...opts, tone: 'bad' }),
    undo: (text, run) => show(text, { tone: 'ok', action: { label: '復原', run } }),
  }
}
