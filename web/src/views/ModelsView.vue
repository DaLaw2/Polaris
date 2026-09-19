<script setup>
import Subtabs from '../admin/Subtabs.vue'
import Definitions from '../models/Definitions.vue'
import Profiles from '../models/Profiles.vue'

const TABS = [
  { key: 'profiles', label: '設定檔' },
  { key: 'defs', label: '模型定義' },
]
</script>

<template>
  <Subtabs :tabs="TABS" v-slot="{ tab }">
    <Profiles v-if="tab === 'profiles'" />
    <Definitions v-else />
  </Subtabs>
</template>
