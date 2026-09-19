<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import EmptyState from '../admin/EmptyState.vue'
import { n } from '../admin/format'
import { usePoll } from '../admin/poll'
import { useToast } from '../admin/toast'
import ProfileDetail from './ProfileDetail.vue'

const route = useRoute()
const router = useRouter()
const poll = usePoll()
const toast = useToast()

const profiles = ref([])
const works = ref(0)
const models = ref([])
const loaded = ref(false)
const adding = ref(null)

const sel = computed(() => Number(route.query.id) || profiles.value.find(p => p.active)?.id || null)
const current = computed(() => profiles.value.find(p => p.id === sel.value) || null)
const maintained = computed(() => profiles.value.filter(p => p.maintained).length)
const open = id => router.replace({ query: { ...route.query, id } })

let seq = 0
async function load() {
  const mine = ++seq
  try {
    const [p, m] = await Promise.all([api('/api/model-profiles'), api('/api/models')])
    if (mine !== seq) return
    profiles.value = p.profiles
    works.value = p.works
    models.value = m.models
  } catch (e) { toast.bad(e) }
  loaded.value = true
}

const jobs = computed(() => (poll.overview?.jobs || []).filter(j => j.kind === 'derive'))
const jobOf = id => jobs.value.find(j => j.model_profile_id === id)
watch(() => jobs.value.map(j => `${j.id}:${j.state}`).join(), load)

function progress(p) {
  const j = jobOf(p.id)
  if (j?.state === 'running') {
    return { tone: 'blue', text: j.total ? `計算中 ${n(j.done)} / ${n(j.total)}` : '計算中' }
  }
  if (j) return { tone: 'blue', text: '排隊中' }
  if (p.maintained && p.works < works.value) return { tone: 'orange', text: '需要重新計算' }
  return null
}

function startAdd(from = '') {
  adding.value = { name: from ? `${from} 複本` : '', from }
}

async function create() {
  const name = adding.value.name.trim()
  if (!name) return toast.bad('名稱不能空白')
  try {
    const r = await api('/api/model-profiles', {
      method: 'POST', body: { name, copy_from: adding.value.from || null },
    })
    adding.value = null
    await load()
    open(r.id)
    toast.ok(`已新增「${name}」`)
  } catch (e) { toast.bad(e) }
}

async function changed(id) {
  await load()
  await poll.refresh()
  if (id !== undefined) open(id || undefined)
}

onMounted(load)
</script>

<template>
  <div class="md mdl">
    <div class="card md-list">
      <div class="card-h">
        <b>設定檔</b>
        <span class="count" title="修改作品或詞彙時，每個保持更新的設定檔各重新計算一次">保持更新 {{ maintained }} 個</span>
        <button class="btn sm" @click="startAdd()">新增設定檔</button>
      </div>

      <div v-if="adding" class="mdl-add">
        <input v-model="adding.name" placeholder="名稱" @keydown.enter="create" @keydown.esc="adding = null" />
        <select v-model="adding.from" title="複製自">
          <option value="">不複製</option>
          <option v-for="p in profiles" :key="p.id" :value="p.name">複製自 {{ p.name }}</option>
        </select>
        <span class="mdl-add-b">
          <button class="btn quiet sm" @click="adding = null">取消</button>
          <button class="btn primary sm" @click="create">新增</button>
        </span>
      </div>

      <div class="md-scroll">
        <button v-for="p in profiles" :key="p.id" type="button"
                :class="['row-g', 'mdl-row', { on: p.id === sel }]" @click="open(p.id)">
          <span class="mdl-name">{{ p.name }}</span>
          <span class="mdl-tags">
            <span v-if="p.active" class="badge green">使用中</span>
            <span v-if="p.maintained" class="badge">保持更新</span>
            <span v-if="progress(p)" :class="['badge', progress(p).tone]">{{ progress(p).text }}</span>
          </span>
        </button>
        <EmptyState v-if="!profiles.length" :title="loaded ? '沒有資料' : '載入中'" />
      </div>
    </div>

    <ProfileDetail v-if="current" :key="current.id" :profile="current" :works="works"
                   :models="models" :job="jobOf(current.id)"
                   @changed="changed" @copy="startAdd(current.name)" />
    <div v-else class="card md-detail mdl-none">
      <EmptyState :title="loaded ? '未選取' : '載入中'" />
    </div>
  </div>
</template>

<style>
.admin .mdl.md {
  grid-template-columns: minmax(240px, 1fr) minmax(0, 4fr);
  height: calc(100vh - var(--bar-h) - 3 * var(--s-5) - 41px);
}
.admin .mdl .md-list .count { margin-left: auto; }
.admin .mdl-add {
  display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--s-2);
  padding: 10px 16px; background: var(--surface2); border-bottom: 1px solid var(--border-soft);
}
.admin .mdl-add-b { display: flex; justify-content: flex-end; gap: var(--s-2); }
.admin .mdl-row {
  width: 100%; text-align: left; cursor: pointer;
  grid-template-columns: minmax(0, 1fr); row-gap: 4px;
}
.admin .mdl-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); font-size: var(--fs-md); }
.admin .mdl-tags { display: flex; gap: 4px; flex-wrap: wrap; }
.admin .mdl-tags:empty { display: none; }
.admin .mdl-none { justify-content: center; }
</style>
