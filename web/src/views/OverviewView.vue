<script setup>
import { computed, inject, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import EmptyState from '../admin/EmptyState.vue'
import { KIND, STATE, n } from '../admin/format'
import { usePoll } from '../admin/poll'
import { useToast } from '../admin/toast'
import { TONE, note, stateOf, where } from '../scan/job'
import JobProgress from '../scan/JobProgress.vue'

const poll = usePoll()
const toast = useToast()
const { stats, collections } = inject('library')

const TODO = [
  { key: 'failed_jobs', label: '失敗的工作', to: () => ({ path: '/scan', query: { t: 'jobs', state: 'failed' } }) },
  { key: 'conflicts', label: '衝突', to: () => ({ path: '/vocab', query: { view: 'conflicts' } }) },
  {
    key: 'unplaced', label: '未收錄',
    to: o => ({ path: '/vocab', query: { kind: o.unplaced_characters ? 'character' : 'series', view: 'unplaced' } }),
  },
  { key: 'characters_without_series', label: '缺系列的角色', to: () => ({ path: '/vocab', query: { kind: 'character', view: 'orphans' } }) },
  { key: 'untyped_works', label: '類型未判定', to: () => ({ path: '/works', query: { filter: 'untyped' } }) },
  { key: 'empty_collections', label: '沒有作品的收藏庫', to: () => ({ path: '/scan' }) },
]

const o = computed(() => poll.overview)
const due = computed(() => (o.value ? TODO.filter(t => o.value[t.key] > 0) : []))
const jobs = computed(() => o.value?.jobs || [])
watch(jobs, note, { immediate: true })

const profiles = ref([])
const active = computed(() => profiles.value.find(p => p.active))
onMounted(async () => {
  try { profiles.value = (await api('/api/model-profiles')).profiles } catch (e) { toast.bad(e) }
})
</script>

<template>
  <dl class="stats ov-stats">
    <div class="stat">
      <dt>作品</dt>
      <dd>{{ stats ? n(stats.total_works) : '—' }}</dd>
    </div>
    <div class="stat">
      <dt>收藏庫</dt>
      <dd>{{ n(collections.length) }}</dd>
      <div class="sub">可搜尋 {{ n(collections.filter(c => c.searchable).length) }}</div>
    </div>
    <div class="stat">
      <dt>使用中的設定檔</dt>
      <dd class="ov-name">{{ active?.name || '—' }}</dd>
      <div v-if="active" class="sub">{{ n(active.members.length) }} 個模型</div>
    </div>
  </dl>

  <div class="card">
    <div class="card-h"><b>待處理</b></div>
    <template v-if="o">
      <div v-for="t in due" :key="t.key" class="row-g ov-row">
        <span>{{ t.label }}</span>
        <span class="num-c ov-n">{{ n(o[t.key]) }}</span>
        <RouterLink class="btn soft sm" :to="t.to(o)">查看</RouterLink>
      </div>
      <EmptyState v-if="!due.length" title="沒有待處理項目" />
    </template>
    <EmptyState v-else title="載入中" />
  </div>

  <div class="card">
    <div class="card-h">
      <b>執行中</b>
      <RouterLink class="btn quiet sm ov-all" :to="{ path: '/scan', query: { t: 'jobs' } }">所有工作</RouterLink>
    </div>
    <div v-for="j in jobs" :key="j.id" class="row-g ov-job">
      <span class="num-c ov-id">{{ j.id }}</span>
      <span>{{ KIND[j.kind] || j.kind }}</span>
      <span class="ov-where" :title="j.paths?.join('\n') || j.path || j.collection || ''">{{ where(j, profiles) }}</span>
      <span class="ov-state">
        <span :class="['badge', TONE[j.state]]">{{ STATE[stateOf(j)] }}</span>
        <span v-if="j.worker_stale" class="badge orange" title="掃描程序沒有回報">無回應</span>
      </span>
      <JobProgress :job="j" />
    </div>
    <p v-if="o && !jobs.length" class="ov-none">沒有執行中的工作</p>
  </div>
</template>

<style>
.admin .ov-stats { margin-bottom: var(--s-5); grid-template-columns: repeat(3, minmax(0, 1fr)); }
.admin .ov-name { font-size: var(--fs-xl); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.admin .ov-row { grid-template-columns: minmax(0, 1fr) 6rem auto; }
.admin .ov-n { font-weight: 700; color: var(--orange); }
.admin .ov-all { margin-left: auto; }
.admin .ov-job { grid-template-columns: 3rem 7rem minmax(8rem, 1fr) 9rem minmax(12rem, 1.6fr); }
.admin .ov-id { color: var(--text3); }
.admin .ov-where { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.admin .ov-state { display: flex; gap: 4px; flex-wrap: wrap; }
.admin .ov-none { padding: 12px 16px; font-size: var(--fs-sm); color: var(--text3); }
</style>
