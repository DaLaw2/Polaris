<script setup>
import { computed, onMounted, ref } from 'vue'
import { explain } from '../admin/errors'
import { n } from '../admin/format'
import { useToast } from '../admin/toast'
import { ENUM, FIELD, concepts, forget, label } from './concepts'

const props = defineProps({
  field: { type: Object, required: true },
  ops: { type: Object, required: true },
})
const toast = useToast()
const SHOWN = 30

const f = computed(() => props.field)
const key = computed(() => f.value.field)
const name = computed(() => FIELD[key.value] || key.value)
const isEnum = computed(() => ENUM.has(key.value))

const options = ref([])
async function loadOptions() {
  try { options.value = await concepts(key.value) } catch (e) { toast.bad(e) }
}
onMounted(loadOptions)

const busy = ref(false)
async function run(fn) {
  busy.value = true
  try { await fn() } catch (e) { toast.bad(e) } finally { busy.value = false }
}

const rows = computed(() => {
  const m = new Map()
  const at = v => {
    const k = v.concept_id ?? `s:${v.slug}`
    if (!m.has(k)) m.set(k, { key: k, v })
    return m.get(k)
  }
  for (const layer of ['model', 'added', 'negated', 'result']) {
    for (const v of f.value[layer]) at(v)[layer] = v
  }
  return [...m.values()].map(r => ({
    ...r, struck: !!r.negated || (!!r.model?.excluded && !r.added),
  }))
})
const all = ref(false)
const shown = computed(() => (all.value ? rows.value : rows.value.slice(0, SHOWN)))

const exclude = v => run(() => props.ops.post(v.concept_id
  ? { field: key.value, concept_id: v.concept_id, negated: true }
  : { field: key.value, value: v.slug, negated: true, create: true }))
const restore = claim => run(() => props.ops.drop(claim.claim_id))
const remove = claim => run(async () => {
  await props.ops.drop(claim.claim_id)
  toast.undo(`已移除「${label(claim)}」`, () =>
    props.ops.post({ field: key.value, concept_id: claim.concept_id }))
})
const reset = () => run(() => props.ops.clear(key.value))

const draft = ref('')
const asking = ref(null)
const listId = computed(() => `cf-${key.value}`)

function add() {
  const text = draft.value.trim()
  if (!text) return
  const t = text.toLowerCase()
  const hit = options.value.find(c => c.slug.toLowerCase() === t || label(c).toLowerCase() === t)
  return run(async () => {
    try {
      await props.ops.post(hit ? { field: key.value, concept_id: hit.id } : { field: key.value, value: text })
      draft.value = ''
    } catch (e) {
      if (e.code !== 'concept_unknown') throw e
      asking.value = { value: e.detail?.value || text, text: explain(e) }
    }
  })
}
const create = () => run(async () => {
  await props.ops.post({ field: key.value, value: asking.value.value, create: true })
  asking.value = null
  draft.value = ''
  forget(key.value)
  await loadOptions()
})

const manual = computed(() => f.value.added[0] || null)
const kept = computed(() => f.value.model.filter(v => !v.excluded))
const pickEnum = e => {
  const id = Number(e.target.value)
  if (id) run(() => props.ops.post({ field: key.value, concept_id: id }))
}
const touched = computed(() => f.value.added.length + f.value.negated.length > 0)
</script>

