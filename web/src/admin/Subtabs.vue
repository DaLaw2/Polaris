<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const props = defineProps({ tabs: { type: Array, required: true } })
const route = useRoute()
const router = useRouter()

const tab = computed(() =>
  props.tabs.some(t => t.key === route.query.t)
    ? route.query.t
    : props.tabs[0].key)

const go = k => router.replace({
  query: k === props.tabs[0].key ? {} : { t: k },
})
</script>

<template>
  <div class="subtabs">
    <button v-for="t in tabs" :key="t.key"
            :class="{ on: tab === t.key }" @click="go(t.key)">
      {{ t.label }}<i v-if="t.count != null">{{ t.count }}</i>
    </button>
  </div>
  <slot :tab="tab" />
</template>
