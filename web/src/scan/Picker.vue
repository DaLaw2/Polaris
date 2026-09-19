<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'
import { n } from '../admin/format'
import { useToast } from '../admin/toast'

const props = defineProps({
  title: { type: String, required: true },
  mode: { type: String, default: 'folder' },
  start: { type: String, default: '' },
  collections: { type: Array, default: () => [] },
  submit: { type: String, default: '選取' },
})
const emit = defineEmits(['pick', 'close'])
const toast = useToast()

const here = ref(null)
const parent = ref(null)
const dirs = ref([])
const files = ref([])
const chosen = ref([])
const busy = ref(false)

const multi = computed(() => props.mode !== 'folder')
const fold = p => String(p).replace(/[\\/]+$/, '').toLowerCase()
const under = (p, root) => {
  const a = fold(p)
  const b = fold(root)
  return a === b || a.startsWith(b + '\\') || a.startsWith(b + '/')
}
const rootOf = p => props.collections.find(c => fold(c.root) === fold(p))

function blocked(e) {
  if (props.mode === 'folder') {
    const c = rootOf(e.path)
    return c ? c.name : null
  }
  if (e.work) return '已登記'
  if (props.mode === 'register' && e.holds) return `含 ${n(e.holds)} 部`
  if (props.mode === 'check' && props.collections.some(c => under(e.path, c.root))) return '收藏庫內'
  return null
}

const crumbs = computed(() => {
  if (!here.value) return []
  const sep = here.value.includes('\\') ? '\\' : '/'
  let at = ''
  return here.value.split(/[\\/]/).filter(Boolean).map(p => {
    at = at ? at + sep + p : p + sep
    return { name: p, path: at }
  })
})

async function open(path) {
  busy.value = true
  try {
    const r = await api(`/api/browse${path ? `?path=${encodeURIComponent(path)}` : ''}`)
    here.value = r.path
    parent.value = r.parent
    dirs.value = r.dirs
    files.value = multi.value ? r.files : []
  } catch (e) { toast.bad(e) }
  busy.value = false
}

function toggle(e) {
  if (blocked(e)) return
  if (!multi.value) { chosen.value = chosen.value[0] === e.path ? [] : [e.path]; return }
  const i = chosen.value.indexOf(e.path)
  i < 0 ? chosen.value.push(e.path) : chosen.value.splice(i, 1)
}

async function allUnregistered() {
  busy.value = true
  try {
    const r = await api(`/api/browse/unregistered?path=${encodeURIComponent(here.value)}`)
    const add = r.items.map(i => i.path).filter(p => !chosen.value.includes(p))
    chosen.value.push(...add)
  } catch (e) { toast.bad(e) }
  busy.value = false
}

const leaf = p => p.split(/[\\/]/).filter(Boolean).pop() || p
const onKey = e => { if (e.key === 'Escape') emit('close') }
onMounted(() => { window.addEventListener('keydown', onKey); open(props.start) })
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="pk-scrim" @click.self="emit('close')">
    <div class="pk" role="dialog" :aria-label="title">
      <div class="card-h">
        <b>{{ title }}</b>
        <button class="btn quiet sm pk-x" aria-label="關閉" title="Esc" @click="emit('close')">✕</button>
      </div>

      <div class="pk-bar">
        <nav class="pk-crumbs">
          <button @click="open('')">電腦</button>
          <button v-for="c in crumbs" :key="c.path" @click="open(c.path)">{{ c.name }}</button>
        </nav>
        <button v-if="mode === 'register' && here" class="btn soft sm" :disabled="busy"
                @click="allUnregistered">選取所有未登記</button>
      </div>

      <div class="pk-list">
        <button v-if="parent" class="row-g pk-row pk-up" @click="open(parent)">
          <span></span><span>上一層</span>
        </button>
        <div v-for="e in [...dirs, ...files]" :key="e.path"
             :class="['row-g', 'pk-row', { on: chosen.includes(e.path), off: blocked(e) }]">
          <input :type="multi ? 'checkbox' : 'radio'" :checked="chosen.includes(e.path)"
                 :disabled="!!blocked(e)" @change="toggle(e)" />
          <button v-if="e.subdirs !== undefined" class="pk-name" @click="open(e.path)">{{ e.name }}</button>
          <span v-else class="pk-name pk-file" @click="toggle(e)">{{ e.name }}</span>
          <span class="pk-meta">
            <span v-if="blocked(e)" class="badge">{{ blocked(e) }}</span>
            <template v-if="e.subdirs !== undefined">
              <span v-if="e.subdirs">{{ n(e.subdirs) }} 個資料夾</span>
              <span v-if="e.images">{{ n(e.images) }} 個檔案</span>
            </template>
            <span v-else class="badge blue">影片</span>
          </span>
        </div>
        <p v-if="!busy && !dirs.length && !files.length" class="pk-none">沒有資料</p>
        <p v-if="busy && !dirs.length" class="pk-none">載入中</p>
      </div>

      <div class="pk-chosen" v-if="multi && chosen.length">
        <span v-for="p in chosen" :key="p" class="badge pk-chip" :title="p">
          {{ leaf(p) }}<button aria-label="移除" @click="chosen.splice(chosen.indexOf(p), 1)">✕</button>
        </span>
      </div>
      <slot />

      <div class="card-f pk-f">
        <span class="pk-count" v-if="multi">已選 {{ n(chosen.length) }} 項</span>
        <span class="pk-count lit-c" v-else>{{ chosen[0] || '' }}</span>
        <button class="btn quiet" @click="emit('close')">取消</button>
        <button class="btn primary" :disabled="!chosen.length"
                @click="emit('pick', [...chosen])">{{ submit }}</button>
      </div>
    </div>
  </div>
