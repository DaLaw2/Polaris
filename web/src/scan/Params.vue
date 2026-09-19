<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import EmptyState from '../admin/EmptyState.vue'
import { EFFECT, GROUPS, LABELS } from '../admin/params'
import { usePoll } from '../admin/poll'
import { useToast } from '../admin/toast'

const props = defineProps({
  profile: { type: Number, default: null },
  skip: { type: Array, default: () => [] },
})
const emit = defineEmits(['saved'])
const toast = useToast()
const poll = usePoll()
const params = ref(null)

const ORDER = ['sample', 'video', 'color', 'worker', 'check', 'type', 'cg', 'score', 'freq', 'freqz']
const scope = () => (props.profile ? `?profile=${props.profile}` : '')

async function load() {
  try {
    const r = await api(`/api/params${scope()}`)
    params.value = r.params
      .filter(p => p.profile === !!props.profile && !props.skip.includes(p.name))
      .map(p => ({ ...p, input: String(p.value) }))
  } catch (e) { toast.bad(e) }
}

const groups = computed(() => {
  const g = {}
  for (const p of params.value || []) (g[p.group] ||= []).push(p)
  return Object.entries(g).sort(([a], [b]) =>
    ((ORDER.indexOf(a) + 1) || 99) - ((ORDER.indexOf(b) + 1) || 99))
})

const parsed = p => (p.input.trim() === '' ? NaN : Number(p.input))
const bad = p => !Number.isFinite(parsed(p))
const dirty = p => bad(p) || parsed(p) !== p.value
const key = p => p.name.split(':').slice(1).join(':')

async function save(rows) {
  const todo = rows.filter(dirty)
  if (todo.some(bad)) return toast.bad('數值不正確')
  let done = 0
  try {
    for (const p of todo) {
      const r = await api(`/api/params/${p.name}${scope()}`, { method: 'PUT', body: { value: parsed(p) } })
      Object.assign(p, r, { input: String(r.value) })
      done++
    }
    toast.ok(`已儲存 ${done} 項`)
  } catch (e) { toast.bad(e) }
  if (done) {
    poll.refresh()
    emit('saved')
  }
}

const revert = rows => rows.forEach(p => { p.input = String(p.value) })

onMounted(load)
</script>

<template>
  <EmptyState v-if="!params" title="載入中" class="card" />
  <div v-for="[group, rows] in groups" :key="group" class="card prm">
    <div class="card-h">
      <b>{{ GROUPS[group] || group }}</b>
      <span class="count">{{ rows.length }} 項</span>
    </div>
    <div class="prm-b">
      <label v-for="p in rows" :key="p.name" class="form-row prm-row">
        <span>
          {{ LABELS[p.name] || key(p) }}
          <small class="lit-c">{{ p.name }}</small>
        </span>
        <span class="prm-c">
          <span :class="['badge', EFFECT[p.effect]?.tone]">{{ EFFECT[p.effect]?.text || p.effect }}</span>
          <input v-model="p.input" :class="['num', { dirty: dirty(p) && !bad(p), bad: bad(p) }]"
                 inputmode="decimal" :title="p.default === null ? '' : `預設 ${p.default}`"
                 @keydown.enter.prevent="save(rows)" />
          <button type="button" class="btn quiet sm prm-reset"
                  :disabled="p.default === null || parsed(p) === p.default"
                  :title="p.default === null ? '' : `預設 ${p.default}`"
                  @click="p.input = String(p.default)">恢復預設</button>
        </span>
      </label>
    </div>
    <div class="card-f prm-f" v-if="rows.some(dirty)">
      <button class="btn quiet sm" @click="revert(rows)">取消</button>
      <button class="btn primary sm" :disabled="rows.some(bad)" @click="save(rows)">
        儲存 {{ rows.filter(dirty).length }} 項
      </button>
    </div>
  </div>
</template>

<style>
.admin .prm-b { display: grid; grid-template-columns: repeat(auto-fill, minmax(520px, 1fr)); }
.admin .prm-row { font-size: var(--fs-sm); }
.admin .prm-row small { margin-top: 2px; font-size: var(--fs-xs); }
.admin .prm-c { display: flex; align-items: center; gap: var(--s-2); }
.admin .prm-c .badge { min-width: 6.5rem; justify-content: center; }
.admin .prm-reset:disabled { visibility: hidden; }
.admin .prm-f { display: flex; justify-content: flex-end; gap: var(--s-2); }
</style>
