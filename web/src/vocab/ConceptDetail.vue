<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { api } from '../api'
import ArmedButton from '../admin/ArmedButton.vue'
import { explain } from '../admin/errors'
import { n } from '../admin/format'
import { usePoll } from '../admin/poll'
import { useToast } from '../admin/toast'

const props = defineProps({
  concept: { type: Object, required: true },
  lists: { type: Object, required: true },
  onChanged: { type: Function, default: () => {} },
})
const emit = defineEmits(['open', 'close'])
const toast = useToast()
const poll = usePoll()
const changed = id => props.onChanged(id)

const help = computed(() => (props.concept.kind === 'tag'
  ? { named: '比對模型輸出、排除與搜尋', search: '僅比對搜尋' }
  : { named: '比對模型輸出與搜尋', search: '僅比對搜尋' }))

const c = computed(() => props.concept)
const kinds = computed(() => props.lists[c.value.kind])
const series = computed(() => props.lists.series)
const parents = computed(() => 'parent_id' in c.value)
const parent = computed(() => series.value.find(s => s.id === c.value.parent_id))
const children = computed(() =>
  c.value.kind === 'series' && parents.value ? props.lists.character.filter(x => x.parent_id === c.value.id) : [])
const groups = computed(() => [
  { key: 'named', label: '別名', only: false },
  { key: 'search', label: '搜尋用別名', only: true },
].map(g => ({ ...g, terms: c.value.terms.filter(t => t.search_only === g.only) })))

const blank = () => ({
  slug: c.value.slug, zh: c.value.display_zh || '', note: c.value.note || '',
  parent: parent.value?.slug || '',
})
const form = reactive(blank())
const reset = () => Object.assign(form, blank())
let resync = false
watch(c, () => { if (resync) { resync = false; reset() } })
const dirty = computed(() => Object.entries(blank()).some(([k, v]) => form[k].trim() !== v))
const taken = ref(null)
watch(() => form.slug, () => { taken.value = null })

const find = (list, text) => list.find(x => x.slug.toLowerCase() === text.trim().toLowerCase())

async function save() {
  const was = blank()
  const body = {}
  if (form.slug.trim() !== was.slug) body.slug = form.slug.trim()
  if (form.zh.trim() !== was.zh) body.display_zh = form.zh.trim()
  if (form.note.trim() !== was.note) body.note = form.note.trim()
  if (form.parent.trim() !== was.parent) {
    const p = form.parent.trim() ? find(series.value, form.parent) : null
    if (form.parent.trim() && !p) return toast.bad(`找不到系列「${form.parent.trim()}」`)
    body.parent_id = p ? p.id : null
  }
  try {
    await api(`/api/concepts/${c.value.id}`, { method: 'PATCH', body })
    toast.ok('已儲存')
    resync = true
    changed()
  } catch (e) {
    if (e.code === 'concept_exists') taken.value = { id: e.detail.id, slug: e.detail.slug, text: explain(e) }
    else toast.bad(e)
  }
}

const words = reactive({ named: '', search: '' })
async function addTerm(g) {
  const term = words[g.key].trim()
  if (!term) return
  if (c.value.terms.some(t => t.term.toLowerCase() === term.toLowerCase())) {
    return toast.bad(`已有別名「${term}」`)
  }
  try {
    await mapTerm(term, g.only)
    words[g.key] = ''
    changed()
    countWorks()
  } catch (e) { toast.bad(e) }
}
const mapTerm = (term, searchOnly) => api('/api/concepts', {
  method: 'POST',
  body: { kind: c.value.kind, slug: c.value.slug, terms: [term], search_only: searchOnly },
})

async function flip(t) {
  try {
    await api(`/api/terms/${t.id}`, { method: 'PATCH', body: { search_only: !t.search_only } })
    changed()
    countWorks()
  } catch (e) { toast.bad(e) }
}

