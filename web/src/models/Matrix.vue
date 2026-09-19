<script>
export const HEAD = {
  general: [['score:general', '門檻'], ['freq:general', '頻率門檻']],
  character: [['score:character', '門檻'], ['freq:character', '頻率門檻']],
  copyright: [['score:copyright', '門檻'], ['freq:copyright', '頻率門檻']],
  artist: [['score:artist', '門檻'], ['freq:artist', '頻率門檻']],
  rating: [['rating:score', '門檻'], ['rating:min_pages', '最少頁數']],
  embedding: [],
}
export const HEAD_PARAMS = Object.values(HEAD).flat().map(([name]) => name)
</script>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { api } from '../api'
import ArmedButton from '../admin/ArmedButton.vue'
import { ROLE, n } from '../admin/format'
import { PRECISION } from './version'
import Switch from '../admin/Switch.vue'
import { useToast } from '../admin/toast'

const props = defineProps({
  profile: { type: Object, required: true },
  models: { type: Array, required: true },
  onChanged: { type: Function, default: () => {} },
})
const toast = useToast()

const CATS = ['general', 'character', 'copyright', 'artist', 'rating', 'embedding']
const RICH = ['general', 'character', 'copyright', 'artist']
const SOLE = ['rating', 'embedding']

const members = computed(() => props.profile.members)
const role = (m, cat) => m.roles.find(r => r.role === cat)
const cats = name => props.models.find(x => x.name === name)?.categories || {}
const supported = (m, cat) => cat in cats(m.backend) || !!role(m, cat)

function fresh(m) {
  return Object.fromEntries(CATS.map(cat => {
    const r = role(m, cat)
    return [cat, { on: !!r, cut: r?.score_cutoff == null ? '' : String(r.score_cutoff), yields: !!r?.yields }]
  }))
}

const drafts = reactive({})
const cutOf = d => (d.cut.trim() === '' ? null : Number(d.cut))
const bad = d => d.on && d.cut.trim() !== '' && !(cutOf(d) >= 0 && cutOf(d) <= 1)

function cellDirty(m, cat) {
  const d = drafts[m.backend]?.[cat]
  const r = role(m, cat)
  if (!d) return false
  if (d.on !== !!r) return true
  if (!d.on || !RICH.includes(cat)) return false
  return bad(d) || cutOf(d) !== (r.score_cutoff ?? null) || d.yields !== !!r.yields
}
const memberDirty = m => CATS.some(cat => cellDirty(m, cat))

watch(members, list => {
  for (const b of Object.keys(drafts)) if (!list.some(m => m.backend === b)) delete drafts[b]
  for (const m of list) if (!drafts[m.backend] || !memberDirty(m)) drafts[m.backend] = fresh(m)
}, { immediate: true })

const params = ref({})
const heads = reactive({})
const paramBad = name => heads[name]?.trim() === '' || !Number.isFinite(Number(heads[name]))
const paramDirty = name => name in params.value && (paramBad(name) || Number(heads[name]) !== params.value[name])

async function loadParams() {
  try {
    const r = await api(`/api/params?profile=${props.profile.id}`)
    params.value = Object.fromEntries(r.params.filter(p => p.profile).map(p => [p.name, p.value]))
    for (const name of HEAD_PARAMS) if (heads[name] === undefined || !paramDirty(name)) heads[name] = String(params.value[name] ?? '')
  } catch (e) { toast.bad(e) }
}
loadParams()
defineExpose({ loadParams })

function inherited(cat) {
  const own = params.value[`score:${cat}`]
  if (own !== undefined) return `沿用 ${own}（類別預設）`
  const all = params.value['score:default']
  return all === undefined ? '' : `沿用 ${all}（全域預設）`
}

const dirtyCells = computed(() =>
  members.value.reduce((s, m) => s + CATS.filter(cat => cellDirty(m, cat)).length, 0)
  + HEAD_PARAMS.filter(paramDirty).length)
const anyBad = computed(() =>
  HEAD_PARAMS.some(p => paramDirty(p) && paramBad(p))
  || members.value.some(m => CATS.some(cat => bad(drafts[m.backend][cat]))))
const embedders = computed(() => members.value.filter(m => drafts[m.backend]?.embedding.on).length)

function body(m) {
  const d = drafts[m.backend]
  return {
    rank: m.rank,
    roles: CATS.filter(cat => d[cat].on).map(cat => {
      const r = role(m, cat)
      return RICH.includes(cat)
        ? { role: cat, score_cutoff: cutOf(d[cat]), yields: d[cat].yields }
        : { role: cat, score_cutoff: r?.score_cutoff ?? null, yields: !!r?.yields }
    }),
  }
}

