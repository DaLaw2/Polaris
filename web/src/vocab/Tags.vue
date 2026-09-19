<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { api } from '../api'
import EmptyState from '../admin/EmptyState.vue'
import Switch from '../admin/Switch.vue'
import { n } from '../admin/format'
import { useToast } from '../admin/toast'

const CAT = { general: '一般', character: '角色', copyright: '系列', artist: '作者', rating: '分級', meta: '其他' }

const toast = useToast()
const tags = ref([])
const loading = ref(true)
const view = ref('all')
const q = ref('')

const VIEWS = computed(() => {
  const hidden = tags.value.filter(t => !t.visible).length
  return [
    { key: 'all', label: '全部', count: tags.value.length },
    { key: 'shown', label: '顯示中', count: tags.value.length - hidden },
    { key: 'hidden', label: '已隱藏', count: hidden },
  ]
})

const shown = computed(() => {
  const s = q.value.trim().toLowerCase()
  return tags.value.filter(t =>
    (view.value === 'all' || t.visible === (view.value === 'shown')) &&
    (!s || t.tag.toLowerCase().includes(s) || (t.zh || '').toLowerCase().includes(s)))
})

async function load() {
  try { tags.value = await api('/api/tags/all?limit=20000') } catch (e) { toast.bad(e) }
  loading.value = false
}
onMounted(load)

async function setVisible(t, on) {
  const flip = v => api(`/api/tags/${v ? 'show' : 'hide'}?tag=${encodeURIComponent(t.tag)}`, { method: 'POST' })
  t.visible = on
  try {
    await flip(on)
    toast.undo(`已${on ? '顯示' : '隱藏'}「${t.tag}」`, async () => {
      t.visible = !on
      await flip(!on).catch(e => { t.visible = on; throw e })
    })
  } catch (e) {
    t.visible = !on
    toast.bad(e)
  }
}

const editing = ref(null)
const draft = ref('')
async function edit(t) {
  editing.value = t.tag
  draft.value = t.zh || ''
  await nextTick()
  document.querySelector('.admin .tg-zh input')?.select()
}

const putZh = (t, zh) => api(`/api/translations/${encodeURIComponent(t.tag)}`, { method: 'PUT', body: { display_zh: zh } })
async function saveZh(t) {
  const was = t.zh || ''
  const zh = draft.value.trim()
  editing.value = null
  if (zh === was) return
  t.zh = zh
  try {
    await putZh(t, zh)
    toast.undo(`已儲存「${t.tag}」的中文名稱`, async () => {
      t.zh = was
      await putZh(t, was).catch(e => { t.zh = zh; throw e })
    })
  } catch (e) {
    t.zh = was
    toast.bad(e)
  }
}

function parse(name, text) {
  if (/\.json$/i.test(name)) {
    const m = JSON.parse(text)
    if (!m || typeof m !== 'object' || Array.isArray(m)) throw new Error('JSON 必須是物件')
    return Object.fromEntries(Object.entries(m).filter(([, v]) => typeof v === 'string' && v.trim()))
  }
  const out = {}
  for (const line of text.replace(/^\uFEFF/, '').split(/\r?\n/)) {
    const [tag, ...rest] = line.split(',')
    const zh = rest.join(',').trim().replace(/^"|"$/g, '')
    if (tag?.trim() && zh) out[tag.trim().replace(/^"|"$/g, '')] = zh
  }
  return out
}

const file = ref(null)
async function importFile(e) {
  const f = e.target.files[0]
  e.target.value = ''
  if (!f) return
  try {
    const mapping = parse(f.name, await f.text())
    const r = await api('/api/translations', { method: 'POST', body: mapping })
    toast.ok(`已讀取 ${n(r.read)} 筆，新增 ${n(r.added)} 筆`)
    await load()
  } catch (err) { toast.bad(err) }
}
</script>

<template>
  <div class="card tg">
    <div class="card-h">
      <div class="seg">
        <button v-for="v in VIEWS" :key="v.key" :class="{ on: view === v.key }"
                @click="view = v.key">{{ v.label }}<i>{{ n(v.count) }}</i></button>
      </div>
      <input v-model="q" type="search" class="tg-q" placeholder="搜尋" />
      <span class="count">{{ n(shown.length) }} 個</span>
      <button class="btn quiet sm" title="JSON 格式為 {&quot;標籤&quot;: &quot;中文名稱&quot;}，CSV 每行為「標籤,中文名稱」；已有中文名稱的標籤不會被覆蓋"
              @click="file.click()">匯入翻譯</button>
      <input ref="file" type="file" accept=".json,.csv" hidden @change="importFile" />
    </div>

    <div class="row-g row-h tg-row">
      <span>標籤</span><span>中文名稱</span><span>類別</span><span class="num-c">作品</span><span title="關閉後，作品與搜尋建議都不顯示此標籤">顯示</span>
    </div>
    <div v-for="t in shown" :key="t.tag" :class="['row-g', 'tg-row', { off: !t.visible }]">
      <span class="lit-c tg-tag">{{ t.tag }}</span>
      <span class="tg-zh">
        <input v-if="editing === t.tag" v-model="draft"
               @keydown.enter="saveZh(t)" @keydown.esc="editing = null" @blur="editing = null" />
        <button v-else type="button" title="編輯" @click="edit(t)">{{ t.zh || '—' }}</button>
      </span>
      <span class="tg-cats"><span v-for="c in t.categories" :key="c" class="badge">{{ CAT[c] || c }}</span></span>
      <span class="num-c">{{ n(t.works) }}</span>
      <Switch :model-value="t.visible" :title="t.visible ? '隱藏' : '顯示'"
              @update:model-value="v => setVisible(t, v)" />
    </div>
    <EmptyState v-if="!shown.length" :title="loading ? '載入中' : '沒有資料'" />

  </div>
</template>

<style>
.admin .tg .card-h { flex-wrap: wrap; }
.admin .tg-q { width: 14rem; height: 30px; margin-left: auto; }
.admin .tg .card-h .count { margin-left: 0; }
.admin .tg-row { grid-template-columns: minmax(0, 1.4fr) minmax(0, 1.2fr) minmax(0, 1fr) 5rem 3rem; }
.admin .tg-row:not(.row-h) { content-visibility: auto; contain-intrinsic-size: auto 42px; }
.admin .tg-row.off .tg-tag, .admin .tg-row.off .tg-zh button { color: var(--text3); }
.admin .tg-tag { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
.admin .tg-zh { min-width: 0; }
.admin .tg-zh button {
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  padding: 2px 6px; margin-left: -6px; border-radius: var(--r-sm); color: var(--text); text-align: left;
}
.admin .tg-zh button:hover { background: var(--surface2); color: var(--accent2); }
.admin .tg-zh input { width: 100%; height: 28px; }
.admin .tg-cats { display: flex; gap: 4px; flex-wrap: wrap; }
</style>
