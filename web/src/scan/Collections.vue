<script setup>
import { computed, inject, nextTick, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import ArmedButton from '../admin/ArmedButton.vue'
import EmptyState from '../admin/EmptyState.vue'
import Switch from '../admin/Switch.vue'
import { n } from '../admin/format'
import { usePoll } from '../admin/poll'
import { useToast } from '../admin/toast'
import Picker from './Picker.vue'
import ScanOptions from './ScanOptions.vue'

const props = defineProps({ profiles: { type: Array, default: () => [] } })
const emit = defineEmits(['start'])

const { collections, loadCollections } = inject('library')
const poll = usePoll()
const toast = useToast()
const router = useRouter()

const active = computed(() => props.profiles.find(p => p.active)?.name || '')
const url = name => `/api/collections/${encodeURIComponent(name)}`
const leaf = p => p.split(/[\\/]/).filter(Boolean).pop() || p

const order = ref([])
watch(collections, list => { order.value = [...list] }, { immediate: true })

async function reload() {
  try { await loadCollections() } catch (e) { toast.bad(e) }
}

async function run(fn) {
  try { return await fn() } catch (e) { toast.bad(e) }
}

let dragged = null
const dragStart = i => { dragged = i }
function dragOver(i) {
  if (dragged === null || dragged === i) return
  const list = order.value
  list.splice(i, 0, list.splice(dragged, 1)[0])
  dragged = i
}
async function dragEnd() {
  if (dragged === null) return
  dragged = null
  const moved = order.value.filter((c, i) => c.ordinal !== i)
  if (!moved.length) return
  await run(async () => {
    for (const c of moved) {
      await api(url(c.name), { method: 'PATCH', body: { ordinal: order.value.indexOf(c) } })
    }
  })
  await reload()
}

const searchable = (c, on) => run(async () => {
  await api(url(c.name), { method: 'PATCH', body: { searchable: on } })
  await reload()
})

const menu = ref('')
function closeMenu(e) {
  if (!e.currentTarget.contains(e.relatedTarget)) menu.value = ''
}

const renaming = reactive({ was: '', name: '' })
async function startRename(c) {
  menu.value = ''
  Object.assign(renaming, { was: c.name, name: c.name })
  await nextTick()
  document.querySelector('.admin .col-rename')?.select()
}
async function rename() {
  const to = renaming.name.trim()
  const was = renaming.was
  if (!was) return
  if (!to) return toast.bad('名稱不能空白')
  if (to === was) { renaming.was = ''; return }
  await run(async () => {
    await api(url(was), { method: 'PATCH', body: { name: to } })
    renaming.was = ''
    await reload()
    toast.undo(`已改名為「${to}」`, async () => {
      await api(url(to), { method: 'PATCH', body: { name: was } })
      await reload()
    })
  })
}

async function remove(c) {
  menu.value = ''
  await run(async () => {
    await api(url(c.name), { method: 'DELETE' })
    await reload()
    toast.undo(`已移除「${c.name}」`, async () => {
      await api('/api/collections', {
        method: 'POST',
        body: { name: c.name, root: c.root, searchable: c.searchable, ordinal: c.ordinal },
      })
      await reload()
    })
  })
}

const picker = ref(null)
const moving = ref(null)
const adding = ref(null)
const rescanning = ref(null)

function pickFor(kind, c) {
  menu.value = ''
  const start = kind === 'register' ? c.root
    : kind === 'relocate' ? c.root.replace(/[\\/][^\\/]*[\\/]?$/, '') : ''
  picker.value = { kind, c, start }
}

const PICK = {
  add: { title: '新增收藏庫', mode: 'folder', submit: '選取' },
  relocate: { title: '變更位置', mode: 'folder', submit: '選取' },
  register: { title: '登記作品', mode: 'register', submit: '登記' },
  check: { title: '檢查重複', mode: 'check', submit: '檢查重複' },
}

const reg = reactive({ profile: '', force: false })
watch(active, a => { reg.profile ||= a }, { immediate: true })

async function picked(paths) {
  const { kind, c } = picker.value
  picker.value = null
  if (kind === 'add') {
    adding.value = { root: paths[0], name: leaf(paths[0]) }
    await nextTick()
    document.querySelector('.admin .col-add input')?.select()
  } else if (kind === 'relocate') {
    moving.value = { name: c.name, root: paths[0], found: null, sampled: null }
    const r = await run(() =>
      api(`${url(c.name)}/relocate?root=${encodeURIComponent(paths[0])}`))
    if (r && moving.value?.name === c.name) Object.assign(moving.value, r)
    else if (!r) moving.value = null
  } else if (kind === 'register') {
    await scan({ paths, profile: reg.profile || null, force: reg.force })
  } else {
    await run(async () => {
      const r = await api('/api/check/jobs', { method: 'POST', body: { paths } })
      queued(`檢查重複 #${r.job_id} 已排入`, {
        label: '在搜尋頁開啟',
        run: () => router.push({ path: '/search', query: { view: 'check', job: r.job_id } }),
      })
    })
  }
}

function queued(text, action) {
  poll.refresh()
  const live = poll.worker?.workers.some(w => !w.stale)
  if (!action && !live) action = { label: '啟動', run: () => emit('start') }
  toast.show(text, { tone: 'ok', action, timeout: 8000 })
}

function scan(body) {
  return run(async () => {
    const r = await api('/api/scan/jobs', { method: 'POST', body })
    queued(`掃描 #${r.job_id} 已排入`)
    return r
  })
}

const rescan = c => scan({ collection: c.name, profile: null, force: false })

function openRescan(c) {
  rescanning.value = rescanning.value?.name === c.name
    ? null
    : { name: c.name, profile: active.value, force: false }
}
async function rescanWith() {
  const o = rescanning.value
  if (await scan({ collection: o.name, profile: o.profile || null, force: o.force })) {
    rescanning.value = null
  }
}

async function add() {
  const a = adding.value
  const name = a.name.trim()
  if (!name) return toast.bad('名稱不能空白')
  await run(async () => {
    await api('/api/collections', { method: 'POST', body: { name, root: a.root } })
    adding.value = null
    await reload()
    toast.ok(`已新增「${name}」`)
  })
}

async function relocate() {
  const m = moving.value
  await run(async () => {
    await api(`${url(m.name)}/relocate`, { method: 'POST', body: { root: m.root } })
    moving.value = null
    await reload()
    toast.ok(`已變更「${m.name}」的位置`)
  })
}
</script>

<template>
  <div class="card cols">
    <div class="card-h">
      <b>收藏庫</b>
      <span class="count">{{ n(order.length) }} 個</span>
      <button class="btn" @click="pickFor('check')">檢查重複</button>
      <button class="btn primary" @click="pickFor('add')">新增收藏庫</button>
    </div>

    <div class="col-strip col-add" v-if="adding">
      <label>名稱 <input v-model="adding.name" @keydown.enter="add" @keydown.esc="adding = null" /></label>
      <span class="lit-c">{{ adding.root }}</span>
      <button class="btn quiet sm" @click="adding = null">取消</button>
      <button class="btn primary sm" @click="add">新增收藏庫</button>
    </div>

    <div class="row-g row-h col-row">
      <span></span><span>名稱</span><span>位置</span>
      <span class="num-c">作品</span><span>搜尋</span><span></span>
    </div>

    <template v-for="(c, i) in order" :key="c.name">
      <div :class="['row-g', 'col-row', { on: menu === c.name }]"
           :draggable="renaming.was !== c.name"
           @dragstart="dragStart(i)" @dragover.prevent="dragOver(i)" @dragend="dragEnd">
        <span class="col-grip" title="拖曳排序">⠿</span>
        <span class="col-name">
          <input v-if="renaming.was === c.name" v-model="renaming.name" class="col-rename"
                 @keydown.enter="rename" @keydown.esc="renaming.was = ''" @blur="renaming.was = ''" />
          <template v-else>
            <b>{{ c.name }}</b>
          </template>
        </span>
        <span class="lit-c col-root" :title="c.root">{{ c.root }}</span>
        <span class="num-c">{{ c.works ? n(c.works) : '—' }}</span>
        <Switch :model-value="c.searchable" title="列入搜尋與全部重新掃描"
                @update:model-value="v => searchable(c, v)" />
        <span class="acts">
          <button class="btn sm" @click="pickFor('register', c)">登記作品</button>
          <span class="col-split">
            <button class="btn sm" :disabled="!c.works" @click="rescan(c)">重新掃描</button>
            <button class="btn sm" :disabled="!c.works" aria-label="進階" title="進階"
                    @click="openRescan(c)">▾</button>
          </span>
          <span class="col-more" @focusout="closeMenu">
            <button class="btn quiet sm" :aria-expanded="menu === c.name"
                    @click="menu = menu === c.name ? '' : c.name">更多</button>
            <span class="col-menu" v-if="menu === c.name">
              <button @click="startRename(c)">改名</button>
              <button @click="pickFor('relocate', c)">變更位置</button>
              <button class="danger" @click="remove(c)">移除</button>
            </span>
          </span>
        </span>
      </div>

      <div class="col-strip" v-if="moving?.name === c.name">
        <span>新位置</span>
        <span class="lit-c">{{ moving.root }}</span>
        <span v-if="moving.sampled === null" class="badge">載入中</span>
        <span v-else :class="['badge', moving.found < moving.sampled ? 'orange' : 'green']"
              title="抽樣已登記的路徑，在新位置下找到的數量">
          找到 {{ moving.found }} / {{ moving.sampled }}
        </span>
        <button class="btn quiet sm" @click="moving = null">取消</button>
        <ArmedButton class="sm" label="變更位置" confirm="確定變更？" busy-label="變更中"
                     :disabled="moving.sampled === null" :run="relocate" />
      </div>

      <div class="col-strip col-opts" v-if="rescanning?.name === c.name">
        <ScanOptions v-model:profile="rescanning.profile" v-model:force="rescanning.force"
                     :profiles="profiles" open />
        <button class="btn quiet sm" @click="rescanning = null">取消</button>
        <button class="btn primary sm" @click="rescanWith">重新掃描</button>
      </div>
    </template>

    <EmptyState v-if="!order.length" title="沒有資料" />

    <Picker v-if="picker" v-bind="PICK[picker.kind]" :start="picker.start"
            :collections="collections" @close="picker = null" @pick="picked">
      <ScanOptions v-if="picker.kind === 'register'" v-model:profile="reg.profile"
                   v-model:force="reg.force" :profiles="profiles" />
    </Picker>
  </div>
</template>

<style>
.admin .card.cols { overflow: visible; }
.admin .col-row { grid-template-columns: 16px minmax(7rem, 1fr) minmax(0, 2.4fr) 5rem 3rem 18rem; }
.admin .col-row[draggable=true] { cursor: grab; }
.admin .col-grip { color: var(--text3); user-select: none; }
.admin .col-name { display: flex; align-items: center; gap: var(--s-2); min-width: 0; }
.admin .col-name b { font-weight: 500; font-size: var(--fs-md); }
.admin .col-name input { width: 100%; height: 28px; }
.admin .col-root { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.admin .col-split { display: inline-flex; }
.admin .col-split .btn:first-child { border-top-right-radius: 0; border-bottom-right-radius: 0; }
.admin .col-split .btn:last-child { border-top-left-radius: 0; border-bottom-left-radius: 0; border-left: 0; padding: 0 8px; }
.admin .col-more { position: relative; }
.admin .col-menu {
  position: absolute; right: 0; top: calc(100% + 4px); z-index: 30; min-width: 8rem;
  display: flex; flex-direction: column; padding: 4px;
  background: var(--raised); border: 1px solid var(--border-strong);
  border-radius: var(--r-md); box-shadow: var(--shadow-pop);
}
.admin .col-menu button { text-align: left; padding: 6px 10px; border-radius: var(--r-sm); font-size: var(--fs-sm); }
.admin .col-menu button:hover { background: var(--surface-h); color: var(--accent2); }
.admin .col-menu button.danger:hover { color: var(--red); }
.admin .col-row.on .acts { opacity: 1; }
.admin .col-strip {
  display: flex; align-items: center; gap: var(--s-3); flex-wrap: wrap;
  padding: 8px 16px 8px 48px; background: var(--surface2);
  border-bottom: 1px solid var(--border-soft); font-size: var(--fs-sm); color: var(--text2);
}
.admin .col-strip .lit-c { flex: 1; min-width: 12rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.admin .col-add label { display: inline-flex; align-items: center; gap: var(--s-2); }
.admin .col-add input { width: 14rem; }
.admin .col-opts .sopt { flex: 1; border: 0; padding: 0; }
.admin .col-opts summary { display: none; }
.admin .col-opts .sopt-b { padding: 0; }
</style>
