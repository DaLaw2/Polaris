<script setup>
import { computed, reactive, ref } from 'vue'
import { api } from '../api'
import ArmedButton from '../admin/ArmedButton.vue'
import { KIND, STATE, ago, n, pct } from '../admin/format'
import { useToast } from '../admin/toast'
import { PRECISION, STEP, stateOf } from './version'

const props = defineProps({
  def: { type: Object, required: true },
  jobs: { type: Array, required: true },
  onChanged: { type: Function, default: () => {} },
})
const toast = useToast()

const d = computed(() => props.def)
const src = computed(() => d.value.source)
const jobOf = v => props.jobs.find(j => j.model_version_id === v.id) || null
const inUse = reactive({})
const precision = ref('fp32')

async function remove() {
  try {
    const r = await api(`/api/model-definitions/${d.value.id}`, { method: 'DELETE' })
    toast.ok(r.retracted ? `已撤回「${r.name}」` : `已刪除「${r.name}」`)
    props.onChanged(r.retracted ? d.value.id : null)
  } catch (e) { toast.bad(e) }
}

async function restore() {
  try {
    await api('/api/model-definitions', { method: 'POST', body: { text: d.value.source_text } })
    toast.ok(`已還原「${d.value.name}」`)
    props.onChanged()
  } catch (e) { toast.bad(e) }
}

async function build(p) {
  try {
    const r = await api(`/api/model-definitions/${d.value.id}/build`, { method: 'POST', body: { precision: p } })
    toast.ok(`${KIND.build} #${r.job_id} ${STATE.queued}`)
    props.onChanged()
  } catch (e) { toast.bad(e) }
}

async function removeVersion(v) {
  delete inUse[v.id]
  try {
    await api(`/api/model-versions/${v.id}`, { method: 'DELETE' })
    toast.ok(`已刪除版本 #${v.id}`)
    props.onChanged()
  } catch (e) {
    if (e.code === 'version_in_use') inUse[v.id] = e.detail.profiles
    else toast.bad(e)
  }
}

const drift = v => (v.drift == null ? '' : `${(v.drift * 100).toFixed(1)}%`)
const stuck = v => v.state === 'failed' || (v.state === 'building' && !jobOf(v))
</script>

<template>
  <div class="card md-detail dd">
    <div class="card-h">
      <b>{{ d.name }}</b>
      <span v-if="d.retracted_at" class="badge">已撤回</span>
      <span class="dd-acts">
        <button v-if="d.retracted_at" class="btn sm" @click="restore">還原</button>
        <ArmedButton v-else-if="d.versions.length" class="sm" label="撤回" confirm="確定撤回？" :run="remove" />
        <ArmedButton v-else class="sm" label="刪除" :run="remove" />
      </span>
    </div>

    <div class="md-scroll">
      <dl class="inset dd-info">
        <dt>來源</dt><dd class="lit-c">{{ src.repo || src.path }}</dd>
        <template v-if="src.subfolder"><dt>子目錄</dt><dd class="lit-c">{{ src.subfolder }}</dd></template>
        <dt>模型檔</dt><dd class="lit-c">{{ src.model }}</dd>
        <template v-if="src.repo">
          <dt>修訂版</dt>
          <dd class="lit-c" :title="src.revision || ''">{{ src.revision ? src.revision.slice(0, 7) : '未指定' }}</dd>
        </template>
        <template v-if="d.note"><dt>說明</dt><dd>{{ d.note }}</dd></template>
        <dt>上傳</dt><dd :title="d.uploaded_at">{{ ago(d.uploaded_at) }}</dd>
      </dl>

      <div class="card">
        <div class="card-h">
          <b>版本</b>
          <span class="count">{{ d.versions.length }} 個</span>
        </div>
        <div class="row-g row-h dv-row">
          <span class="num-c">#</span><span>精度</span><span>狀態</span>
          <span class="num-c">偏移</span><span class="num-c">向量</span><span></span>
        </div>
        <template v-for="v in d.versions" :key="v.id">
          <div class="row-g dv-row">
            <span class="num-c dv-id">{{ v.id }}</span>
            <span>{{ PRECISION[v.precision] || v.precision }}</span>
            <span><span :class="['badge', stateOf(v, jobOf(v)).tone]">{{ stateOf(v, jobOf(v)).text }}</span></span>
            <span class="num-c">
              <span v-if="v.state === 'ready' && v.drift == null" class="badge orange">未驗證</span>
              <template v-else>{{ drift(v) || '—' }}</template>
            </span>
            <span class="num-c">{{ v.embedding_dim ? n(v.embedding_dim) : '—' }}</span>
            <span class="dv-acts">
              <button v-if="stuck(v)" class="btn sm" :disabled="!!d.retracted_at"
                      @click="build(v.precision)">重新建置</button>
              <ArmedButton v-if="!jobOf(v)" class="sm quiet" label="刪除" :run="() => removeVersion(v)" />
            </span>
          </div>
          <div v-if="jobOf(v)?.state === 'running'" class="dv-sub dv-prog">
            <span>{{ STEP[jobOf(v).current_path] || '準備中' }}</span>
            <span class="track"><i :style="{ width: pct(jobOf(v).done, jobOf(v).total || 4) + '%' }"></i></span>
            <span class="dv-num">{{ jobOf(v).done }} / {{ jobOf(v).total || 4 }}</span>
          </div>
          <p v-if="v.state === 'failed' && v.error" class="dv-sub dv-err">{{ v.error }}</p>
          <p v-if="inUse[v.id]" class="dv-sub dv-err">使用中：{{ inUse[v.id].join('、') }}</p>
        </template>
        <p v-if="!d.versions.length" class="dv-none">沒有資料</p>
        <div class="card-f dv-f">
          <div class="seg">
            <button v-for="(label, k) in PRECISION" :key="k" :class="{ on: precision === k }"
                    @click="precision = k">{{ label }}</button>
          </div>
          <button class="btn primary sm" :disabled="!!d.retracted_at" :title="d.retracted_at ? '已撤回' : ''"
                  @click="build(precision)">建置</button>
        </div>
      </div>

      <details class="card dd-src">
        <summary class="card-h"><b>定義內容</b></summary>
        <pre>{{ d.source_text }}</pre>
      </details>
    </div>
  </div>