<template>
  <section class="cf">
    <div class="cf-h" :title="f.field === 'tag' ? null : f.model_mappable ? '可由模型填入' : '僅限手動'">
      <b>{{ name }}</b>
      <span v-if="f.multi" class="count">{{ n(rows.length) }}</span>
      <button v-if="!f.multi && touched" class="btn quiet sm cf-reset" :disabled="busy"
              @click="reset">還原為模型結果</button>
    </div>

    <template v-if="f.multi">
      <div v-for="r in shown" :key="r.key" :class="['row-g', 'cf-row', { struck: r.struck }]">
        <span class="cf-v" :title="r.v.slug">{{ label(r.v) }}</span>
        <span class="cf-src">
          <span v-if="r.model" class="badge">模型</span>
          <span v-if="r.added" class="badge blue">手動</span>
          <span v-if="r.struck" class="badge orange">已排除</span>
        </span>
        <span class="cf-act">
          <button v-if="r.negated" class="btn soft sm" :disabled="busy" @click="restore(r.negated)">復原</button>
          <button v-else-if="r.added" class="btn quiet sm" :disabled="busy" @click="remove(r.added)">移除</button>
          <button v-else-if="!r.struck" class="btn quiet sm" :disabled="busy"
                  @click="exclude(r.model || r.result)">排除</button>
        </span>
      </div>
      <button v-if="rows.length > SHOWN && !all" class="btn quiet sm cf-all" @click="all = true">
        顯示全部 {{ n(rows.length) }}</button>
      <div class="cf-new">
        <input v-model="draft" :list="listId" type="search" :disabled="busy"
               @keydown.enter="add" @input="asking = null" />
        <button class="btn sm" :disabled="busy || !draft.trim()" @click="add">新增</button>
      </div>
    </template>

    <template v-else>
      <div class="cf-line">
        <span class="cf-k">模型</span>
        <span class="cf-vals">
          <span v-for="v in kept" :key="v.concept_id" class="cf-one">
            <span :title="v.slug">{{ label(v) }}</span>
            <button class="btn quiet sm" :disabled="busy" @click="exclude(v)">排除</button>
          </span>
          <span v-for="v in f.negated" :key="v.concept_id" class="cf-one struck">
            <span :title="v.slug">{{ label(v) }}</span>
            <span class="badge orange">已排除</span>
            <button class="btn soft sm" :disabled="busy" @click="restore(v)">復原</button>
          </span>
          <span v-if="!kept.length && !f.negated.length" class="cf-none">—</span>
        </span>
        <span class="cf-k">手動</span>
        <span class="cf-vals">
          <span v-if="manual" class="cf-one" :title="manual.slug">{{ label(manual) }}</span>
          <span v-else class="cf-none">—</span>
        </span>
        <span class="cf-k">→</span>
        <span class="cf-vals cf-res">
          <b v-for="v in f.result" :key="v.concept_id" :title="v.slug">{{ label(v) }}</b>
          <span v-if="!f.result.length" class="cf-none">—</span>
        </span>
      </div>
      <div class="cf-new">
        <select v-if="isEnum" :value="manual?.concept_id ?? ''" :disabled="busy" @change="pickEnum">
          <option value="" disabled>手動</option>
          <option v-for="c in options" :key="c.id" :value="c.id">{{ label(c) }}</option>
        </select>
        <template v-else>
          <input v-model="draft" :list="listId" type="search" :disabled="busy"
                 @keydown.enter="add" @input="asking = null" />
          <button class="btn sm" :disabled="busy || !draft.trim()" @click="add">儲存</button>
        </template>
      </div>
    </template>

    <div v-if="asking" class="cf-ask">
      <span>{{ asking.text }}</span>
      <button class="btn quiet sm" :disabled="busy" @click="asking = null">取消</button>
      <button class="btn primary sm" :disabled="busy" @click="create">新增「{{ asking.value }}」</button>
    </div>

    <datalist v-if="!isEnum" :id="listId">
      <option v-for="c in options" :key="c.id" :value="c.slug">{{ c.display || '' }}</option>
    </datalist>
  </section>
</template>

<style>
.admin .cf { display: flex; flex-direction: column; border: 1px solid var(--border-soft); border-radius: var(--r-md); }
.admin .cf-h { display: flex; align-items: center; gap: var(--s-2); min-height: 40px; padding: 6px 14px; border-bottom: 1px solid var(--border-soft); }
.admin .cf-h b { font-size: var(--fs-md); }
.admin .cf-h .count { font-size: var(--fs-xs); color: var(--text3); font-variant-numeric: tabular-nums; }
.admin .cf-reset { margin-left: auto; }
.admin .cf-row { grid-template-columns: minmax(0, 1fr) 4.5rem 4.5rem; min-height: 36px; padding: 3px 14px; }
.admin .cf-v { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
.admin .cf-src { display: flex; gap: 4px; }
.admin .cf-act { display: flex; justify-content: flex-end; }
.admin .struck .cf-v, .admin .cf-one.struck > span:first-child { text-decoration: line-through; color: var(--text3); }
.admin .cf-all { align-self: flex-start; margin: 6px 14px 0; }
.admin .cf-new { display: flex; gap: var(--s-2); padding: 8px 14px; }
.admin .cf-new input, .admin .cf-new select { flex: 1; min-width: 0; height: 28px; }
.admin .cf-line {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px var(--s-2);
  padding: 10px 14px 2px; font-size: var(--fs-sm);
}
.admin .cf-k { color: var(--text3); font-size: var(--fs-xs); }
.admin .cf-vals { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 4px var(--s-3); min-height: 28px; margin-right: var(--s-3); }
.admin .cf-one { display: inline-flex; align-items: center; gap: 6px; color: var(--text); }
.admin .cf-res b { color: var(--accent2); font-weight: 600; }
.admin .cf-none { color: var(--text3); }
.admin .cf-ask {
  display: flex; align-items: center; gap: var(--s-2); flex-wrap: wrap; padding: 8px 14px;
  border-top: 1px solid var(--border-soft); background: var(--surface2); font-size: var(--fs-sm);
}
.admin .cf-ask span { flex: 1; color: var(--orange); }
</style>
