<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import EmptyState from '../admin/EmptyState.vue'
import { usePoll } from '../admin/poll'
import { useToast } from '../admin/toast'
import DefinitionDetail from './DefinitionDetail.vue'
import DefinitionUpload from './DefinitionUpload.vue'
import { summary } from './version'

const route = useRoute()
const router = useRouter()
const poll = usePoll()
const toast = useToast()

const defs = ref([])
const loaded = ref(false)

const all = computed(() => route.query.all === '1')
const shown = computed(() => defs.value.filter(d => all.value || !d.retracted_at))
const uploading = computed(() => route.query.def === 'new')
const sel = computed(() => Number(route.query.def) || shown.value[0]?.id || null)
const current = computed(() => defs.value.find(d => d.id === sel.value) || null)
const open = def => router.replace({ query: { ...route.query, def } })
const setAll = on => router.replace({ query: { ...route.query, all: on ? '1' : undefined } })

let seq = 0
async function load() {
  const mine = ++seq
  try {
    const r = await api('/api/model-definitions')
    if (mine !== seq) return
    defs.value = r.definitions
  } catch (e) { toast.bad(e) }
  loaded.value = true
}

const builds = computed(() => (poll.overview?.jobs || []).filter(j => j.kind === 'build'))
watch(() => builds.value.map(j => `${j.id}:${j.state}`).join(), load)
watch(() => poll.finished, load)

async function changed(id) {
  await poll.refresh()
  await load()
  if (id !== undefined) open(id || undefined)
}

onMounted(load)
</script>

<template>
  <div class="md dfl">
    <div class="card md-list">
      <div class="card-h">
        <b>模型定義</b>
        <label class="dfl-all"><input type="checkbox" :checked="all" @change="setAll($event.target.checked)" />已撤回</label>
        <span class="count">{{ shown.length }} 個</span>
        <button class="btn sm" @click="open('new')">上傳</button>
      </div>
      <div class="md-scroll">
        <button v-for="d in shown" :key="d.id" type="button"
                :class="['row-g', 'dfl-row', { on: !uploading && d.id === sel, 'dfl-off': d.retracted_at }]"
                @click="open(d.id)">
          <span class="dfl-text">
            <span class="dfl-name">{{ d.name }}<small v-if="d.retracted_at">已撤回</small></span>
            <span class="dfl-src">{{ d.source.repo || d.source.path }}</span>
          </span>
          <span :class="['badge', summary(d).tone]">{{ summary(d).text }}</span>
        </button>
        <EmptyState v-if="!shown.length" :title="loaded ? '沒有資料' : '載入中'" />
      </div>
    </div>

    <DefinitionUpload v-if="uploading" :on-changed="changed" />
    <DefinitionDetail v-else-if="current" :key="current.id" :def="current"
                      :jobs="builds" :on-changed="changed" />
    <div v-else class="card md-detail dfl-none">
      <EmptyState :title="loaded ? '未選取' : '載入中'" />
    </div>
  </div>
</template>

<style>
.admin .dfl.md {
  grid-template-columns: minmax(280px, 1fr) minmax(0, 3fr);
  height: calc(100vh - var(--bar-h) - 3 * var(--s-5) - 41px);
}
.admin .dfl-all { margin-left: auto; display: flex; align-items: center; gap: 6px; font-size: var(--fs-sm); color: var(--text2); cursor: pointer; }
.admin .dfl .md-list .count { margin-left: 0; }
.admin .dfl-row { width: 100%; text-align: left; cursor: pointer; grid-template-columns: minmax(0, 1fr) 5rem; gap: var(--s-3); }
.admin .dfl-row .badge { justify-self: end; }
.admin .dfl-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.admin .dfl-name { color: var(--text); font-size: var(--fs-md); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.admin .dfl-name small { margin-left: 8px; font-size: var(--fs-xs); color: var(--text3); }
.admin .dfl-src { font-family: var(--mono); font-size: var(--fs-xs); color: var(--text3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.admin .dfl-off .dfl-name, .admin .dfl-off .dfl-src, .admin .dfl-off .badge { opacity: .5; }
.admin .dfl-none { justify-content: center; }
</style>
