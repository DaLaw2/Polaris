<script setup>
import { computed, ref, watch } from 'vue'
import { api } from '../api'
import EmptyState from '../admin/EmptyState.vue'
import Reader from '../Reader.vue'
import { useToast } from '../admin/toast'
import ClaimField from './ClaimField.vue'

const props = defineProps({
  id: { type: Number, required: true },
  onChanged: { type: Function, default: () => {} },
})
const emit = defineEmits(['close'])
const toast = useToast()
const watching = ref(false)
const noCover = ref(false)
const cover = path => `/api/cover?path=${encodeURIComponent(path)}&w=960`

async function openOnDesktop() {
  try { await api('/api/open', { method: 'POST', body: { path: work.value.folder_path } }) }
  catch (e) { toast.bad(e) }
}

const work = ref(null)
const missing = ref(false)
const ORDER = ['artist', 'character', 'series', 'tag', 'rating', 'color_mode', 'work_type', 'language']
const rank = f => (ORDER.includes(f) ? ORDER.indexOf(f) : ORDER.length)
const fields = computed(() => [...(work.value?.fields ?? [])]
  .sort((a, b) => rank(a.field) - rank(b.field) || a.field.localeCompare(b.field)))

watch(() => props.id, async id => {
  noCover.value = false
  watching.value = false
  try { work.value = await api(`/api/works/${id}/claims`) } catch (e) {
    missing.value = true
    toast.bad(e)
  }
}, { immediate: true })

function apply(w) {
  work.value = w
  props.onChanged(w)
  return w
}

const base = () => `/api/works/${props.id}/claims`
const ops = {
  post: body => api(base(), { method: 'POST', body }).then(apply),
  drop: claimId => api(`${base()}/${claimId}`, { method: 'DELETE' }).then(apply),
  clear: field => api(`${base()}?field=${encodeURIComponent(field)}`, { method: 'DELETE' }).then(apply),
}
</script>

<template>
  <div class="card md-detail wd">
    <div class="card-h">
      <b :title="work?.title">{{ work ? work.title : missing ? '沒有資料' : '載入中' }}</b>
      <button class="btn quiet sm wd-x" aria-label="關閉" @click="emit('close')">✕</button>
    </div>

    <div v-if="work" class="md-scroll">
      <div class="inset wd-where">
        <button v-if="!noCover" class="wd-cover" title="觀看" @click="watching = true">
          <img :src="cover(work.folder_path)" alt="" @error="noCover = true" />
        </button>
        <span class="lit-c wd-path">{{ work.folder_path }}</span>
        <span class="wd-coll">{{ work.collection }}</span>
        <div class="wd-acts">
          <button class="btn primary" @click="watching = true">觀看</button>
          <button class="btn" @click="openOnDesktop">在桌面開啟</button>
        </div>
      </div>
      <ClaimField v-for="f in fields" :key="f.field" :field="f" :ops="ops" />
    </div>
    <EmptyState v-else :title="missing ? '沒有資料' : '載入中'" />
    <Reader v-if="watching && work" :path="work.folder_path" :title="work.title" @close="watching = false" />
  </div>
</template>

<style>
.admin .wd .card-h b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.admin .wd-x { margin-left: auto; }
.admin .wd-where { display: flex; flex-direction: column; gap: var(--s-2); }
.admin .wd-cover { padding: 0; border-radius: var(--r-md); overflow: hidden; background: var(--surface); cursor: pointer; }
.admin .wd-cover img { display: block; width: 100%; max-height: 320px; object-fit: contain; }
.admin .wd-path { word-break: break-all; color: var(--text); }
.admin .wd-coll { font-size: var(--fs-sm); color: var(--text2); }
.admin .wd-acts { display: grid; grid-template-columns: 1fr 1fr; gap: var(--s-2); margin-top: var(--s-1); }
</style>