const busy = ref(false)
async function save() {
  if (anyBad.value) return toast.bad('數值不正確')
  busy.value = true
  let done = 0
  for (const name of HEAD_PARAMS.filter(paramDirty)) {
    try {
      const r = await api(`/api/params/${name}?profile=${props.profile.id}`, {
        method: 'PUT', body: { value: Number(heads[name]) },
      })
      params.value = { ...params.value, [name]: r.value }
      heads[name] = String(r.value)
      done++
    } catch (e) { toast.bad(e) }
  }
  const gains = m => SOLE.filter(cat => drafts[m.backend][cat].on && !role(m, cat)).length
  for (const m of members.value.filter(memberDirty).sort((a, b) => gains(a) - gains(b))) {
    try {
      await api(`/api/model-profiles/${props.profile.id}/members/${encodeURIComponent(m.backend)}`, {
        method: 'PUT', body: body(m),
      })
      done++
    } catch (e) { toast.bad(e) }
  }
  busy.value = false
  if (done) toast.ok(`已儲存 ${done} 項`)
  props.onChanged()
}

function revert() {
  for (const m of members.value) drafts[m.backend] = fresh(m)
  for (const name of HEAD_PARAMS) heads[name] = String(params.value[name] ?? '')
}

const spare = computed(() => props.models
  .filter(x => !members.value.some(m => m.backend === x.name))
  .flatMap(x => x.versions.filter(v => v.state === 'ready').map(v => ({ ...v, name: x.name }))))
const label = v => [v.name, PRECISION[v.precision] || v.precision, v.revision?.slice(0, 7)].filter(Boolean).join(' · ')
const pick = ref('')
async function add() {
  const v = spare.value.find(x => x.id === pick.value)
  pick.value = ''
  if (!v) return
  const name = v.name
  const taken = SOLE.filter(cat => members.value.some(m => role(m, cat)))
  const roles = CATS.filter(cat => cat in cats(name) && !taken.includes(cat))
    .map(cat => ({ role: cat, score_cutoff: null, yields: false }))
  try {
    await api(`/api/model-profiles/${props.profile.id}/members/${encodeURIComponent(name)}`, {
      method: 'PUT', body: { rank: 0, roles, version_id: v.id },
    })
    toast.ok(`已加入 ${name}`)
    props.onChanged()
  } catch (e) { toast.bad(e) }
}

async function remove(m) {
  try {
    await api(`/api/model-profiles/${props.profile.id}/members/${encodeURIComponent(m.backend)}`, { method: 'DELETE' })
    toast.ok(`已移除 ${m.backend}`)
    props.onChanged()
  } catch (e) { toast.bad(e) }
}
</script>

<template>
  <div class="card">
    <div class="card-h">
      <b>門檻</b>
      <span class="count">{{ members.length }} 個模型</span>
    </div>
    <div class="mx-wrap">
      <div class="mx">
        <div class="mx-h mx-corner">模型</div>
        <div v-for="cat in CATS" :key="cat" class="mx-h">
          <b>{{ ROLE[cat] }}</b>
          <label v-for="[name, label] in HEAD[cat]" :key="name" class="mx-hp" :title="name">
            <span>{{ label }}</span>
            <input v-model="heads[name]" inputmode="decimal"
                   :class="['num', { dirty: paramDirty(name) && !paramBad(name), bad: paramDirty(name) && paramBad(name) }]" />
          </label>
          <span v-if="cat === 'embedding' && embedders !== 1" class="badge orange"
                title="使用中的設定檔需要剛好一個向量模型">{{ embedders ? `${embedders} 個` : '未指定' }}</span>
        </div>

        <template v-for="m in members" :key="m.backend">
          <div class="mx-r">
            <b>{{ m.backend }}</b>
            <small>{{ [PRECISION[m.precision], m.revision?.slice(0, 7)].filter(Boolean).join(' · ') }}</small>
            <ArmedButton class="sm quiet mx-rm" label="移除" confirm="確定移除？" :run="() => remove(m)" />
          </div>
          <template v-for="cat in CATS" :key="cat">
            <div v-if="!supported(m, cat)" class="mx-c na" title="模型不支援"></div>
            <div v-else :class="['mx-c', { dirty: cellDirty(m, cat), off: !drafts[m.backend][cat].on, set: RICH.includes(cat) && drafts[m.backend][cat].cut.trim() !== '' }]">
              <label class="mx-on"><input v-model="drafts[m.backend][cat].on" type="checkbox" />啟用</label>
              <template v-if="RICH.includes(cat)">
                <input v-model="drafts[m.backend][cat].cut" class="mx-cut" inputmode="decimal" aria-label="門檻"
                       :class="{ bad: bad(drafts[m.backend][cat]) }" :placeholder="inherited(cat)"
                       :disabled="!drafts[m.backend][cat].on" />
                <label class="mx-y">
                  <Switch v-model="drafts[m.backend][cat].yields" :disabled="!drafts[m.backend][cat].on" />
                  讓給官方門檻
                </label>
                <small class="mx-n">官方門檻 {{ n(m.thresholds[cat]) }} 個</small>
              </template>
            </div>
          </template>
        </template>
      </div>
      <p v-if="!members.length" class="mx-none">沒有資料</p>
    </div>
    <div class="card-f mx-f">
      <select v-model="pick" :disabled="!spare.length" @change="add">
        <option value="">加入模型</option>
        <option v-for="v in spare" :key="v.id" :value="v.id">{{ label(v) }}</option>
      </select>
      <template v-if="dirtyCells">
        <button class="btn quiet sm mx-left" :disabled="busy" @click="revert">取消</button>
        <button class="btn primary sm" :disabled="busy || anyBad" @click="save">儲存 {{ dirtyCells }} 項</button>
      </template>
    </div>
  </div>
