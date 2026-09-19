<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import ArmedButton from '../admin/ArmedButton.vue'
import { KIND, STATE, n } from '../admin/format'
import { usePoll } from '../admin/poll'
import Switch from '../admin/Switch.vue'
import { useToast } from '../admin/toast'
import Params from '../scan/Params.vue'
import Explain from './Explain.vue'
import Matrix, { HEAD_PARAMS } from './Matrix.vue'

const props = defineProps({
  profile: { type: Object, required: true },
  works: { type: Number, required: true },
  models: { type: Array, required: true },
  job: { type: Object, default: null },
  onChanged: { type: Function, default: () => {} },
})
const emit = defineEmits(['copy'])
const toast = useToast()
const poll = usePoll()
const matrix = ref(null)

const p = computed(() => props.profile)
const base = () => `/api/model-profiles/${p.value.id}`

const renaming = ref(null)
async function rename() {
  const name = renaming.value.trim()
  if (!name) return toast.bad('名稱不能空白')
  if (name === p.value.name) return (renaming.value = null)
  await edit({ name }, '已改名')
  renaming.value = null
}

async function edit(body, done) {
  try {
    await api(base(), { method: 'PATCH', body })
    toast.ok(done)
    props.onChanged()
  } catch (e) { toast.bad(e) }
}

const offing = ref(false)
function toggle(on) {
  if (on) edit({ maintained: true }, '已開啟保持更新')
  else offing.value = true
}
async function stopMaintaining() {
  await edit({ maintained: false }, '已關閉保持更新')
  offing.value = false
}

const deriving = computed(() => !!props.job || p.value.deriving != null)
const embedders = computed(() =>
  p.value.members.filter(m => m.roles.some(r => r.role === 'embedding')).length)
const activateBlock = computed(() => {
  if (!p.value.maintained) return '需要保持更新'
  if (deriving.value) return '計算中'
  if (p.value.works < props.works) return `需要重新計算（${n(p.value.works)} / ${n(props.works)}）`
  if (embedders.value !== 1) return '需要剛好一個向量模型'
  return ''
})
const activate = () => edit({ active: true }, `已將「${p.value.name}」設為使用中`)

async function remove() {
  try {
    await api(base(), { method: 'DELETE' })
    toast.ok(`已刪除「${p.value.name}」`)
    props.onChanged(null)
  } catch (e) { toast.bad(e) }
}

const coverage = ref(null)
async function loadCoverage() {
  try { coverage.value = (await api(`${base()}/coverage`)).members } catch (e) { toast.bad(e) }
}
const missing = computed(() => (coverage.value || []).some(c => c.searchable.scored < c.searchable.works))
async function scanMissing() {
  try {
    const r = await api('/api/scan/jobs', { method: 'POST', body: { profile: p.value.name } })
    toast.ok(`${KIND.scan} #${r.job_id} ${STATE.queued}`)
    poll.refresh()
  } catch (e) { toast.bad(e) }
}

function membersChanged() {
  props.onChanged()
  loadCoverage()
}

onMounted(loadCoverage)
watch(() => poll.finished, loadCoverage)
</script>

