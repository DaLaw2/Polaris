<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import ArmedButton from '../admin/ArmedButton.vue'
import EmptyState from '../admin/EmptyState.vue'
import { KIND, STATE, ago, n } from '../admin/format'
import { usePoll } from '../admin/poll'
import { useToast } from '../admin/toast'
import { TONE, live, note, short, stateOf, where } from './job'
import JobProgress from './JobProgress.vue'

const props = defineProps({ profiles: { type: Array, default: () => [] } })

const poll = usePoll()
const toast = useToast()
const route = useRoute()
const router = useRouter()

const jobs = ref([])
const loading = ref(false)
const filter = reactive({
  kind: computed(() => route.query.kind || ''),
  state: computed(() => route.query.state || ''),
})
const setFilter = (key, v) => router.replace({ query: { ...route.query, [key]: v || undefined } })
const failures = reactive({})
const open = ref(null)
const openErrors = ref([])

const KINDS = [['', '全部'], ...Object.entries(KIND)]
const STATES = [['', '全部'], ...Object.entries(STATE).filter(([k]) => k !== 'stopping')]

const shown = computed(() => jobs.value.filter(j =>
  (!filter.kind || j.kind === filter.kind) &&
  (!filter.state || j.state === filter.state)))

async function load() {
  loading.value = true
  try {
    const r = await api('/api/scan/jobs?limit=100')
    note(r.jobs)
    jobs.value = r.jobs
    await countFailures(r.jobs)
    if (open.value) await loadErrors(open.value)
  } catch (e) { toast.bad(e) }
  loading.value = false
}

async function countFailures(list) {
  const due = list.filter(j => j.run_id && (live(j) || failures[j.id] === undefined))
  await Promise.all(due.map(async j => {
    try { failures[j.id] = (await api(`/api/scan/jobs/${j.id}`)).errors } catch { }
  }))
}

async function loadErrors(id) {
  openErrors.value = (await api(`/api/scan/jobs/${id}/errors?limit=200`)).errors
}

async function toggle(j) {
  if (open.value === j.id) { open.value = null; return }
  open.value = j.id
  openErrors.value = []
  try { await loadErrors(j.id) } catch (e) { toast.bad(e) }
}

async function act(fn, text) {
  try {
    await fn()
    if (text) toast.ok(text)
    poll.refresh()
    await load()
  } catch (e) { toast.bad(e) }
}
const stop = j => act(() => api(`/api/scan/jobs/${j.id}`, { method: 'DELETE' }))
const retry = j => act(() => api(`/api/scan/jobs/${j.id}/retry`, { method: 'POST' }), `#${j.id} 已重新排入`)
const remove = j => act(() => api(`/api/scan/jobs/${j.id}/record`, { method: 'DELETE' }), `已刪除工作 #${j.id}`)

const log = ref('')
const logBox = ref(null)
const logOpen = ref(false)
async function loadLog() {
  try {
    log.value = (await api('/api/scan/worker/log?lines=300')).lines.join('\n')
    await nextTick()
    if (logBox.value) logBox.value.scrollTop = logBox.value.scrollHeight
  } catch (e) { toast.bad(e) }
}
function logToggle(e) {
  logOpen.value = e.target.open
  if (logOpen.value) loadLog()
}

watch(() => poll.overview, () => {
  load()
  if (logOpen.value) loadLog()
})
onMounted(load)
</script>

