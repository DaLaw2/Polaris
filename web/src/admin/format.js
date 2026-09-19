export const KIND = { scan: '掃描', check: '檢查重複', derive: '重新計算', build: '建置' }

export const ROLE = {
  general: '一般', character: '角色', copyright: '系列', artist: '作者',
  rating: '分級', embedding: '向量',
}

export const STATE = {
  queued: '排隊中', running: '執行中', done: '已完成',
  failed: '失敗', cancelled: '已取消', stopping: '停止中',
}

export const n = v => (v ?? 0).toLocaleString('en-US')

export function ago(ts) {
  if (!ts) return ''
  const s = Math.max(0, (Date.now() - new Date(ts)) / 1000)
  if (s < 90) return `${Math.round(s)} 秒前`
  if (s < 5400) return `${Math.round(s / 60)} 分鐘前`
  if (s < 129600) return `${Math.round(s / 3600)} 小時前`
  return `${Math.round(s / 86400)} 天前`
}

export const gb = mb => (mb / 1024).toFixed(1)

export const pct = (done, total) => (total ? Math.round((done / total) * 100) : 0)