async function drop(t) {
  const slug = c.value.slug
  const kind = c.value.kind
  try {
    await api(`/api/terms/${t.id}`, { method: 'DELETE' })
    changed()
    countWorks()
    toast.undo(`已刪除別名「${t.term}」`, async () => {
      await api('/api/concepts', {
        method: 'POST', body: { kind, slug, terms: [t.term], search_only: t.search_only },
      })
      changed()
      countWorks()
    })
  } catch (e) { toast.bad(e) }
}

const works = ref(null)
async function countWorks() {
  try { works.value = (await api(`/api/works?concept=${c.value.id}&limit=1`)).total } catch (e) { toast.bad(e) }
}
watch([() => c.value.id, () => poll.finished], countWorks, { immediate: true })

const merging = ref(null)
const mergeInput = ref(null)
const target = computed(() => {
  const t = merging.value !== null && find(kinds.value, merging.value)
  return t && t.id !== c.value.id ? t : null
})
function openMerge() {
  merging.value = ''
  setTimeout(() => mergeInput.value?.focus())
}

async function merge(into) {
  try {
    await api(`/api/concepts/${c.value.id}/merge`, { method: 'POST', body: { into: into.id } })
    toast.ok(`已合併到「${into.slug}」`)
    changed(into.id)
  } catch (e) { toast.bad(e) }
}

async function remove() {
  try {
    await api(`/api/concepts/${c.value.id}`, { method: 'DELETE' })
    toast.ok(`已刪除「${c.value.slug}」`)
    changed(null)
  } catch (e) {
    if (e.code === 'concept_in_use') toast.bad(e, { action: { label: '合併到…', run: openMerge } })
    else toast.bad(e)
  }
}
</script>

<template>
  <div class="card md-detail cd">
    <div class="card-h">
      <b>{{ c.display || c.slug }}</b>
      <span class="lit-c" v-if="c.display">{{ c.slug }}</span>
      <button class="btn quiet sm cd-x" aria-label="關閉" @click="emit('close')">✕</button>
    </div>

    <div class="md-scroll">
      <div class="cd-form">
        <label>名稱<input v-model="form.slug" @keydown.enter="save" /></label>
        <div v-if="taken" class="cd-taken">
          <span>{{ taken.text }}</span>
          <ArmedButton class="sm" :label="`合併到「${taken.slug}」`" confirm="確定合併？"
                       busy-label="合併中" :run="() => merge(taken)" />
        </div>
        <label>中文名稱<input v-model="form.zh" :placeholder="c.display_zh ? '' : c.display || ''"
                         @keydown.enter="save" /></label>
        <label>備註<input v-model="form.note" @keydown.enter="save" /></label>
        <label v-if="c.kind === 'character' && parents">所屬系列
          <input v-model="form.parent" list="cd-series" type="search" @keydown.enter="save" />
        </label>
        <datalist id="cd-series">
          <option v-for="s in series" :key="s.id" :value="s.slug">{{ s.display || '' }}</option>
        </datalist>
        <div class="cd-save" v-if="dirty">
          <button class="btn quiet sm" @click="reset">取消</button>
          <button class="btn primary sm" @click="save">儲存</button>
        </div>
      </div>

      <section v-for="g in groups" :key="g.key" class="cd-group">
        <div class="cd-gh"><b>{{ g.label }}</b><span class="count">{{ n(g.terms.length) }}</span></div>
        <small>{{ help[g.key] }}</small>
        <div v-for="t in g.terms" :key="t.id" class="row-g cd-term">
          <span class="lit-c">{{ t.term }}</span>
          <span class="acts">
            <button class="btn quiet sm" @click="flip(t)">{{ g.only ? '設為別名' : '設為搜尋用' }}</button>
            <button class="btn quiet sm" aria-label="刪除" title="刪除" @click="drop(t)">✕</button>
          </span>
        </div>
        <div class="cd-new">
          <input v-model="words[g.key]" @keydown.enter="addTerm(g)" />
          <button class="btn sm" :disabled="!words[g.key].trim()" @click="addTerm(g)">新增別名</button>
        </div>
      </section>

      <section v-if="c.kind === 'series' && parents" class="cd-group">
        <div class="cd-gh"><b>角色</b><span class="count">{{ n(children.length) }}</span></div>
        <div class="cd-kids">
          <button v-for="x in children" :key="x.id" class="btn sm"
                  :title="x.slug" @click="emit('open', 'character', x.id)">{{ x.display || x.slug }}</button>
        </div>
      </section>

      <div class="inset cd-works">
        <span>作品</span>
        <RouterLink :to="{ path: '/works', query: { concept: c.id } }">
          {{ works === null ? '載入中' : n(works) }}
        </RouterLink>
      </div>

      <div class="cd-acts">
        <template v-if="merging !== null">
          <input ref="mergeInput" v-model="merging" list="cd-same" type="search" class="cd-merge"
                 @keydown.esc="merging = null" />
          <datalist id="cd-same">
            <option v-for="x in kinds" :key="x.id" :value="x.slug">{{ x.display || '' }}</option>
          </datalist>
          <button class="btn quiet sm" @click="merging = null">取消</button>
          <ArmedButton class="sm" :label="target ? `合併到「${target.slug}」` : '合併到…'"
                       confirm="確定合併？" busy-label="合併中" :disabled="!target"
                       :run="() => merge(target)" />
        </template>
        <button v-else class="btn sm" @click="openMerge">合併到…</button>
        <ArmedButton class="sm cd-del" label="刪除" busy-label="刪除中" :run="remove" />
      </div>
    </div>
  </div>
