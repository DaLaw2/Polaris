<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Reader from '../Reader.vue'
import { api } from '../api'

const route = useRoute()
const router = useRouter()

const f = reactive({
  tags: [],
  notTags: [],
  artist: '',
  series: '',
  title: '',
  character: [],
  copyright: [],
  language: [],
  color_mode: [],
  rating: [],
  work_type: [],
  mode: 'AND',
  sort: 'newest',
  collection: [],
})

const PAGE_SIZES = [50, 100, 150, 200]
const SIZE_KEY = 'polaris:pagesize'
const stored = Number(localStorage.getItem(SIZE_KEY))
const pageSize = ref(PAGE_SIZES.includes(stored) ? stored : 50)
watch(pageSize, v => {
  try { localStorage.setItem(SIZE_KEY, String(v)) } catch {}
  search(true)
})
const page = ref(0)
const results = ref([])
const total = ref(0)
const facets = ref({})
const loading = ref(false)
const error = ref('')
const stats = ref(null)
const tagTranslations = ref({})
const vocabulary = ref([])

const similarRef = ref('')
const similarMode = ref('similar')

const view = ref('works')
const copiesFlag = () => view.value === 'copies' ? '&copies=true' : ''

const checkJob = ref(null)
const checkJobs = ref([])
const checkRun = ref(null)
const checkCounts = ref({})
const checkFilter = ref('')
const VERDICT_LABEL = { same: '相同', near: '相似', new: '新的', error: '出錯' }
const discarding = ref('')
const batch = ref(false)
const picked = reactive(new Set())

const selectedWork = ref(null)
const selectedOverrides = ref([])
const overrideError = ref('')
const addingTag = ref(false)
const newTagInput = ref('')
const editingField = ref(null)
const editFieldValue = ref('')

const reading = ref(null)
const openError = ref('')

function read(work) {
  reading.value = { path: work.folder_path, title: work.title || work.folder_name }
  syncUrl()
}
function closeReader() {
  reading.value = null
  syncUrl()
}
async function openOnDesktop(work) {
  openError.value = ''
  try { await api('/api/open', { method: 'POST', body: { path: work.folder_path } }) }
  catch (e) { openError.value = e.message }
}

const term = ref('')
const showAC = ref(false)
const acResults = ref([])
const acIndex = ref(-1)
const searchInputRef = ref(null)

const SECTIONS = [
  { key: 'collection', label: '收藏庫', kind: 'multi' },
  { key: 'artist', label: '作者', kind: 'text', placeholder: '作者名（可部分）' },
  { key: 'series', label: '系列', kind: 'text', placeholder: '系列名（可部分）' },
  { key: 'copyright', label: '作品', kind: 'multi', tagLike: true },
  { key: 'character', label: '角色', kind: 'multi', tagLike: true },
  { key: 'language', label: '語言', kind: 'multi' },
  { key: 'color_mode', label: '色彩', kind: 'multi' },
  { key: 'rating', label: '評級', kind: 'multi' },
  { key: 'work_type', label: '類型', kind: 'multi' },
  { key: 'general', label: '常見標籤', kind: 'tag', tagLike: true },
]

const FALLBACK_ENUMS = {
  rating: [
    { slug: 'general' }, { slug: 'sensitive' },
    { slug: 'questionable' }, { slug: 'explicit' },
  ],
  color_mode: [
    { slug: 'full_color', display_zh: '全彩' },
    { slug: 'grayscale', display_zh: '黑白' },
    { slug: 'partial_color', display_zh: '部分彩色' },
  ],
  work_type: [
    { slug: 'comic', display_zh: '漫畫' },
    { slug: 'illustration', display_zh: '插圖' },
    { slug: 'cg_set', display_zh: 'CG 集' },
  ],
}

const enums = reactive({ rating: [], color_mode: [], work_type: [] })

const VALUE_LABELS = reactive({})

function enumLabel(c) {
  return c.display_zh || c.slug
}

function setEnum(kind, rows) {
  enums[kind] = rows
  for (const c of rows) if (c.display_zh) VALUE_LABELS[c.slug] = c.display_zh
}

for (const [kind, rows] of Object.entries(FALLBACK_ENUMS)) setEnum(kind, rows)

async function loadEnums() {
  await Promise.all(Object.keys(FALLBACK_ENUMS).map(async kind => {
    try {
      const res = await fetch(`/api/concepts?kind=${kind}`)
      const rows = await res.json()
      if (Array.isArray(rows) && rows.length) setEnum(kind, rows)
    } catch {}
  }))
}

const collapsed = reactive({
  artist: true, series: true,
  copyright: true, character: true, general: true,
})

const QUICK_ENUMS = computed(() =>
  enums.color_mode.map(c => ({ label: enumLabel(c), dim: 'color_mode', value: c.slug })))

const QUICK = computed(() => [
  ...QUICK_ENUMS.value,
  ...Object.keys(stats.value?.top_tags || {})
    .slice(0, 8)
    .map(t => ({ label: tr(t), dim: 'tag', value: t })),
])

function formatDuration(seconds) {
  const total = Math.round(seconds)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const sec = total % 60
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
    : `${m}:${String(sec).padStart(2, '0')}`
}

function quickActive(q) {
  return q.dim === 'tag' ? f.tags.includes(q.value) : f[q.dim].includes(q.value)
}
function toggleQuick(q) {
  if (q.dim === 'tag') toggleIn(f.tags, q.value)
  else toggleIn(f[q.dim], q.value)
  search(true)
}

function toggleIn(arr, value) {
  const i = arr.indexOf(value)
  if (i >= 0) arr.splice(i, 1)
  else arr.push(value)
}

const hasTagTerms = computed(() =>
  f.tags.length > 0 || f.character.length > 0 || f.copyright.length > 0)

function syncSort() {
  if (hasTagTerms.value && f.sort === 'newest') f.sort = 'relevance'
  else if (!hasTagTerms.value && f.sort === 'relevance') f.sort = 'newest'
}

function toggleFacet(key, value) {
  if (key === 'general') toggleIn(f.tags, value)
  else if (SECTIONS.find(s => s.key === key)?.kind === 'text') {
    f[key] = f[key] === value ? '' : value
  } else toggleIn(f[key], value)
  search(true)
}

function excludeFacet(key, value) {
  const src = key === 'general' ? f.tags : f[key]
  const i = src.indexOf(value)
  if (i >= 0) src.splice(i, 1)
  if (!f.notTags.includes(value)) f.notTags.push(value)
  search(true)
}

