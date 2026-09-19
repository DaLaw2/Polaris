<script setup>
import { ref } from 'vue'
import { api } from '../api'
import { useToast } from '../admin/toast'

const props = defineProps({ onChanged: { type: Function, default: () => {} } })
const toast = useToast()

const text = ref('')
const error = ref('')
const busy = ref(false)

async function pick(e) {
  const f = e.target.files[0]
  e.target.value = ''
  if (!f) return
  text.value = await f.text()
  error.value = ''
}

async function upload() {
  busy.value = true
  error.value = ''
  try {
    const r = await api('/api/model-definitions', { method: 'POST', body: { text: text.value } })
    toast.ok(r.created ? `已上傳「${r.name}」` : `已有相同的「${r.name}」`)
    props.onChanged(r.id)
  } catch (e) {
    if (e.code === 'definition_invalid') error.value = e.message
    else toast.bad(e)
  }
  busy.value = false
}
</script>

<template>
  <div class="card md-detail du">
    <div class="card-h">
      <b>上傳定義</b>
      <label class="btn sm du-file">選擇檔案<input type="file" accept=".toml" @change="pick" /></label>
    </div>
    <div class="md-scroll">
      <textarea v-model="text" :class="['du-text', { bad: error }]" spellcheck="false"
                placeholder="貼上 TOML" aria-label="定義內容" @input="error = ''"></textarea>
      <p v-if="error" class="du-err"><b>定義有誤</b>{{ error }}</p>
    </div>
    <div class="card-f du-f">
      <button class="btn primary sm" :disabled="busy || !text.trim()" @click="upload">上傳</button>
    </div>
  </div>
</template>

<style>
.admin .du-file { margin-left: auto; cursor: pointer; }
.admin .du-file input { display: none; }
.admin .du-text { flex: 1; min-height: 20rem; width: 100%; resize: none; font-family: var(--mono); font-size: var(--fs-xs); }
.admin .du-text.bad { border-color: var(--red); box-shadow: 0 0 0 2px var(--red-soft); }
.admin .du-err { margin: 0; padding: 8px 12px; border-radius: var(--r-md); background: var(--red-soft); color: var(--red); font-family: var(--mono); font-size: var(--fs-xs); white-space: pre-wrap; }
.admin .du-err b { margin-right: 8px; font-family: var(--type); }
.admin .du-f { display: flex; justify-content: flex-end; }
</style>