</template>

<style>
.admin .mx-wrap { overflow: auto; }
.admin .mx {
  display: grid; font-size: var(--fs-sm); font-variant-numeric: tabular-nums;
  grid-template-columns: 8rem repeat(6, minmax(0, 1fr));
}
.admin .mx-h {
  display: flex; flex-direction: column; gap: 6px; padding: 10px 12px;
  background: var(--surface2); color: var(--text2); font-size: var(--fs-xs); font-weight: 500;
  border-bottom: 1px solid var(--border-strong); border-left: 1px solid var(--border-soft);
  position: sticky; top: 0; z-index: 2;
}
.admin .mx-h b { font-size: var(--fs-sm); color: var(--text); }
.admin .mx-h .badge { align-self: flex-start; }
.admin .mx-corner { left: 0; z-index: 3; border-left: 0; justify-content: flex-end; }
.admin .mx-hp { display: flex; align-items: center; justify-content: space-between; gap: var(--s-2); white-space: nowrap; }
.admin .mx-hp input.num { width: 4.5rem; height: 28px; }
.admin .mx-r {
  display: flex; flex-direction: column; justify-content: center; gap: 4px; padding: 10px 16px;
  background: var(--surface); border-right: 1px solid var(--border); border-bottom: 1px solid var(--border-soft);
  position: sticky; left: 0; z-index: 1;
}
.admin .mx-r b { font-size: var(--fs-sm); font-weight: 500; }
.admin .mx-r small { font-family: var(--mono); font-size: var(--fs-xs); color: var(--text3); }
.admin .mx-rm { align-self: flex-start; margin-left: -10px; }
.admin .mx-c {
  position: relative; display: flex; flex-direction: column; gap: 6px; padding: 10px 12px;
  border-bottom: 1px solid var(--border-soft); border-left: 1px solid var(--border-soft);
}
.admin .mx-c.set { background: var(--accent-soft); }
.admin .mx-c.dirty::before {
  content: ''; position: absolute; top: 5px; right: 5px;
  width: 6px; height: 6px; border-radius: 50%; background: var(--orange);
}
.admin .mx-c.na { background: repeating-linear-gradient(135deg, transparent 0 6px, rgba(148, 163, 184, .08) 6px 7px); }
.admin .mx-c.off .mx-y, .admin .mx-c.off .mx-n { opacity: .45; }
.admin .mx-on, .admin .mx-y { display: flex; align-items: center; gap: 8px; color: var(--text2); cursor: pointer; }
.admin .mx-y { gap: 6px; white-space: nowrap; font-size: var(--fs-xs); }
.admin .mx-cut { width: 100%; height: 30px; font-family: var(--mono); }
.admin .mx-cut::placeholder { font-family: inherit; }
.admin .mx-cut:disabled { opacity: .45; }
.admin .mx-n { font-size: var(--fs-xs); color: var(--text3); }
.admin .mx-none { padding: var(--s-4); color: var(--text3); text-align: center; }
.admin .mx-f { display: flex; align-items: center; gap: var(--s-2); }
.admin .mx-f select { height: 28px; }
.admin .mx-left { margin-left: auto; }
</style>