function isFacetActive(key, value) {
  if (key === 'general') return f.tags.includes(value)
  const v = f[key]
  return Array.isArray(v) ? v.includes(value) : v === value
}

const activeChips = computed(() => {
  const out = []
  for (const t of f.tags) out.push({ label: tr(t), raw: t, dim: 'tags', kind: 'tag' })
  for (const t of f.notTags) out.push({ label: '− ' + tr(t), raw: t, dim: 'notTags', kind: 'not' })
  for (const s of SECTIONS) {
    if (s.kind === 'text') {
      if (f[s.key]) out.push({ label: `${s.label}: ${f[s.key]}`, raw: f[s.key], dim: s.key, kind: 'text' })
    } else if (s.key !== 'general') {
      for (const v of f[s.key]) {
        out.push({ label: `${s.label}: ${VALUE_LABELS[v] || tr(v)}`, raw: v, dim: s.key, kind: 'multi' })
      }
    }
  }
  return out
})

function removeChip(chip) {
  if (chip.kind === 'text') f[chip.dim] = ''
  else toggleIn(f[chip.dim], chip.raw)
  search(true)
}

function clearAll() {
  f.tags = []; f.notTags = []
  f.artist = ''; f.series = ''; f.title = ''
  f.character = []; f.copyright = []
  f.language = []; f.color_mode = []; f.rating = []; f.work_type = []
  f.collection = []
  similarRef.value = ''
  search(true)
}

const hasFilters = computed(() => activeChips.value.length > 0)

const LIST_KEYS = ['collection', 'character', 'copyright', 'language',
                   'color_mode', 'rating', 'work_type']

function toQuery() {
  const p = buildParams()
  p.delete('limit')
  p.delete('offset')
  if (page.value) p.set('page', String(page.value + 1))
  if (similarRef.value) {
    p.set('similar', similarRef.value)
    p.set('simMode', similarMode.value)
  }
  if (view.value !== 'works') p.set('view', view.value)
  if (view.value === 'check' && checkJob.value) p.set('job', String(checkJob.value))
  if (selectedWork.value) p.set('work', selectedWork.value.folder_path)
  if (reading.value) p.set('read', reading.value.path)
  return Object.fromEntries(p)
}

function fromQuery(q) {
  f.tags = q.q ? q.q.split(',') : []
  f.notTags = q.exclude ? q.exclude.split(',') : []
  for (const k of ['artist', 'series', 'title']) f[k] = q[k] || ''
  for (const k of LIST_KEYS) f[k] = q[k] ? q[k].split(',') : []
  f.mode = q.mode || 'AND'
  f.sort = q.sort || 'newest'
  page.value = Math.max(0, (Number(q.page) || 1) - 1)
  similarRef.value = q.similar || ''
  similarMode.value = q.simMode || 'similar'
  view.value = ['copies', 'check'].includes(q.view) ? q.view : 'works'
  checkJob.value = q.job ? Number(q.job) : checkJob.value
}

const same = (a, b) => JSON.stringify(Object.entries(a).sort())
                    === JSON.stringify(Object.entries(b).sort())

let applying = false
let firstSearch = true

function syncUrl() {
  if (applying) return
  const q = toQuery()
  if (same(q, route.query)) return
  router[firstSearch ? 'replace' : 'push']({ query: q })
}

watch(() => route.query, async q => {
  if (same(q, toQuery())) return
  applying = true
  fromQuery(q)
  applying = false
  await search()
})

watch(selectedWork, () => syncUrl())

function buildParams() {
  const p = new URLSearchParams()
  if (f.tags.length) p.set('q', f.tags.join(','))
  if (f.notTags.length) p.set('exclude', f.notTags.join(','))
  for (const key of ['artist', 'series', 'title']) {
    if (f[key]) p.set(key, f[key])
  }
  for (const key of ['character', 'copyright', 'language', 'color_mode', 'rating', 'work_type']) {
    if (f[key].length) p.set(key, f[key].join(','))
  }
  p.set('mode', f.mode)
  p.set('sort', f.sort)
  if (f.collection.length) p.set('collection', f.collection.join(','))
  p.set('limit', String(pageSize.value))
  p.set('offset', String(page.value * pageSize.value))
  return p
}

let seq = 0
let inflight = null
function freshSignal() {
  inflight?.abort()
  inflight = new AbortController()
  return inflight.signal
}
async function search(resetPage = false, withFacets = true) {
  if (resetPage) page.value = 0
  picked.clear()
  syncSort()
  syncUrl()
  firstSearch = false
  if (view.value === 'check') return searchCheck()
  if (similarRef.value) return searchSimilar()

  const mine = ++seq
  loading.value = true
  error.value = ''
  try {
    const res = await fetch(
      `/api/search/faceted?${buildParams()}${copiesFlag()}${withFacets ? '' : '&facets=false'}`,
      { signal: freshSignal() })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    if (mine !== seq) return
    results.value = data.results
    total.value = data.total
    if (withFacets) facets.value = data.facets || {}
  } catch (e) {
    if (mine === seq && e.name !== 'AbortError') error.value = `查詢失敗: ${e.message}`
  } finally {
    if (mine === seq) loading.value = false
  }
}

async function searchSimilar() {
  const mine = ++seq
  loading.value = true
  error.value = ''
  try {
    const c = f.collection.length
      ? `&collection=${encodeURIComponent(f.collection.join(','))}` : ''
    const url = similarMode.value === 'hybrid' && f.tags.length
      ? `/api/hybrid?q=${encodeURIComponent(f.tags.join(' '))}&similar_to=${encodeURIComponent(similarRef.value)}&limit=60${c}${copiesFlag()}`
      : `/api/similar?path=${encodeURIComponent(similarRef.value)}&limit=60${c}${copiesFlag()}`
    const res = await fetch(url, { signal: freshSignal() })
    const data = await res.json()
    if (mine !== seq) return
    results.value = data
    total.value = 0
    facets.value = {}
  } catch (e) {
    if (mine === seq && e.name !== 'AbortError') error.value = `查詢失敗: ${e.message}`
  } finally {
    if (mine === seq) loading.value = false
  }
}

async function loadCheckJobs() {
  const d = await api('/api/scan/jobs?kind=check&limit=20')
  checkJobs.value = d.jobs
  if (!checkJobs.value.some(j => j.id === checkJob.value)) {
    checkJob.value = checkJobs.value.length ? checkJobs.value[0].id : null
  }
}

