<script setup>
import { computed, provide, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'
import './admin.css'
import { KIND, STATE, gb, n } from './format'
import { providePoll, scannerState } from './poll'
import Toasts from './Toasts.vue'
import { useToast } from './toast'

const route = useRoute()
const toast = useToast()

const NAV = [
  { name: 'overview', to: '/', label: '總覽', due: o => o.failed_jobs },
  { name: 'scan', to: '/scan', label: '掃描' },
  { name: 'vocab', to: '/vocab', label: '詞彙', due: o => o.conflicts + o.unplaced },
  { name: 'works', to: '/works', label: '作品', due: o => o.untyped_works },
  { name: 'models', to: '/models', label: '模型' },
]

const stats = ref(null)
const collections = ref([])
async function loadCollections() { collections.value = await api('/api/collections') }
async function loadStats() {
  try { stats.value = await api('/api/stats') } catch { stats.value = null }
}
provide('library', { collections, loadCollections, stats, loadStats })
loadStats()
loadCollections().catch(() => {})

async function finished(id) {
  loadStats()
  loadCollections().catch(() => {})
  try {
    const j = await api(`/api/scan/jobs/${id}`)
    const text = `${KIND[j.kind] || j.kind} #${id} ${STATE[j.state] || j.state}`
    if (j.state === 'failed') toast.bad(j.error ? `${text}：${j.error}` : text)
    else if (j.state === 'done') toast.ok(text)
    else toast.show(text)
  } catch (e) { toast.bad(e) }
}

const poll = providePoll({ gpu: () => route.name === 'scan', onFinish: finished })
watch(() => route.name, name => { if (name === 'scan') poll.refresh() })

const due = t => (t.due && poll.overview ? t.due(poll.overview) : 0)
const title = computed(() => NAV.find(t => t.name === route.name)?.label || '')

const scanner = computed(() => scannerState(poll))
const queued = computed(() =>
  Object.values(poll.worker?.by_kind || {}).reduce((s, k) => s + k.queued, 0))
const card = computed(() => (route.name === 'scan' && poll.gpu?.present ? poll.gpu : null))
</script>

<template>
  <div class="admin">
    <aside class="side">
      <div class="mark"><b>Polaris</b></div>
      <nav>
        <RouterLink v-for="t in NAV" :key="t.name" :to="t.to"
                    :class="['nav-i', { on: route.name === t.name }]">
          {{ t.label }}
          <span v-if="due(t)" class="badge orange">{{ n(due(t)) }}</span>
        </RouterLink>
        <RouterLink to="/search" class="nav-i find">搜尋</RouterLink>
      </nav>
    </aside>

    <main>
      <header class="topbar">
        <h2>{{ title }}</h2>
        <div class="pills">
          <span class="pill"><i :class="['dot', scanner.tone]"></i>掃描程序 <b>{{ scanner.text }}</b></span>
          <span class="pill">排隊中 <b>{{ queued }}</b></span>
          <span class="pill" v-if="card">
            {{ card.name.replace(/^NVIDIA (GeForce )?/, '') }}
            <b>{{ gb(card.used_mb) }} / {{ gb(card.total_mb) }} GB</b>
          </span>
        </div>
      </header>
      <div class="work"><RouterView /></div>
    </main>
    <Toasts />
  </div>
</template>