</template>

<style>
.admin .pk-scrim {
  position: fixed; inset: 0; z-index: 150; background: rgba(3, 7, 18, .72);
  display: flex; align-items: center; justify-content: center; padding: var(--s-8);
}
.admin .pk {
  width: min(760px, 100%); height: min(720px, 100%); display: flex; flex-direction: column;
  background: var(--surface); border: 1px solid var(--border-strong);
  border-radius: var(--r-lg); box-shadow: var(--shadow-pop); overflow: hidden;
}
.admin .pk-x { margin-left: auto; }
.admin .pk-bar {
  display: flex; align-items: center; gap: var(--s-2);
  padding: 8px 16px; border-bottom: 1px solid var(--border-soft);
}
.admin .pk-crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 2px; flex: 1; min-width: 0; font-size: var(--fs-sm); }
.admin .pk-crumbs button { color: var(--text2); padding: 2px 6px; border-radius: var(--r-sm); }
.admin .pk-crumbs button:hover { color: var(--accent2); background: var(--surface2); }
.admin .pk-crumbs button + button::before { content: '›'; margin-right: 8px; color: var(--text3); }
.admin .pk-crumbs button:last-child { color: var(--text); }
.admin .pk-list { flex: 1; min-height: 0; overflow-y: auto; }
.admin .pk-row { grid-template-columns: 16px minmax(0, 1fr) 16rem; min-height: 38px; padding: 4px 16px; width: 100%; text-align: left; }
.admin .pk-up { color: var(--text2); }
.admin .pk-name { text-align: left; font-size: var(--fs-md); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.admin button.pk-name:hover { color: var(--accent2); }
.admin .pk-file { cursor: pointer; }
.admin .pk-row.off .pk-name { color: var(--text3); }
.admin .pk-meta { display: flex; align-items: center; justify-content: flex-end; gap: var(--s-3); font-size: var(--fs-xs); color: var(--text3); font-variant-numeric: tabular-nums; }
.admin .pk-row input[type=radio] { width: 16px; height: 16px; padding: 0; accent-color: var(--accent); box-shadow: none; }
.admin .pk-none { padding: var(--s-8); text-align: center; color: var(--text3); font-size: var(--fs-sm); }
.admin .pk-chosen {
  display: flex; flex-wrap: wrap; gap: 6px; max-height: 6.5rem; overflow-y: auto;
  padding: 10px 16px; border-top: 1px solid var(--border-soft);
}
.admin .pk-chip { gap: 4px; padding-right: 4px; }
.admin .pk-chip button { color: var(--text3); font-size: 10px; padding: 0 4px; }
.admin .pk-chip button:hover { color: var(--red); }
.admin .pk-f { display: flex; align-items: center; gap: var(--s-2); border-top: 1px solid var(--border-soft); }
.admin .pk-count { margin-right: auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