</template>

<style>
.admin .dd .md-scroll > .card { margin: 0; flex: none; }
.admin .dd-acts { margin-left: auto; display: flex; gap: var(--s-2); }
.admin .dd-info { display: grid; grid-template-columns: 6rem minmax(0, 1fr); gap: 6px var(--s-4); margin: 0; font-size: var(--fs-sm); }
.admin .dd-info dt { color: var(--text3); }
.admin .dd-info dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.admin .dv-row { grid-template-columns: 3rem 4rem 5rem 5rem 5rem minmax(0, 1fr); }
@media (max-width: 1100px) {
  .admin .dv-row { grid-template-columns: 2.5rem 3.5rem 4.5rem 4.5rem 4rem; gap: var(--s-3); }
  .admin .dv-acts { grid-column: 1 / -1; }
  .admin .dv-acts:empty { display: none; }
  .admin .dv-sub { padding-left: 16px; }
}
.admin .dv-id { color: var(--text3); }
.admin .dv-acts { display: flex; gap: var(--s-1); justify-content: flex-end; }
.admin .dv-sub {
  margin: 0; padding: 8px 16px 10px calc(3rem + 16px + var(--s-4));
  border-bottom: 1px solid var(--border-soft); font-size: var(--fs-sm);
}
.admin .dv-prog { display: grid; grid-template-columns: 4rem minmax(0, 20rem) auto; align-items: center; gap: var(--s-3); color: var(--text2); }
.admin .dv-prog .track { background: var(--border); }
.admin .dv-num { font-size: var(--fs-xs); font-variant-numeric: tabular-nums; }
.admin .dv-err { color: var(--red); white-space: pre-wrap; overflow-wrap: anywhere; }
.admin .dv-none { padding: var(--s-4); color: var(--text3); text-align: center; }
.admin .dv-f { display: flex; align-items: center; justify-content: flex-end; gap: var(--s-2); }
.admin .dd-src summary { cursor: pointer; list-style: none; border-bottom: 0; }
.admin .dd-src summary::-webkit-details-marker { display: none; }
.admin .dd-src summary::before { content: '›'; color: var(--text3); transition: transform var(--t-base); }
.admin .dd-src[open] summary::before { transform: rotate(90deg); }
.admin .dd-src[open] summary { border-bottom: 1px solid var(--border-soft); }
.admin .dd-src pre {
  margin: 0; padding: 12px 16px; max-height: 32rem; overflow: auto;
  font-family: var(--mono); font-size: var(--fs-xs); line-height: 1.6; color: var(--text2); white-space: pre-wrap;
}
</style>