let checkTimer = null
function armCheckPoll() {
  clearInterval(checkTimer)
  checkTimer = null
  const state = checkRun.value && checkRun.value.state
  if (view.value === 'check' && (state === 'queued' || state === 'running')) {
    checkTimer = setInterval(() => searchCheck(), 2000)
  }
}
onUnmounted(() => clearInterval(checkTimer))

async function searchCheck() {
  const mine = ++seq
  loading.value = true
  error.value = ''
  try {
    await loadCheckJobs()
    if (!checkJob.value) {
      results.value = []
      total.value = 0
      checkRun.value = null
      checkCounts.value = {}
      return
    }
    const q = new URLSearchParams()
    if (checkFilter.value) q.set('verdict', checkFilter.value)
    q.set('limit', String(pageSize.value))
    q.set('offset', String(page.value * pageSize.value))
    const d = await api(`/api/check/jobs/${checkJob.value}/items?${q}`)
    if (mine !== seq) return
    checkRun.value = d.job
    checkCounts.value = d.counts
    total.value = d.total
    facets.value = {}
    results.value = d.items.map(it => ({
      verdict: it.verdict,
      score: it.matches.length ? it.matches[0].score : null,
      work: { folder_path: it.path, folder_name: it.path.split('\\').pop() },
      places: [
        {
          path: it.path, collection: '待檢查', copy: false,
          exists: !it.discarded, bytes: it.bytes, item_id: it.id,
        },
        ...it.matches.map(m => ({
          path: m.work.folder_path, collection: m.work.collection,
          copy: true, exists: true, bytes: null, locked: true,
          score: m.score,
        })),
      ],
    }))
  } catch (e) {
    if (mine === seq) error.value = `查詢失敗: ${e.message}`
  } finally {
    if (mine === seq) loading.value = false
    armCheckPoll()
  }
}

function setCheckJob(id) {
  checkJob.value = Number(id) || null
  search(true)
}

function filterVerdict(v) {
  checkFilter.value = checkFilter.value === v ? '' : v
  search(true)
}

function findSimilar(work) {
  similarRef.value = work.folder_path
  similarMode.value = 'similar'
  search(true)
}

function useAsBase(work) {
  similarRef.value = work.folder_path
  similarMode.value = 'hybrid'
  search(true)
}

function clearSimilar() {
  similarRef.value = ''
  search(true)
}

function setView(v) {
  if (view.value === v) return
  view.value = v
  endBatch()
  search(true)
}

function formatBytes(b) {
  if (b == null) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++ }
  return `${b.toFixed(i ? 1 : 0)} ${units[i]}`
}

const alive = r => (r.places || []).filter(p => p.exists).length

async function revealPlace(p) {
  try { await api('/api/open', { method: 'POST', body: { path: p.path, reveal: true } }) }
  catch (e) { error.value = e.message }
}

const discardOne = p => p.item_id
  ? api(`/api/check/items/${p.item_id}/discard`, { method: 'POST' })
  : api('/api/copies/discard', { method: 'POST', body: { path: p.path } })

async function discardPlace(p) {
  if (!confirm(`把這份丟進資源回收筒？\n\n${p.path}\n${formatBytes(p.bytes)}`)) return
  discarding.value = p.path
  error.value = ''
  try {
    await discardOne(p)
    await search(false)
  } catch (e) { error.value = e.message }
  discarding.value = ''
}

const pickedIn = r => (r.places || []).filter(p => picked.has(p.path)).length
const canPick = (r, p) => view.value === 'check'
  ? (!p.locked && p.exists)
  : picked.has(p.path) || (p.exists && alive(r) - pickedIn(r) > 1)

function togglePick(r, p) {
  if (picked.has(p.path)) picked.delete(p.path)
  else if (canPick(r, p)) picked.add(p.path)
}

function endBatch() {
  batch.value = false
  picked.clear()
}

const pickedPlaces = computed(() =>
  results.value.flatMap(r => (r.places || []).filter(p => picked.has(p.path))))
const pickedBytes = computed(() =>
  pickedPlaces.value.reduce((n, p) => n + (p.bytes || 0), 0))

async function discardPicked() {
  const todo = [...pickedPlaces.value].sort((a, b) => b.copy - a.copy)
  if (!confirm(`把 ${todo.length} 份丟進資源回收筒？共 ${formatBytes(pickedBytes.value)}`)) return
  error.value = ''
  const failed = []
  for (const [i, p] of todo.entries()) {
    discarding.value = `${i + 1}/${todo.length}`
    try { await discardOne(p) }
    catch (e) { failed.push(`${p.path}：${e.message}`) }
  }
  discarding.value = ''
  endBatch()
  if (failed.length) error.value = `${failed.length} 份沒有刪成：${failed.join('；')}`
  await search(false)
}

const totalPages = computed(() => Math.ceil(total.value / pageSize.value))

