<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import ArmedButton from '../admin/ArmedButton.vue'
import EmptyState from '../admin/EmptyState.vue'
import { n } from '../admin/format'
import { usePoll } from '../admin/poll'
import { useToast } from '../admin/toast'
import ConceptDetail from './ConceptDetail.vue'

const KINDS = [
  { key: 'artist', label: '作者', cat: 'artist' },
  { key: 'character', label: '角色', cat: 'character' },
  { key: 'series', label: '系列', cat: 'copyright' },
  { key: 'tag', label: '標籤', cat: 'general' },
]

const poll = usePoll()
const toast = useToast()
const route = useRoute()
const router = useRouter()

const kind = ref(KINDS.some(k => k.key === route.query.kind) ? route.query.kind : 'character')
const view = ref(route.query.view || 'all')
watch([kind, view], ([k, v]) => router.replace({
  query: { ...route.query, kind: k === 'character' ? undefined : k, view: v === 'all' ? undefined : v },
}))
const q = ref('')
const sel = ref(null)
const lists = reactive(Object.fromEntries(KINDS.map(k => [k.key, []])))
const unplaced = ref({ asserts: false, total: 0, tags: [] })
const conflicts = ref([])
const loading = ref(true)

const here = computed(() => KINDS.find(k => k.key === kind.value))
const list = computed(() => lists[kind.value])
const current = computed(() => list.value.find(c => c.id === sel.value) || null)
const bySlug = (k, slug) => lists[k].find(c => c.slug.toLowerCase() === slug.toLowerCase())

const clashes = computed(() =>
  conflicts.value.filter(x => x.concepts.some(c => c.kind === kind.value)))
const clashing = computed(() =>
  new Set(clashes.value.flatMap(x => x.concepts.map(c => c.slug.toLowerCase()))))
const parents = computed(() => 'parent_id' in (lists.character[0] || {}))
const orphans = computed(() =>
  kind.value === 'character' && parents.value ? list.value.filter(c => c.parent_id == null) : [])

const VIEWS = computed(() => [
  { key: 'all', label: '全部', count: list.value.length },
  unplaced.value.asserts && { key: 'unplaced', label: '未收錄', count: unplaced.value.total },
  { key: 'conflicts', label: '衝突', count: clashes.value.length },
  kind.value === 'character' && parents.value && { key: 'orphans', label: '缺系列', count: orphans.value.length },
].filter(Boolean))

const needle = computed(() => q.value.trim().toLowerCase())
const has = s => (s || '').toLowerCase().includes(needle.value)
const hit = c => !needle.value || has(c.slug) || has(c.display) ||
  c.terms.some(t => has(t.term))

const shownConcepts = computed(() =>
  (view.value === 'orphans' ? orphans.value : list.value).filter(hit))
const shownUnplaced = computed(() =>
  unplaced.value.tags.filter(t => !needle.value || has(t.tag) || has(t.zh)))
const shownClashes = computed(() =>
  clashes.value.filter(x => !needle.value || has(x.term) ||
    x.concepts.some(c => has(c.slug) || has(c.name))))

async function loadSide(fresh = () => true) {
  const k = kind.value
  const [u, cf] = await Promise.all([
    api(`/api/vocabulary/unplaced?category=${here.value.cat}&kind=${kind.value}&limit=2000`),
    api('/api/terms/conflicts'),
  ])
  if (k !== kind.value || !fresh()) return false
  unplaced.value = u
  conflicts.value = cf
  return true
}

function keepView() {
  if (!VIEWS.value.some(v => v.key === view.value)) view.value = 'all'
}

function findConflicts() {
  if (route.query.kind || view.value !== 'conflicts' || clashes.value.length) return
  const k = KINDS.find(k => conflicts.value.some(x => x.concepts.some(c => c.kind === k.key)))
  if (k) kind.value = k.key
}

let seq = 0
async function reload() {
  const mine = ++seq
  try {
    const [all] = await Promise.all([
      Promise.all(KINDS.map(k => api(`/api/concepts?kind=${k.key}`))),
      loadSide(() => mine === seq),
    ])
    if (mine !== seq) return
    KINDS.forEach((k, i) => { lists[k.key] = all[i] })
    keepView()
  } catch (e) { toast.bad(e) }
  loading.value = false
  poll.refresh()
}

watch(kind, async () => {
  sel.value = null
  unplaced.value = { asserts: false, total: 0, tags: [] }
  try { if (await loadSide()) keepView() } catch (e) { toast.bad(e) }
})
watch(() => poll.finished, reload)
onMounted(async () => {
  await reload()
  findConflicts()
})

function open(k, id) {
  if (k !== kind.value) {
    kind.value = k
    nextTick(() => { sel.value = id })
  } else sel.value = id
}

function openConflict(c) {
  const hitC = bySlug(c.kind, c.slug)
  if (hitC) open(c.kind, hitC.id)
}

async function place(t) {
  try {
    const r = await api('/api/concepts', {
      method: 'POST',
      body: { kind: kind.value, slug: t.tag, display_zh: t.zh || null, terms: [t.tag], search_only: false },
    })
    toast.ok(`已收錄「${t.tag}」`)
    await reload()
    sel.value = r.id
  } catch (e) { toast.bad(e) }
}

async function placeAll() {
  try {
    const r = await api(`/api/vocabulary/place?category=${here.value.cat}&kind=${kind.value}`, { method: 'POST' })
    const jobs = r.jobs.length ? `，重新計算 ${r.jobs.map(j => `#${j}`).join('、')} 已排入` : ''
    toast.ok(`已收錄 ${n(r.placed)} 個${jobs}`, { timeout: 8000 })
    await reload()
  } catch (e) { toast.bad(e) }
}