</template>

<style>
.admin .cd .card-h { gap: var(--s-3); }
.admin .cd .card-h b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.admin .cd-x { margin-left: auto; }
.admin .cd-form { display: grid; gap: var(--s-2); min-width: 0; }
.admin .cd-form label {
  display: grid; grid-template-columns: 5rem minmax(0, 1fr); align-items: center; gap: var(--s-3);
  font-size: var(--fs-sm); color: var(--text2);
}
.admin .cd-taken, .admin .cd-save { display: flex; align-items: center; gap: var(--s-2); justify-content: flex-end; flex-wrap: wrap; font-size: var(--fs-sm); }
.admin .cd-taken { padding-left: calc(5rem + var(--s-3)); }
.admin .cd-taken span { color: var(--orange); flex: 1 1 100%; }
.admin .cd-group { display: flex; flex-direction: column; border: 1px solid var(--border-soft); border-radius: var(--r-md); }
.admin .cd-gh { display: flex; align-items: center; gap: var(--s-2); padding: 10px 14px 0; }
.admin .cd-gh b { font-size: var(--fs-md); }
.admin .cd-gh .count { font-size: var(--fs-xs); color: var(--text3); font-variant-numeric: tabular-nums; }
.admin .cd-group > small { padding: 2px 14px 8px; font-size: var(--fs-xs); color: var(--text3); border-bottom: 1px solid var(--border-soft); }
.admin .cd-term { grid-template-columns: minmax(0, 1fr) auto; min-height: 38px; padding: 4px 14px; }
.admin .cd-term .lit-c { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
.admin .cd-new { display: flex; gap: var(--s-2); padding: 8px 14px; }
.admin .cd-new input { flex: 1; min-width: 0; height: 28px; }
.admin .cd-kids { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 14px; max-height: 16rem; overflow-y: auto; }
.admin .cd-works { display: flex; justify-content: space-between; font-size: var(--fs-sm); color: var(--text2); }
.admin .cd-works a { color: var(--accent2); font-weight: 500; font-variant-numeric: tabular-nums; }
.admin .cd-acts { display: flex; align-items: center; gap: var(--s-2); flex-wrap: wrap; }
.admin .cd-merge { flex: 1; min-width: 8rem; height: 28px; }
.admin .cd-del { margin-left: auto; }
</style>