function goPage(n) {
  if (n < 0 || n >= totalPages.value) return
  page.value = n
  search(false, false)
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

const pageWindow = computed(() => {
  const n = totalPages.value, c = page.value
  if (n <= 9) return [...Array(n).keys()]
  const out = new Set([0, n - 1])
  for (let i = c - 2; i <= c + 2; i++) if (i >= 0 && i < n) out.add(i)
  const sorted = [...out].sort((a, b) => a - b)
  const withGaps = []
  for (let i = 0; i < sorted.length; i++) {
    if (i && sorted[i] - sorted[i - 1] > 1) withGaps.push('…')
    withGaps.push(sorted[i])
  }
  return withGaps
})

function trigrams(s) {
  const out = new Set()
  for (const w of s.toLowerCase().replace(/[^a-z0-9一-鿿]+/g, ' ').split(' ')) {
    if (!w) continue
    const p = `  ${w} `
    for (let i = 0; i + 3 <= p.length; i++) out.add(p.slice(i, i + 3))
  }
  return out
}

let queryGrams = { key: null, set: new Set() }

function trigramSim(a, b) {
  if (queryGrams.key !== a) queryGrams = { key: a, set: trigrams(a) }
  const ga = queryGrams.set, gb = trigrams(b)
  if (!ga.size || !gb.size) return 0
  let shared = 0
  for (const g of ga) if (gb.has(g)) shared++
  return shared / (ga.size + gb.size - shared)
}

function rank(raw, tag, clean, zh) {
  tag = tag.toLowerCase()
  clean = clean.toLowerCase()
  if (tag === raw || clean === raw) return 0
  if (zh === raw) return 1
  if (tag.startsWith(raw)) return 2
  const words = clean.split(' ')
  if (words.includes(raw)) return 3
  if (words.some(w => w.startsWith(raw))) return 4
  if (zh && zh.startsWith(raw)) return 5
  if (zh && zh.includes(raw)) return 6
  if (clean.includes(raw)) return 7
  if (raw.length >= 4) {
    const near = Math.max(trigramSim(raw, clean), zh ? trigramSim(raw, zh) : 0)
    if (near >= 0.4) return 8
  }
  return null
}

function onInput() {
  const raw = term.value.trim().replace(/^-/, '').toLowerCase()
  if (raw.length < 2) { showAC.value = false; return }
  const scored = []
  for (const item of vocabulary.value) {
    const clean = item.tag.replace(/_/g, ' ')
    const tier = rank(raw, item.tag, clean, item.zh)
    if (tier === null) continue
    scored.push({ tier, item, clean })
  }
  scored.sort((a, b) => a.tier - b.tier || b.item.works - a.item.works)
  const seen = new Set()
  const out = []
  for (const { tier, item, clean } of scored) {
    const key = item.key || item.tag
    if (seen.has(key)) continue
    seen.add(key)
    out.push({ en: item.tag, zh: item.zh, clean, guess: tier === 8,
               works: item.works, category: item.category })
    if (out.length >= 12) break
  }
  acResults.value = out
  acIndex.value = -1
  showAC.value = acResults.value.length > 0
}

function commitTerm(value) {
  const raw = (value ?? term.value).trim()
  if (!raw) return
  const negate = raw.startsWith('-')
  const tag = negate ? raw.slice(1) : raw
  if (!tag) return
  const target = negate ? f.notTags : f.tags
  if (!target.includes(tag)) target.push(tag)
  term.value = ''
  showAC.value = false
  search(true)
  nextTick(() => searchInputRef.value?.focus())
}

function onKeydown(e) {
  if (e.key === 'Escape') { showAC.value = false; return }
  if (!showAC.value) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    acIndex.value = Math.min(acIndex.value + 1, acResults.value.length - 1)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    acIndex.value = Math.max(acIndex.value - 1, -1)
  } else if (e.key === 'Tab') {
    e.preventDefault()
    if (acIndex.value >= 0) term.value = acResults.value[acIndex.value].en
  }
}

function onEnter(e) {
  const negate = term.value.trim().startsWith('-')
  if (showAC.value && acIndex.value >= 0) {
    e.preventDefault()
    commitTerm((negate ? '-' : '') + acResults.value[acIndex.value].en)
    return
  }
  commitTerm()
}

let detailSeq = 0
async function showDetail(work) {
  const mine = ++detailSeq
  if (selectedWork.value?.folder_path === work.folder_path) {
    selectedWork.value = null
    return
  }
  addingTag.value = false
  editingField.value = null
  const w = await fetch(`/api/work?path=${encodeURIComponent(work.folder_path)}`)
    .then(res => res.json()).catch(() => null)
  if (mine !== detailSeq) return
  selectedWork.value = w || work
  await loadOverrides(work.folder_path)
}

async function loadOverrides(path) {
  const out = await fetch(`/api/work/overrides?path=${encodeURIComponent(path)}`)
    .then(res => res.json()).catch(() => [])
  if (selectedWork.value?.folder_path === path) selectedOverrides.value = out
}