const adding = ref(null)
async function startAdd() {
  adding.value = { slug: '' }
  await nextTick()
  document.querySelector('.admin .voc-add input')?.focus()
}
async function create() {
  const slug = adding.value.slug.trim()
  if (!slug) return toast.bad('名稱不能空白')
  const had = bySlug(kind.value, slug)
  if (had) {
    adding.value = null
    view.value = 'all'
    sel.value = had.id
    return toast.show(`已有「${had.slug}」`)
  }
  try {
    const r = await api('/api/concepts', { method: 'POST', body: { kind: kind.value, slug } })
    adding.value = null
    await reload()
    view.value = 'all'
    sel.value = r.id
    toast.ok(`已新增「${slug}」`)
  } catch (e) { toast.bad(e) }
}

async function changed(id) {
  await reload()
  sel.value = id === undefined ? sel.value : id
}
</script>

<template>
  <div class="md voc">
    <div class="card md-list">
      <div class="card-h">
        <div class="seg">
          <button v-for="k in KINDS" :key="k.key" :class="{ on: kind === k.key }"
                  @click="kind = k.key">{{ k.label }}<i>{{ n(lists[k.key].length) }}</i></button>
        </div>
        <button class="btn sm voc-new" @click="startAdd">新增</button>
      </div>
      <div class="card-h">
        <div class="seg">
          <button v-for="v in VIEWS" :key="v.key" :class="{ on: view === v.key }"
                  @click="view = v.key">{{ v.label }}<i>{{ n(v.count) }}</i></button>
        </div>
        <input v-model="q" type="search" class="voc-q" placeholder="搜尋" />
      </div>

      <div v-if="adding" class="voc-strip voc-add">
        <input v-model="adding.slug" placeholder="名稱" @keydown.enter="create" @keydown.esc="adding = null" />
        <button class="btn quiet sm" @click="adding = null">取消</button>
        <button class="btn primary sm" @click="create">新增</button>
      </div>

      <div v-if="view === 'unplaced'" class="voc-strip">
        <span>{{ n(unplaced.total) }} 個 · {{ n(unplaced.works) }} 部作品</span>
        <ArmedButton class="sm voc-all" :label="`全部收錄 ${n(unplaced.total)}`" confirm="確定收錄？"
                     busy-label="收錄中" :disabled="!unplaced.total" :run="placeAll" />
      </div>

      <div class="md-scroll">
        <template v-if="view === 'unplaced'">
          <div v-for="t in shownUnplaced" :key="t.tag" class="row-g voc-row voc-un">
            <span class="voc-slug">{{ t.tag }}</span>
            <span class="voc-zh">{{ t.zh || '' }}</span>
            <span class="num-c">{{ n(t.works) }}</span>
            <button class="btn sm soft" @click="place(t)">收錄</button>
          </div>
          <EmptyState v-if="!shownUnplaced.length" title="沒有資料" />
        </template>

        <template v-else-if="view === 'conflicts'">
          <div v-for="x in shownClashes" :key="x.term" class="row-g voc-row voc-cf">
            <span class="lit-c">{{ x.term }}</span>
            <span class="voc-cfs">
              <button v-for="c in x.concepts" :key="c.id" class="btn sm"
                      :title="c.slug" @click="openConflict(c)">{{ c.name }}</button>
            </span>
          </div>
          <EmptyState v-if="!shownClashes.length" title="沒有資料" />
        </template>

        <template v-else>
          <button v-for="c in shownConcepts" :key="c.id" type="button"
                  :class="['row-g', 'voc-row', 'voc-c', { on: c.id === sel }]" @click="sel = c.id">
            <span class="voc-slug">{{ c.slug }}</span>
            <span class="voc-zh">{{ c.display || '' }}</span>
            <span class="voc-tags">
              <span v-if="clashing.has(c.slug.toLowerCase())" class="badge red">衝突</span>
              <span v-if="kind === 'character' && parents && c.parent_id == null" class="badge orange">缺系列</span>
            </span>
          </button>
          <EmptyState v-if="!shownConcepts.length" :title="loading ? '載入中' : '沒有資料'" />
        </template>
      </div>
    </div>

    <ConceptDetail v-if="current" :key="current.id" :concept="current" :lists="lists"
                   @changed="changed" @open="open" @close="sel = null" />
    <div v-else class="card md-detail voc-none">
      <EmptyState title="未選取" />
    </div>
  </div>
</template>

<style>
.admin .voc.md { height: calc(100vh - var(--bar-h) - 3 * var(--s-5) - 41px); }
.admin .voc .card-h { flex-wrap: wrap; }
.admin .voc-new { margin-left: auto; }
.admin .voc-q { margin-left: auto; width: 12rem; height: 30px; }
.admin .voc-strip {
  display: flex; align-items: center; gap: var(--s-3); flex-wrap: wrap;
  padding: 8px 16px; background: var(--surface2); border-bottom: 1px solid var(--border-soft);
  font-size: var(--fs-sm); color: var(--text2);
}
.admin .voc-strip > :first-child { flex: 1; }
.admin .voc-add input { height: 28px; }
.admin .voc-row { content-visibility: auto; contain-intrinsic-size: auto 42px; }
.admin .voc-c { width: 100%; text-align: left; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) 5rem; cursor: pointer; }
.admin .voc-un { grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) 4rem auto; }
.admin .voc-cf { grid-template-columns: minmax(8rem, 1fr) minmax(0, 2fr); }
.admin .voc-slug { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
.admin .voc-zh { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text2); }
.admin .voc-tags, .admin .voc-cfs { display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }
.admin .voc-cfs { justify-content: flex-start; }
.admin .voc-none { justify-content: center; }
</style>