<template>
  <div class="card md-detail pd">
    <div class="card-h pd-h">
      <template v-if="renaming !== null">
        <input v-model="renaming" class="pd-name" aria-label="名稱"
               @keydown.enter="rename" @keydown.esc="renaming = null" />
        <button class="btn quiet sm" @click="renaming = null">取消</button>
        <button class="btn primary sm" @click="rename">儲存</button>
      </template>
      <template v-else>
        <b>{{ p.name }}</b>
        <span v-if="p.active" class="badge green">使用中</span>
        <button class="btn quiet sm icon-btn" title="改名" aria-label="改名" @click="renaming = p.name"><svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M11 2.5l2.5 2.5L5.5 13H3v-2.5z"/></svg></button>
      </template>
      <span class="pd-acts">
        <button class="btn sm" @click="emit('copy')">複製</button>
        <ArmedButton v-if="!p.active" class="sm" label="設為使用中" confirm="確定切換？"
                     :disabled="!!activateBlock" :title="activateBlock" :run="activate" />
        <ArmedButton class="sm" label="刪除" :disabled="p.active"
                     :title="p.active ? '使用中的設定檔無法刪除' : ''" :run="remove" />
      </span>
    </div>

    <div class="md-scroll">
      <div class="inset pd-state">
        <label class="pd-sw" :title="p.active ? '使用中的設定檔必須保持更新' : ''">
          <Switch :model-value="p.maintained && !offing" :disabled="p.active || offing" @update:model-value="toggle" />
          保持更新
        </label>
        <template v-if="offing">
          <ArmedButton class="sm" label="關閉並刪除計算結果" confirm="確定關閉？" :run="stopMaintaining" />
          <button class="btn quiet sm" @click="offing = false">取消</button>
        </template>
        <span v-else-if="job" class="pd-job">
          <span class="badge blue">{{ job.state === 'running' ? '計算中' : STATE[job.state] }}</span>
          <template v-if="job.total">
            <span class="track"><i :style="{ width: (job.done / job.total) * 100 + '%' }"></i></span>
            <span>{{ n(job.done) }} / {{ n(job.total) }}</span>
          </template>
        </span>
        <span v-else-if="p.maintained" class="pd-count">{{ n(p.works) }} / {{ n(works) }}</span>
      </div>

      <div v-if="p.active" class="inset pd-warn">
        <span>修改會重新計算</span>
      </div>

      <Matrix ref="matrix" :profile="p" :models="models" @changed="membersChanged" />

      <div class="card">
        <div class="card-h">
          <b>已分析</b>
          <button v-if="missing" class="btn sm soft pd-scan" @click="scanMissing">分析缺少的</button>
        </div>
        <div class="row-g row-h pd-cov">
          <span>模型</span><span class="num-c">可搜尋收藏庫</span><span class="num-c">其他收藏庫</span>
        </div>
        <div v-for="c in coverage || []" :key="c.backend" class="row-g pd-cov">
          <span>{{ c.backend }}</span>
          <span :class="['num-c', { 'pd-lack': c.searchable.scored < c.searchable.works }]">
            已分析 {{ n(c.searchable.scored) }} / {{ n(c.searchable.works) }}</span>
          <span :class="['num-c', { 'pd-lack': c.other.scored < c.other.works }]">
            已分析 {{ n(c.other.scored) }} / {{ n(c.other.works) }}</span>
        </div>
        <p v-if="!coverage || !coverage.length" class="mx-none">{{ coverage ? '沒有資料' : '載入中' }}</p>
      </div>

      <Explain :profile="p.id" />

      <Params :profile="p.id" :skip="HEAD_PARAMS" @saved="matrix?.loadParams()" />
    </div>
  </div>
</template>

<style>
.admin .pd .md-scroll > .card { margin: 0; flex: none; }
.admin .pd-h { flex-wrap: wrap; }
.admin .pd-name { width: 16rem; height: 30px; }
.admin .pd-acts { margin-left: auto; display: flex; gap: var(--s-2); }
.admin .pd-state, .admin .pd-warn { display: flex; align-items: center; gap: var(--s-3); font-size: var(--fs-sm); }
.admin .pd-sw { display: flex; align-items: center; gap: 10px; cursor: pointer; }
.admin .pd-job, .admin .pd-count { margin-left: auto; display: flex; align-items: center; gap: var(--s-3); color: var(--text2); font-variant-numeric: tabular-nums; }
.admin .pd-job .track { width: 12rem; background: var(--border); }
.admin .pd-warn { background: var(--orange-soft); color: var(--orange); }
.admin .pd-warn .btn { margin-left: auto; }
.admin .pd-scan { margin-left: auto; }
.admin .pd-cov { grid-template-columns: minmax(8rem, 1fr) 14rem 14rem; }
.admin .pd-lack { color: var(--orange); }
</style>
