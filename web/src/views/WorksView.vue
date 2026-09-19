<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import EmptyState from '../admin/EmptyState.vue'
import { n } from '../admin/format'
import { usePoll } from '../admin/poll'
import { useToast } from '../admin/toast'
import { concepts, forget, label } from '../works/concepts'
import WorkDetail from '../works/WorkDetail.vue'

const FILTERS = [
  { key: '', label: '全部' },
  { key: 'untyped', label: '類型未判定' },
  { key: 'no_artist', label: '缺作者' },
  { key: 'claimed', label: '已修改' },
  { key: 'negated', label: '已排除' },
]
const PAGE = 50
const LINKED = ['artist', 'character', 'series', 'tag']

const route = useRoute()
const router = useRouter()
const toast = useToast()
const poll = usePoll()

const q = ref('')
const items = ref([])
const total = ref(0)
const counts = ref({})
const loading = ref(true)
const types = ref({})
const conceptName = ref('')
forget()

const sel = computed(() => Number(route.query.id) || null)
const concept = computed(() => Number(route.query.concept) || null)
const setQuery = patch => router.replace({ query: { ...route.query, ...patch } })
const filter = computed({
  get: () => (FILTERS.some(f => f.key === route.query.filter) ? route.query.filter : ''),
  set: v => setQuery({ filter: v || undefined }),
})

function url(f, limit, offset) {
  const p = new URLSearchParams({ limit, offset })
  if (f) p.set('filter', f)
  if (concept.value) p.set('concept', concept.value)
  if (q.value.trim()) p.set('q', q.value.trim())
  return `/api/works?${p}`
}

let seq = 0
async function load() {
  const mine = ++seq
  countSeq++
  loading.value = true
  try {
    const [page, ...totals] = await Promise.all([
      api(url(filter.value, PAGE, 0)),
      ...FILTERS.map(f => api(url(f.key, 1, 0))),
    ])
    if (mine !== seq) return
    items.value = page.items
    total.value = page.total
    counts.value = Object.fromEntries(FILTERS.map((f, i) => [f.key, totals[i].total]))
  } catch (e) { toast.bad(e) }
  if (mine === seq) loading.value = false
}

async function more() {
  try {
    const page = await api(url(filter.value, PAGE, items.value.length))
    items.value = [...items.value, ...page.items]
    total.value = page.total
  } catch (e) { toast.bad(e) }
}

let countSeq = 0
async function recount() {
  const mine = ++countSeq
  try {
    const totals = await Promise.all(FILTERS.map(f => api(url(f.key, 1, 0))))
    if (mine !== countSeq) return
    counts.value = Object.fromEntries(FILTERS.map((f, i) => [f.key, totals[i].total]))
    if (counts.value[filter.value] !== total.value) load()
  } catch (e) { toast.bad(e) }
}

let typing = null
watch(q, () => { clearTimeout(typing); typing = setTimeout(load, 300) })
watch([filter, concept], load)
watch(() => poll.finished, load)

watch(concept, async id => {
  conceptName.value = ''
  if (!id) return
  try {
    const all = (await Promise.all(LINKED.map(concepts))).flat()
    const c = all.find(x => x.id === id)
    conceptName.value = c ? label(c) : `#${id}`
  } catch (e) { toast.bad(e) }
}, { immediate: true })

onMounted(async () => {
  load()
  try {
    types.value = Object.fromEntries((await concepts('work_type')).map(c => [c.slug, label(c)]))
  } catch (e) { toast.bad(e) }
})

function changed(work) {
  const row = items.value.find(w => w.id === work.id)
  if (row) {
    row.claims = work.fields.reduce((s, f) => s + f.added.length, 0)
    row.negations = work.fields.reduce((s, f) => s + f.negated.length, 0)
    const type = work.fields.find(f => f.field === 'work_type')?.result[0]
    row.work_type = type?.slug ?? null
    row.artist = work.fields.find(f => f.field === 'artist')?.result[0]?.slug ?? null
  }
  recount()
  poll.refresh()
}
</script>