async function postOverride(path, body) {
  const res = await fetch(`/api/work/overrides?path=${encodeURIComponent(path)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const out = await res.json().catch(() => ({ error: 'no response' }))
  if (!res.ok || out.error) {
    overrideError.value = out.error || `HTTP ${res.status}`
    return false
  }
  overrideError.value = ''
  await reloadDrawer(path)
  return true
}

async function reloadDrawer(path) {
  await loadOverrides(path)
  try {
    const res = await fetch(`/api/work?path=${encodeURIComponent(path)}`)
    const w = await res.json()
    if (w && selectedWork.value?.folder_path === path) selectedWork.value = w
    const card = w && results.value.find(r => r.work?.folder_path === path)
    if (card) for (const k of CARD_FIELDS) card.work[k] = w[k]
  } catch {}
}
const CARD_FIELDS = ['artist', 'artist_display', 'series', 'series_display', 'rating', 'color_mode', 'language']

const FIELD_OF = { general: 'tag', character: 'character', copyright: 'series', artist: 'artist' }
const FIELD_LABEL = {
  tag: '標籤', character: '角色', series: '系列', artist: '作者',
  language: '語言', rating: '分級', color_mode: '色彩', work_type: '類型',
}

function negation(tag, category) {
  return selectedOverrides.value.find(o =>
    o.negated && o.field === FIELD_OF[category] && o.value === tag)
}

function withRemoved(tags, category) {
  const out = { ...(tags || {}) }
  for (const o of selectedOverrides.value)
    if (o.negated && o.field === FIELD_OF[category] && !(o.value in out)) out[o.value] = 0
  return out
}

async function dropOverride(o) {
  if (!selectedWork.value) return
  const path = selectedWork.value.folder_path
  const res = await fetch(`/api/overrides/${o.id}`, { method: 'DELETE' })
  if (!res.ok) { overrideError.value = `HTTP ${res.status}`; return }
  overrideError.value = ''
  await reloadDrawer(path)
}

async function removeTag(tag, category = 'general') {
  if (!selectedWork.value) return
  const done = negation(tag, category)
  if (done) return dropOverride(done)
  await postOverride(selectedWork.value.folder_path,
                     { action: 'remove', tag_category: category, original_tag: tag })
}

async function addTag() {
  const tag = newTagInput.value.trim()
  if (!tag || !selectedWork.value) return
  const path = selectedWork.value.folder_path
  await postOverride(path, { action: 'add', tag_category: 'general', new_tag: tag })
  newTagInput.value = ''
  addingTag.value = false
}

async function saveField(name) {
  if (!selectedWork.value) return
  const value = editFieldValue.value.trim()
  if (!value) return
  const path = selectedWork.value.folder_path
  await postOverride(path, { action: 'set_field', field_name: name, field_value: value })
  editingField.value = null
}

function startEdit(name, current) {
  editingField.value = name
  editFieldValue.value = current || ''
}

function isTagRemoved(tag, category = 'general') {
  return !!negation(tag, category)
}

function filterBy(dim, value) {
  if (!value) return
  if (dim === 'artist' || dim === 'series') f[dim] = value
  else toggleIn(f[dim], value)
  selectedWork.value = null
  search(true)
}

function tr(tag) {
  if (!tag) return ''
  const v = tagTranslations.value[tag]
  if (v && v !== tag && v !== tag.replace(/_/g, ' ')) return v
  return tag.replace(/_/g, ' ')
}
function label(key, value) {
  return VALUE_LABELS[value] || (SECTIONS.find(s => s.key === key)?.tagLike ? tr(value) : value)
}
function coverUrl(work, w = 480) {
  return `/api/cover?path=${encodeURIComponent(work.folder_path)}&w=${w}`
}
function displaySeries(work) {
  return work?.series_display || work?.series || ''
}
function displayArtist(work) {
  return work?.artist_display || work?.artist || ''
}
function colorClass(m) {
  return m === 'full_color' ? 'badge-color' : m === 'grayscale' ? 'badge-gray' : 'badge-partial'
}
function ratingClass(r) {
  return `rating-${r}`
}
function topTags(tags, n = 6) {
  const seen = new Set()
  const out = []
  for (const tag of Object.keys(tags || {})) {
    const label = tr(tag)
    if (seen.has(label)) continue
    seen.add(label)
    out.push(tag)
    if (out.length >= n) break
  }
  return out
}
function facetList(key) {
  const rows = facets.value[key] || []
  return collapsed[key] ? rows.slice(0, 8) : rows.slice(0, 40)
}

onMounted(async () => {
  await loadEnums()
  try {
    const [s, t, v] = await Promise.all([
      fetch('/api/stats'),
      fetch('/api/translations'),
      fetch('/api/vocabulary'),
    ])
    stats.value = await s.json()
    tagTranslations.value = await t.json()
    vocabulary.value = await v.json()
  } catch (e) {
    console.error('Failed to load metadata:', e)
  }
  const entry = { ...route.query }
  fromQuery(entry)
  if (entry.read) reading.value = { path: entry.read, title: '' }
  await search()
  if (entry.work) await showDetail({ folder_path: entry.work })
})
</script>

<template>
  <div class="app" @click="showAC = false" @contextmenu="e => e.target.closest('input, textarea') || e.preventDefault()">
    <header>
      <div class="brand">
        <h1>Polaris</h1>
        <span class="sub" v-if="stats">{{ stats.total_works }} 作品</span>
      </div>

      <div class="searchbox" @click.stop>
        <input
          ref="searchInputRef"
          v-model="term"
          type="text"
          placeholder="加入標籤篩選…（打 -tag 排除，Enter 送出）"
          @input="onInput"
          @keydown="onKeydown"
          @keydown.enter="onEnter"
          @focus="onInput"
        />
        <button class="go" @click="commitTerm()" :disabled="loading">
          {{ loading ? '…' : '加入' }}
        </button>
        <div class="ac" v-if="showAC">
          <div
            v-for="(item, i) in acResults"
            :key="item.en"
            :class="['ac-row', { on: i === acIndex }]"
            @click="commitTerm((term.trim().startsWith('-') ? '-' : '') + item.en)"
            @mouseenter="acIndex = i"
          >
            <span class="ac-zh">{{ item.zh || item.clean }}</span>
            <span class="ac-en">{{ item.clean }}</span>
            <span class="ac-cat" v-if="item.guess">相近</span>
            <span class="ac-cat" v-if="item.category !== 'general'">
              {{ item.category === 'character' ? '角色' : item.category }}
            </span>
            <span class="ac-n">{{ item.works }}</span>
          </div>
        </div>
      </div>

      <label class="mode-toggle viewswitch">
        <span :class="{ on: view === 'works' }" @click="setView('works')">作品</span>
        <span :class="{ on: view === 'copies' }" @click="setView('copies')">重複</span>
        <span :class="{ on: view === 'check' }" @click="setView('check')">檢查</span>
      </label>

      <div class="head-actions">
        <select v-model="pageSize" class="select" title="一頁顯示幾筆">
          <option v-for="n in PAGE_SIZES" :key="n" :value="n">{{ n }} 筆</option>
        </select>
        <select v-model="f.sort" @change="search(true)" class="select">
          <option value="relevance" v-if="hasTagTerms">相關度</option>
          <option value="newest">最新索引</option>
          <option value="oldest">最早索引</option>
          <option value="title">標題</option>
          <option value="pages">頁數</option>
        </select>
        <RouterLink to="/" class="go">管理</RouterLink>
      </div>
    </header>

    <div class="quickbar">
      <button v-for="q in QUICK" :key="q.label"
              :class="['chip', 'quick', { on: quickActive(q) }]"
              @click="toggleQuick(q)">{{ q.label }}</button>
      <span class="spacer"></span>
      <label class="mode-toggle" v-if="f.tags.length > 1">
        <span :class="{ on: f.mode === 'AND' }" @click="f.mode = 'AND'; search(true)">全部符合</span>
        <span :class="{ on: f.mode === 'OR' }" @click="f.mode = 'OR'; search(true)">任一符合</span>
      </label>
    </div>

    <div class="chipbar" v-if="hasFilters || similarRef">
      <span class="chipbar-label">篩選中</span>
      <button v-for="c in activeChips" :key="c.dim + c.raw"
              :class="['chip', 'active', c.kind]" @click="removeChip(c)">
        {{ c.label }} <em>✕</em>
      </button>
      <button v-if="similarRef" class="chip active sim" @click="clearSimilar">
        {{ similarMode === 'hybrid' ? '混合' : '相似' }}：{{ similarRef.split('\\').pop().slice(0, 28) }} <em>✕</em>
      </button>
      <button class="chip clear" @click="clearAll">清除全部</button>
    </div>

    <div class="error" v-if="error">{{ error }}</div>

    <div class="layout">
      <aside class="facets">
        <div class="facet-note" v-if="similarRef">
          相似度搜尋依向量距離排序，沒有分面統計。
        </div>
        <template v-else>
          <section v-for="s in SECTIONS" :key="s.key" class="facet"
                   v-show="s.kind === 'text' || (facets[s.key] || []).length">
            <h3>
              {{ s.label }}
              <button v-if="(facets[s.key] || []).length > 8"
                      class="more" @click="collapsed[s.key] = !collapsed[s.key]">
                {{ collapsed[s.key] ? '更多' : '收合' }}
              </button>
            </h3>

            <input v-if="s.kind === 'text'" class="facet-input"
                   :placeholder="s.placeholder" :value="f[s.key]"
                   @change="f[s.key] = $event.target.value; search(true)" />

            <ul class="facet-values">
              <li v-for="v in facetList(s.key)" :key="v.value"
                  :class="{ on: isFacetActive(s.key, v.value) }">
                <button class="fv" @click="toggleFacet(s.key, v.value)" :title="v.value">
                  <span class="fv-label">{{ label(s.key, v.value) }}</span>
                  <span class="fv-count">{{ v.count }}</span>
                </button>
                <button v-if="s.tagLike" class="fv-not" title="排除"
                        @click="excludeFacet(s.key, v.value)">−</button>
              </li>
            </ul>
          </section>
        </template>
      </aside>

      <main>
        <div class="resbar">
          <span v-if="loading">搜尋中…</span>
          <span v-else-if="similarRef && results.length">最相似的 {{ results.length }} 本</span>
          <span v-else>{{ total }} {{ view === 'copies' ? '件有複本的作品' : view === 'check' ? '項檢查結果' : '筆結果' }}<template v-if="totalPages > 1">，第 {{ page + 1 }} / {{ totalPages }} 頁</template></span>
        </div>

        <div class="batchbar checkbar" v-if="view === 'check'">
          <select class="select" :value="checkJob || ''"
                  @change="setCheckJob($event.target.value)">
            <option v-if="!checkJobs.length" value="">還沒有檢查過任何資料夾</option>
            <option v-for="j in checkJobs" :key="j.id" :value="j.id">{{ j.path || (j.paths || []).join('、') }}</option>
          </select>
          <span v-if="checkRun && (checkRun.state === 'queued' || checkRun.state === 'running')">
            檢查中 {{ checkRun.done }} / {{ checkRun.total || '?' }}
          </span>
          <button v-for="v in ['same', 'near', 'new', 'error']" :key="v"
                  v-show="checkCounts[v]"
                  :class="['chip', { on: checkFilter === v }]"
                  @click="filterVerdict(v)">
            {{ VERDICT_LABEL[v] }} {{ checkCounts[v] }}
          </button>
        </div>

        <div class="batchbar" v-if="view !== 'works' && results.length">
          <button class="mini" v-if="!batch" @click="batch = true">批次選擇</button>
          <template v-else>
            <span>已選 {{ pickedPlaces.length }} 份（{{ formatBytes(pickedBytes) }}）<template v-if="view === 'copies'">，每件作品至少留一份</template></span>
            <button class="btn del" :disabled="!pickedPlaces.length || !!discarding"
                    @click="discardPicked">
              {{ discarding ? `刪除中 ${discarding}` : '丟進回收筒' }}
            </button>
            <button class="btn" :disabled="!!discarding" @click="endBatch">取消</button>
          </template>
        </div>

        <div class="grid" v-if="results.length && view === 'works'">
          <article v-for="r in results" :key="r.work.folder_path" class="card"
                   @click="showDetail(r.work)">
            <div class="cover">
              <img :src="coverUrl(r.work)" loading="lazy" />
              <div class="hover">
                <button class="lead" @click.stop="read(r.work)">觀看</button>
                <button @click.stop="findSimilar(r.work)">類似</button>
                <button @click.stop="useAsBase(r.work)">基準</button>
              </div>
              <span class="pages" v-if="r.work.has_video && r.work.duration_s">{{ formatDuration(r.work.duration_s) }}</span>
              <span class="pages" v-else-if="r.work.total_images">{{ r.work.total_images }}P</span>
              <span class="score" v-if="f.sort === 'relevance' && r.score">
                {{ (r.score * 100).toFixed(0) }}
              </span>
            </div>
            <div class="body">
              <div class="title" :title="r.work.title || r.work.folder_name">
                {{ r.work.title || r.work.folder_name }}
              </div>
              <div class="meta">
                <button v-if="r.work.artist" class="link"
                        @click.stop="filterBy('artist', r.work.artist)">{{ displayArtist(r.work) }}</button>
                <button v-if="r.work.series" class="link dim"
                        @click.stop="filterBy('series', r.work.series)">{{ displaySeries(r.work) }}</button>
              </div>
              <div class="badges">
                <span v-if="r.work.color_mode"
                      :class="['badge', colorClass(r.work.color_mode)]">
                  {{ VALUE_LABELS[r.work.color_mode] || r.work.color_mode }}
                </span>
                <span v-if="r.work.rating"
                      :class="['badge', ratingClass(r.work.rating)]">
                  {{ VALUE_LABELS[r.work.rating] || r.work.rating }}
                </span>
                <span class="badge badge-lang" v-if="r.work.language">{{ r.work.language }}</span>
              </div>
              <div class="tags">
                <span v-for="tag in topTags(r.work.general_tags)" :key="tag" class="tag"
                      :title="tag">{{ tr(tag) }}</span>
              </div>
            </div>
          </article>
        </div>

        <div class="duprows" v-else-if="results.length">
          <article v-for="r in results" :key="r.work.folder_path" class="duprow">
            <div class="dupinfo" @click="view === 'check' ? read(r.work) : showDetail(r.work)">
              <div class="title" :title="r.work.title || r.work.folder_name">
                {{ r.work.title || r.work.folder_name }}
              </div>
              <div class="meta">
                <button v-if="r.work.artist" class="link"
                        @click.stop="filterBy('artist', r.work.artist)">{{ displayArtist(r.work) }}</button>
                <button v-if="r.work.series" class="link dim"
                        @click.stop="filterBy('series', r.work.series)">{{ displaySeries(r.work) }}</button>
              </div>
              <div class="badges">
                <span class="badge" v-if="r.verdict">
                  {{ VERDICT_LABEL[r.verdict] }}<template v-if="r.score"> {{ (r.score * 100).toFixed(0) }}</template>
                </span>
                <span class="badge" v-if="similarRef && r.score">相似 {{ (r.score * 100).toFixed(0) }}</span>
                <span class="badge" v-if="r.work.has_video && r.work.duration_s">{{ formatDuration(r.work.duration_s) }}</span>
                <span class="badge" v-else-if="r.work.total_images">{{ r.work.total_images }}P</span>
                <span v-if="r.work.color_mode"
                      :class="['badge', colorClass(r.work.color_mode)]">
                  {{ VALUE_LABELS[r.work.color_mode] || r.work.color_mode }}
                </span>
                <span v-if="r.work.rating"
                      :class="['badge', ratingClass(r.work.rating)]">
                  {{ VALUE_LABELS[r.work.rating] || r.work.rating }}
                </span>
                <span class="badge badge-lang" v-if="r.work.language">{{ r.work.language }}</span>
              </div>
              <div class="tags">
                <span v-for="tag in topTags(r.work.general_tags)" :key="tag" class="tag"
                      :title="tag">{{ tr(tag) }}</span>
              </div>
              <div class="dupacts">
                <button class="mini" @click.stop="read(r.work)">觀看</button>
                <button class="mini" @click.stop="findSimilar(r.work)">類似</button>
                <button class="mini" @click.stop="useAsBase(r.work)">基準</button>
              </div>
            </div>

            <div class="dupplaces">
              <div v-for="p in r.places" :key="p.path"
                   :class="['dupplace', { copy: p.copy, gone: !p.exists, picked: picked.has(p.path) }]">
                <div class="dupcover"
                     @click="batch ? togglePick(r, p) : (p.exists && read({ ...r.work, folder_path: p.path }))">
                  <img v-if="p.exists" :src="coverUrl({ folder_path: p.path }, 360)" loading="lazy" alt="" />
                  <span v-else>已不在磁碟上</span>
                </div>
                <div class="dupmeta">
                  <label v-if="batch" class="duppick">
                    <input type="checkbox" :checked="picked.has(p.path)"
                           :disabled="!canPick(r, p) || !!discarding" @change="togglePick(r, p)" />
                    選取
                  </label>
                  <span :class="['duptag', p.copy ? 'copy' : 'main']">
                    {{ view === 'check' ? (p.copy ? '庫裡已有' : '這一份') : (p.copy ? '複本' : '主要') }}
                  </span>
                  <span>{{ p.collection }}</span>
                  <span v-if="p.score">相似 {{ (p.score * 100).toFixed(0) }}</span>
                  <span>{{ formatBytes(p.bytes) }}</span>
                </div>
                <div class="loc">{{ p.path }}</div>
                <div class="dupacts">
                  <button class="mini" :disabled="!p.exists" @click="revealPlace(p)">在檔案總管中顯示</button>
                  <button class="mini del" v-if="!batch && !p.locked"
                          :disabled="!p.exists || (view === 'copies' && alive(r) < 2) || !!discarding"
                          @click="discardPlace(p)">
                    {{ discarding === p.path ? '刪除中…' : '丟進回收筒' }}
                  </button>
                </div>
              </div>
            </div>
          </article>
        </div>

        <div class="empty" v-else-if="!loading">
          <p>沒有符合的作品。</p>
          <button v-if="hasFilters" class="chip clear" @click="clearAll">清除篩選</button>
        </div>

        <nav class="pager" v-if="totalPages > 1">
          <button :disabled="page === 0" @click="goPage(page - 1)">‹</button>
          <template v-for="(p, i) in pageWindow" :key="i">
            <span v-if="p === '…'" class="gap">…</span>
            <button v-else :class="{ on: p === page }" @click="goPage(p)">{{ p + 1 }}</button>
          </template>
          <button :disabled="page >= totalPages - 1" @click="goPage(page + 1)">›</button>
        </nav>
      </main>
    </div>

    <div class="drawer-scrim" v-if="selectedWork" @click="selectedWork = null"></div>
    <aside class="drawer" v-if="selectedWork">
      <div class="drawer-head">
        <h2>{{ selectedWork.folder_name }}</h2>
        <button class="icon" @click="selectedWork = null">✕</button>
      </div>
      <div class="drawer-body">
        <img class="drawer-cover" :src="coverUrl(selectedWork, 960)" />

        <div class="row" v-if="selectedWork.artist">
          <b>作者</b>
          <template v-if="editingField !== 'artist'">
            <button class="link" @click="filterBy('artist', selectedWork.artist)">{{ selectedWork.artist }}</button>
            <button class="mini" @click="startEdit('artist', selectedWork.artist)">✏️</button>
          </template>
          <template v-else>
            <input v-model="editFieldValue" class="edit" @keydown.enter="saveField('artist')" @keydown.esc="editingField = null" />
            <button class="mini ok" @click="saveField('artist')">✓</button>
            <button class="mini" @click="editingField = null">✕</button>
          </template>
        </div>


        <div class="row">
          <b>系列</b>
          <template v-if="editingField !== 'series'">
            <button class="link" v-if="selectedWork.series" @click="filterBy('series', selectedWork.series)">
              {{ displaySeries(selectedWork) }}
            </button>
            <span v-else class="muted">(無)</span>
            <button class="mini" @click="startEdit('series', selectedWork.series)">✏️</button>
          </template>
          <template v-else>
            <input v-model="editFieldValue" class="edit" @keydown.enter="saveField('series')" @keydown.esc="editingField = null" />
            <button class="mini ok" @click="saveField('series')">✓</button>
            <button class="mini" @click="editingField = null">✕</button>
          </template>
        </div>

        <div class="row" v-if="selectedWork.language">
          <b>語言</b>
          <button class="link" @click="filterBy('language', selectedWork.language)">{{ selectedWork.language }}</button>
        </div>

        <div class="row">
          <b>評級</b>
          <template v-if="editingField !== 'rating'">
            <span v-if="selectedWork.rating"
                  :class="['badge', ratingClass(selectedWork.rating)]">
              {{ VALUE_LABELS[selectedWork.rating] || selectedWork.rating }}
            </span>
            <span v-else class="badge dim">未判定</span>
            <button class="mini" @click="startEdit('rating', selectedWork.rating)">✏️</button>
          </template>
          <template v-else>
            <select v-model="editFieldValue" class="edit" @change="saveField('rating')">
              <option value="" disabled>未判定</option>
              <option v-for="c in enums.rating" :key="c.slug" :value="c.slug">
                {{ enumLabel(c) }}
              </option>
            </select>
            <button class="mini" @click="editingField = null">✕</button>
          </template>
        </div>

        <div class="row">
          <b>色彩</b>
          <template v-if="editingField !== 'color_mode'">
            <span v-if="selectedWork.color_mode"
                  :class="['badge', colorClass(selectedWork.color_mode)]">
              {{ VALUE_LABELS[selectedWork.color_mode] || selectedWork.color_mode }}
            </span>
            <span v-else class="badge dim">未判定</span>
            <button class="mini" @click="startEdit('color_mode', selectedWork.color_mode)">✏️</button>
          </template>
          <template v-else>
            <select v-model="editFieldValue" class="edit" @change="saveField('color_mode')">
              <option value="" disabled>未判定</option>
              <option v-for="c in enums.color_mode" :key="c.slug" :value="c.slug">
                {{ enumLabel(c) }}
              </option>
            </select>
            <button class="mini" @click="editingField = null">✕</button>
          </template>
        </div>

        <div class="row"><b>類型</b> <span>{{ VALUE_LABELS[selectedWork.work_type] || selectedWork.work_type || '未判定' }}</span></div>

        <div class="block" v-if="Object.keys(withRemoved(selectedWork.copyright_tags, 'copyright')).length">
          <b>作品</b>
          <div class="cloud">
            <span v-for="(c, tag) in withRemoved(selectedWork.copyright_tags, 'copyright')" :key="tag"
                  :class="['tag', 'tag-copy', 'tag-edit', { removed: isTagRemoved(tag, 'copyright') }]"
                  :title="tag">
              <button class="tag-main" @click="filterBy('copyright', tag)">{{ tr(tag) }}</button>
              <button class="tag-x" @click.stop="removeTag(tag, 'copyright')"
                      :title="isTagRemoved(tag, 'copyright') ? '取消移除' : '這部不是這個系列'">
                {{ isTagRemoved(tag, 'copyright') ? '↺' : '✕' }}
              </button>
            </span>
          </div>
        </div>

        <div class="block" v-if="Object.keys(withRemoved(selectedWork.character_tags, 'character')).length">
          <b>角色</b>
          <div class="cloud">
            <span v-for="(c, tag) in withRemoved(selectedWork.character_tags, 'character')" :key="tag"
                  :class="['tag', 'tag-char', 'tag-edit', { removed: isTagRemoved(tag, 'character') }]"
                  :title="tag">
              <button class="tag-main" @click="filterBy('character', tag)">{{ tr(tag) }}</button>
              <button class="tag-x" @click.stop="removeTag(tag, 'character')"
                      :title="isTagRemoved(tag, 'character') ? '取消移除' : '這部沒有這個角色'">
                {{ isTagRemoved(tag, 'character') ? '↺' : '✕' }}
              </button>
            </span>
          </div>
        </div>

        <div class="block">
          <b>標籤</b>
          <button class="mini" @click="addingTag = !addingTag">＋</button>
          <div v-if="addingTag" class="addrow">
            <input v-model="newTagInput" class="edit" placeholder="新增標籤 (英文)"
                   @keydown.enter="addTag" @keydown.esc="addingTag = false" />
            <button class="mini ok" @click="addTag">✓</button>
          </div>
          <div class="cloud">
            <span v-for="(conf, tag) in withRemoved(selectedWork.general_tags, 'general')" :key="tag"
                  :class="['tag', 'tag-edit', { removed: isTagRemoved(tag) }]"
                  :title="`${tag} (${Number(conf).toFixed(3)})`">
              <button class="tag-main" @click="filterBy('tags', tag)">{{ tr(tag) }}</button>
              <button class="tag-x" @click.stop="removeTag(tag)"
                      :title="isTagRemoved(tag) ? '取消移除' : '移除'">
                {{ isTagRemoved(tag) ? '↺' : '✕' }}
              </button>
            </span>
          </div>
        </div>

        <div class="block err" v-if="overrideError">
          <div class="label">未能儲存</div>
          <div>{{ overrideError }}</div>
        </div>
        <div class="block" v-if="selectedOverrides.length">
          <b>修正紀錄</b>
          <div class="overrides">
            <div v-for="o in selectedOverrides" :key="o.id" class="ov">
              <span v-if="o.negated" class="ov-rm">移除{{ FIELD_LABEL[o.field] || o.field }} {{ tr(o.value) }}</span>
              <span v-else class="ov-add">{{ FIELD_LABEL[o.field] || o.field }} → {{ tr(o.value) }}</span>
              <button class="mini" @click="dropOverride(o)" title="收回這筆修正">✕</button>
            </div>
          </div>
        </div>

        <div class="drawer-foot">
          <button class="wide ok" @click="read(selectedWork)">觀看</button>
          <div class="addrow">
            <button class="wide" @click="findSimilar(selectedWork); selectedWork = null">
              找類似作品
            </button>
            <button class="wide" @click="openOnDesktop(selectedWork)">在桌面開啟</button>
          </div>
          <div class="block err" v-if="openError">{{ openError }}</div>
          <code class="path">{{ selectedWork.folder_path }}</code>
        </div>
      </div>
    </aside>

    <Reader v-if="reading" :path="reading.path" :title="reading.title"
            @close="closeReader" />
  </div>
</template>

<style>
.viewswitch { flex-shrink: 0; }
.duprows { display: flex; flex-direction: column; }
.duprow {
  display: grid; grid-template-columns: 17rem minmax(0, 1fr); gap: 1.2rem;
  padding: 1rem 0; border-top: 1px solid var(--border);
}
.duprow:first-child { border-top: 0; padding-top: 0; }
.dupinfo { display: flex; flex-direction: column; gap: 0.35rem; min-width: 0; cursor: pointer; }
.dupinfo:hover .title { color: var(--accent2); }
.dupacts { display: flex; gap: 0.3rem; flex-wrap: wrap; }
.dupplaces {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr)); gap: 0.9rem;
}
.dupplace { display: flex; flex-direction: column; gap: 0.35rem; min-width: 0; }
.dupplace.gone { opacity: 0.5; }
.dupcover {
  height: 11rem; background: var(--surface2); border: 1px solid var(--border);
  border-radius: 8px; overflow: hidden; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  font-size: 0.78rem; color: var(--text2);
}
.dupplace.copy .dupcover { border-color: var(--orange); }
.dupcover:hover { border-color: var(--accent); }
.dupcover img { width: 100%; height: 100%; object-fit: contain; display: block; }
.dupmeta { display: flex; align-items: center; gap: 0.6rem; font-size: 0.76rem; color: var(--text2); }
.duptag {
  font-size: 0.7rem; padding: 0.05rem 0.45rem; border-radius: 4px;
  border: 1px solid var(--border);
}
.duptag.main { border-color: var(--green); color: var(--green); }
.duptag.copy { border-color: var(--orange); color: var(--orange); }
.mini.del:hover:enabled { color: var(--red); }
.batchbar {
  position: sticky; top: 4.2rem; z-index: 25; background: var(--bg);
  display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap;
  padding: 0.5rem 0; margin-bottom: 0.6rem; border-bottom: 1px solid var(--border);
  font-size: 0.82rem; color: var(--text2);
}
.batchbar .btn.del:enabled { border-color: var(--red); color: var(--red); }
.duppick { display: flex; align-items: center; gap: 0.25rem; cursor: pointer; color: var(--text); }
.dupplace.picked .dupcover { border-color: var(--red); box-shadow: 0 0 0 1px var(--red); }
.dupplace.picked .dupcover img { opacity: 0.5; }
@media (max-width: 1000px) { .duprow { grid-template-columns: 1fr; } }
</style>
