export const STEP = { download: '下載', validate: '檢查', convert: '轉換', verify: '驗證' }

export const PRECISION = { fp32: 'FP32', fp16: 'FP16' }

export function stateOf(v, job) {
  if (v.state === 'ready') return { tone: 'green', text: '就緒' }
  if (v.state === 'failed') return { tone: 'red', text: '失敗' }
  if (!job) return { tone: 'orange', text: '已停止' }
  return { tone: 'blue', text: job.state === 'queued' ? '排隊中' : '建置中' }
}

export function summary(d) {
  const has = s => d.versions.some(v => v.state === s)
  if (has('building')) return { tone: 'blue', text: '建置中' }
  if (has('ready')) return { tone: 'green', text: '就緒' }
  if (has('failed')) return { tone: 'red', text: '失敗' }
  return { tone: '', text: '未建置' }
}
