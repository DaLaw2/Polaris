<script setup>
import Switch from '../admin/Switch.vue'

defineProps({
  profiles: { type: Array, default: () => [] },
  open: { type: Boolean, default: false },
})
const profile = defineModel('profile', { type: String, default: '' })
const force = defineModel('force', { type: Boolean, default: false })
</script>

<template>
  <details class="sopt" :open="open">
    <summary>進階</summary>
    <div class="sopt-b">
      <label>
        <span>模型設定檔</span>
        <select v-model="profile">
          <option v-for="p in profiles" :key="p.id" :value="p.name">
            {{ p.name }}{{ p.active ? '（使用中）' : '' }}
          </option>
        </select>
      </label>
      <label title="所有模型重新抽樣與評分，包括已有分數的作品">
        <span>強制重新掃描</span>
        <Switch v-model="force" />
      </label>
    </div>
  </details>
</template>

<style>
.admin .sopt { padding: 8px 16px; border-top: 1px solid var(--border-soft); font-size: var(--fs-sm); }
.admin .sopt summary { cursor: pointer; color: var(--text2); width: max-content; }
.admin .sopt summary:hover { color: var(--text); }
.admin .sopt-b { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s-5); padding-top: var(--s-2); }
.admin .sopt label { display: inline-flex; align-items: center; gap: var(--s-2); color: var(--text2); }
.admin .sopt select { min-width: 12rem; }
</style>