<template>
  <div class="card">
    <div class="card-h jobs-h">
      <div class="seg">
        <button v-for="[k, label] in KINDS" :key="k" :class="{ on: filter.kind === k }"
                @click="setFilter('kind', k)">{{ label }}</button>
      </div>
      <div class="seg">
        <button v-for="[k, label] in STATES" :key="k" :class="{ on: filter.state === k }"
                @click="setFilter('state', k)">{{ label }}</button>
      </div>
      <span class="count">{{ n(shown.length) }} 個</span>
      <button class="btn quiet sm" :disabled="loading" @click="load">重新整理</button>
    </div>

    <div class="row-g row-h job-row">
      <span class="num-c">#</span><span>種類</span><span>範圍</span><span>狀態</span>
      <span>進度</span><span class="num-c">失敗</span><span>時間</span><span></span>
    </div>

    <template v-for="j in shown" :key="j.id">
      <div :class="['row-g', 'job-row', { on: open === j.id }]">
        <span class="num-c job-id">{{ j.id }}</span>
        <span>{{ KIND[j.kind] || j.kind }}<span v-if="j.force" class="badge job-force">強制</span></span>
        <span class="job-where" :title="j.paths?.join('\n') || j.path || j.collection || ''">{{ where(j, props.profiles) }}</span>
        <span class="job-state">
          <span :class="['badge', TONE[j.state]]">{{ STATE[stateOf(j)] }}</span>
          <span v-if="j.worker_stale" class="badge orange" title="掃描程序沒有回報">無回應</span>
        </span>
        <JobProgress :job="j" />
        <span :class="['num-c', { 'job-bad': failures[j.id] }]">{{ j.run_id ? n(failures[j.id]) : '—' }}</span>
        <span class="job-time" :title="j.requested_at">{{ ago(j.requested_at) }}</span>
        <span class="acts">
          <RouterLink v-if="j.kind === 'check'" class="btn quiet sm"
                      :to="{ path: '/search', query: { view: 'check', job: j.id } }">在搜尋頁開啟</RouterLink>
          <button v-if="j.run_id || j.error" class="btn quiet sm" :aria-expanded="open === j.id"
                  @click="toggle(j)">失敗清單</button>
          <button v-if="j.state === 'failed' || j.state === 'cancelled'" class="btn sm"
                  @click="retry(j)">重試</button>
          <ArmedButton v-if="live(j) && !j.cancel_requested" class="sm warn" label="停止"
                       confirm="確定停止？" :run="() => stop(j)" />
          <ArmedButton v-if="!live(j)" class="sm quiet" label="刪除" :run="() => remove(j)" />
        </span>
      </div>
      <div v-if="open === j.id" class="job-fails">
        <p v-if="j.error" class="job-err">{{ j.error }}</p>
        <p v-for="(e, i) in openErrors" :key="i">
          <span class="badge code">{{ e.stage }}</span>
          <span class="lit-c" :title="e.path">{{ short(e.path) }}</span>
          <span>{{ e.message }}</span>
        </p>
        <p v-if="!openErrors.length && !j.error" class="job-none">沒有資料</p>
      </div>
    </template>

    <EmptyState v-if="!shown.length" :title="loading ? '載入中' : '沒有資料'" />
  </div>

  <details class="card job-log" @toggle="logToggle">
    <summary class="card-h">
      <b>掃描程序記錄</b>
      <button v-if="logOpen" class="btn quiet sm job-log-r" @click.prevent="loadLog">重新整理</button>
    </summary>
    <pre ref="logBox">{{ log || '—' }}</pre>
  </details>
</template>

<style>
.admin .jobs-h { flex-wrap: wrap; }
.admin .job-row { grid-template-columns: 3rem 7rem minmax(8rem, 1.2fr) 9rem minmax(12rem, 1.6fr) 3.5rem 6rem 16rem; }
.admin .job-id { color: var(--text3); }
.admin .job-force { margin-left: 6px; }
.admin .job-where { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.admin .job-state { display: flex; gap: 4px; flex-wrap: wrap; }
.admin .job-bad { color: var(--red); }
.admin .job-time { font-size: var(--fs-xs); color: var(--text3); }
.admin .job-fails {
  padding: 10px 16px 12px 64px; background: var(--surface2);
  border-bottom: 1px solid var(--border-soft);
  display: flex; flex-direction: column; gap: 6px; max-height: 20rem; overflow-y: auto;
  font-size: var(--fs-sm);
}
.admin .job-fails p { display: grid; grid-template-columns: 7rem minmax(0, 18rem) minmax(0, 1fr); gap: var(--s-3); align-items: center; }
.admin .job-fails .lit-c { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.admin .job-fails p.job-err, .admin .job-fails p.job-none { display: block; }
.admin .job-err { color: var(--red); white-space: pre-wrap; }
.admin .job-none { color: var(--text3); }
.admin .job-log summary { cursor: pointer; list-style: none; border-bottom: 0; }
.admin .job-log summary::-webkit-details-marker { display: none; }
.admin .job-log summary::before { content: '›'; color: var(--text3); transition: transform var(--t-base); }
.admin .job-log[open] summary::before { transform: rotate(90deg); }
.admin .job-log[open] summary { border-bottom: 1px solid var(--border-soft); }
.admin .job-log-r { margin-left: auto; }
.admin .job-log pre {
  max-height: 28rem; overflow: auto; margin: 0; padding: 12px 16px;
  font-family: var(--mono); font-size: var(--fs-xs); line-height: 1.6; color: var(--text2);
  white-space: pre-wrap; word-break: break-all;
}
</style>
