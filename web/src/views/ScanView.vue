<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { gb, n, pct } from '../admin/format'
import { scannerState, usePoll } from '../admin/poll'
import { useToast } from '../admin/toast'
import Subtabs from '../admin/Subtabs.vue'
import Collections from '../scan/Collections.vue'
import Jobs from '../scan/Jobs.vue'
import Params from '../scan/Params.vue'

const poll = usePoll()
const toast = useToast()

const TABS = [
  { key: 'libs', label: '收藏庫' },
  { key: 'jobs', label: '工作' },
  { key: 'params', label: '參數' },
]

const profiles = ref([])
onMounted(async () => {
  try { profiles.value = (await api('/api/model-profiles')).profiles } catch (e) { toast.bad(e) }
})

const scanner = computed(() => scannerState(poll))
const workers = computed(() => poll.worker?.workers || [])
const live = computed(() => workers.value.filter(w => !w.stale))
const stale = computed(() => workers.value.filter(w => w.stale))
const scanJobs = computed(() => {
  const k = poll.worker?.by_kind || {}
  return ['scan', 'check', 'build'].reduce((s, x) => s + (k[x]?.queued || 0), 0)
})
const busyWith = computed(() => live.value.find(w => w.job_id)?.job_id)

const derive = computed(() => poll.worker?.by_kind?.derive || { queued: 0, running: 0 })
const deriving = computed(() =>
  poll.overview?.jobs.find(j => j.kind === 'derive' && j.state === 'running'))

const gpu = computed(() => poll.gpu)
const gpuPct = computed(() => (gpu.value?.present ? pct(gpu.value.used_mb, gpu.value.total_mb) : 0))

const busy = ref(false)
async function worker(method, query = '') {
  busy.value = true
  try {
    await api(`/api/scan/worker${query}`, { method })
    await poll.refresh()
  } catch (e) { toast.bad(e) }
  busy.value = false
}
const start = () => worker('POST')
const stop = () => worker('DELETE')
const forget = () => worker('DELETE', '?forget=true')
</script>

<template>
  <dl class="stats scan-cards">
    <div :class="['stat', { lit: scanner.tone === 'warn' || scanner.tone === 'bad' }]">
      <dt>掃描程序</dt>
      <dd class="scan-state"><i :class="['dot', scanner.tone]"></i>{{ scanner.text }}</dd>
      <div class="sub">
        <template v-if="busyWith">工作 #{{ busyWith }} · </template>排隊中 {{ n(scanJobs) }}
      </div>
      <div class="scan-acts">
        <button v-if="!live.length" class="btn sm" :disabled="busy || !poll.reachable" @click="start">啟動</button>
        <button v-else class="btn sm warn" :disabled="busy || live.every(w => w.stop_requested)"
                title="完成目前的作品後停止" @click="stop">停止</button>
        <button v-if="stale.length" class="btn quiet sm" :disabled="busy" @click="forget">
          清除無回應 {{ stale.length }}
        </button>
      </div>
    </div>

    <div class="stat">
      <dt>重新計算</dt>
      <dd>{{ n(derive.running) }}<small>執行中</small></dd>
      <div class="sub">排隊中 {{ n(derive.queued) }}</div>
      <div v-if="deriving?.total" class="scan-track">
        <span class="track"><i :style="{ width: pct(deriving.done, deriving.total) + '%' }"></i></span>
        <span>{{ n(deriving.done) }} / {{ n(deriving.total) }}</span>
      </div>
    </div>

    <div class="stat">
      <dt>GPU</dt>
      <template v-if="gpu?.present">
        <dd>{{ gb(gpu.used_mb) }}<small>/ {{ gb(gpu.total_mb) }} GB</small></dd>
        <div class="sub">{{ gpu.name.replace(/^NVIDIA (GeForce )?/, '') }}</div>
        <span :class="['track', { warn: gpuPct > 85 }]"><i :style="{ width: gpuPct + '%' }"></i></span>
      </template>
      <template v-else>
        <dd>—</dd>
        <div class="sub">{{ gpu ? '未偵測到' : '載入中' }}</div>
      </template>
    </div>
  </dl>

  <Subtabs :tabs="TABS" v-slot="{ tab }">
    <Collections v-if="tab === 'libs'" :profiles="profiles" @start="start" />
    <Jobs v-else-if="tab === 'jobs'" :profiles="profiles" />
    <Params v-else />
  </Subtabs>
</template>

<style>
.admin .scan-cards { margin-bottom: var(--s-5); grid-template-columns: repeat(3, minmax(0, 1fr)); }
.admin .scan-state { display: flex; align-items: center; gap: 10px; font-size: var(--fs-xl); }
.admin .scan-acts { display: flex; gap: var(--s-2); margin-top: var(--s-3); }
.admin .scan-track { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: var(--s-3); margin-top: var(--s-3); font-size: var(--fs-xs); color: var(--text2); font-variant-numeric: tabular-nums; }
.admin .scan-track .track { margin: 0; }
</style>
