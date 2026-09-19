<script setup>
import { ref } from 'vue'
import { api } from '../api'
import EmptyState from '../admin/EmptyState.vue'
import { ROLE } from '../admin/format'
import { useToast } from '../admin/toast'

const props = defineProps({ profile: { type: Number, required: true } })
const toast = useToast()

const tag = ref('')
const rows = ref(null)
const asked = ref('')

const source = s =>
  s === 'official' ? '官方' : s === 'member' ? '模型設定' : s === 'score:default' ? '全域預設' : '類別預設'

async function ask() {
  const t = tag.value.trim()
  if (!t) return
  try {
    const r = await api(`/api/model-profiles/${props.profile}/explain?tag=${encodeURIComponent(t)}`)
    rows.value = r.rows.filter(x => x.category !== 'rating')
    asked.value = t
  } catch (e) { toast.bad(e) }
}
</script>

<template>
  <div class="card">
    <div class="card-h">
      <b>查詢標籤門檻</b>
      <input v-model="tag" class="ex-q" placeholder="標籤" @keydown.enter="ask" />
      <button class="btn sm" :disabled="!tag.trim()" @click="ask">查詢</button>
    </div>
    <template v-if="rows">
      <div class="row-g row-h ex-row">
        <span>模型</span><span>類別</span><span class="num-c">生效門檻</span><span>來源</span>
        <span>讓出</span><span>讓給</span><span class="num-c">頻率門檻</span>
      </div>
      <div v-for="r in rows" :key="`${r.backend}:${r.category}`"
           :class="['row-g', 'ex-row', { 'ex-mute': !r.emits }]"
           :title="r.emits ? '' : `${r.backend} 的${ROLE[r.category]}不輸出 ${asked}`">
        <span>{{ r.backend }}</span>
        <span>{{ ROLE[r.category] }}</span>
        <span class="num-c lit-c">{{ r.threshold }}</span>
        <span>{{ source(r.source) }}</span>
        <span :class="{ 'ex-yes': r.skipped }">{{ r.skipped ? '是' : '否' }}</span>
        <span>{{ r.yielded_to.join('、') || '—' }}</span>
        <span class="num-c lit-c" :title="`${r.freq_source} · ${r.freqz_source} ${r.freqz}`">{{ r.freq }}</span>
      </div>
      <EmptyState v-if="!rows.length" title="沒有資料" />
    </template>
  </div>
</template>

<style>
.admin .ex-q { margin-left: auto; width: 14rem; height: 30px; }
.admin .ex-row { grid-template-columns: minmax(8rem, 1.2fr) 5rem 6rem 6rem 4rem minmax(6rem, 1fr) 6rem; }
.admin .ex-mute { color: var(--text3); }
.admin .ex-yes { color: var(--orange); }
</style>
