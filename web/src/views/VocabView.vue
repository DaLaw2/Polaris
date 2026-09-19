<script setup>
import Subtabs from '../admin/Subtabs.vue'
import Concepts from '../vocab/Concepts.vue'
import Tags from '../vocab/Tags.vue'

const TABS = [
  { key: 'concepts', label: '概念' },
  { key: 'tags', label: '標籤' },
]
</script>

<template>
  <Subtabs :tabs="TABS" v-slot="{ tab }">
    <Concepts v-if="tab === 'concepts'" />
    <Tags v-else />
  </Subtabs>
</template>