<template>
  <div class="md wk">
    <div class="card md-list">
      <div class="card-h">
        <div class="seg">
          <button v-for="f in FILTERS" :key="f.key" :class="{ on: filter === f.key }"
                  @click="filter = f.key">{{ f.label }}<i>{{ n(counts[f.key]) }}</i></button>
        </div>
        <input v-model="q" type="search" class="wk-q" placeholder="搜尋" title="標題或路徑" />
      </div>
      <div v-if="concept" class="wk-strip">
        <span class="wk-chip">概念：{{ conceptName || '載入中' }}
          <button aria-label="移除篩選" title="移除篩選" @click="setQuery({ concept: undefined })">✕</button>
        </span>
      </div>

      <div class="md-scroll">
        <button v-for="w in items" :key="w.id" type="button"
                :class="['row-g', 'wk-row', { on: w.id === sel }]" @click="setQuery({ id: w.id })">
          <span class="wk-title" :title="w.folder_path">{{ w.title }}</span>
          <span class="wk-meta">
            <span>{{ w.collection }}</span>
            <span>{{ types[w.work_type] || w.work_type || '—' }}</span>
            <span class="wk-artist">{{ w.artist || '—' }}</span>
          </span>
          <span class="wk-tags">
            <span v-if="w.claims" class="badge blue">手動 {{ n(w.claims) }}</span>
            <span v-if="w.negations" class="badge orange">已排除 {{ n(w.negations) }}</span>
          </span>
        </button>
        <EmptyState v-if="!items.length" :title="loading ? '載入中' : '沒有資料'" />
      </div>
      <div class="card-f wk-foot">
        <span>{{ n(items.length) }} / {{ n(total) }}</span>
        <button v-if="items.length < total" class="btn sm" @click="more">載入更多</button>
      </div>
    </div>

    <WorkDetail v-if="sel" :key="sel" :id="sel" @changed="changed"
                @close="setQuery({ id: undefined })" />
    <div v-else class="card md-detail wk-none">
      <EmptyState title="未選取" />
    </div>
  </div>
</template>

<style>
.admin .wk.md { grid-template-columns: minmax(0, 2fr) minmax(420px, 3fr); }
.admin .wk .card-h { flex-wrap: wrap; }
.admin .wk-q { margin-left: auto; width: 14rem; height: 30px; }
.admin .wk-strip { padding: 8px 16px; background: var(--surface2); border-bottom: 1px solid var(--border-soft); }
.admin .wk-chip {
  display: inline-flex; align-items: center; gap: 6px; height: 26px; padding: 0 4px 0 10px;
  border-radius: var(--r-full); background: var(--accent-soft); color: var(--accent2); font-size: var(--fs-sm);
}
.admin .wk-chip button { width: 20px; height: 20px; border-radius: var(--r-full); color: inherit; }
.admin .wk-chip button:hover { background: var(--accent-soft2); }
.admin .wk-row {
  width: 100%; text-align: left; cursor: pointer; grid-template-columns: minmax(0, 1fr) 11rem;
  grid-template-areas: "t b" "m b"; row-gap: 2px; content-visibility: auto; contain-intrinsic-size: auto 56px;
}
.admin .wk-title { grid-area: t; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
.admin .wk-meta { grid-area: m; display: flex; gap: var(--s-3); min-width: 0; font-size: var(--fs-xs); color: var(--text3); }
.admin .wk-meta > span { white-space: nowrap; }
.admin .wk-artist { overflow: hidden; text-overflow: ellipsis; }
.admin .wk-tags { grid-area: b; display: flex; gap: 4px; justify-content: flex-end; }
.admin .wk-foot { display: flex; align-items: center; justify-content: space-between; }
.admin .wk-none { justify-content: center; }
</style>
