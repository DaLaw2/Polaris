function reason(detail) {
  if (Array.isArray(detail)) return detail.map(d => d?.msg ?? String(d)).join('; ')
  if (detail && typeof detail === 'object') return detail.message ?? detail.code
  return detail
}

export async function api(path, { method = 'GET', body } = {}) {
  const r = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await r.json().catch(() => null)
  if (!r.ok) {
    const e = new Error(reason(data?.detail) || `${r.status} ${r.statusText}`)
    e.code = data?.detail?.code
    e.detail = data?.detail
    throw e
  }
  return data
}
