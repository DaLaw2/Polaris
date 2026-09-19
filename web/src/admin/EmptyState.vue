<script setup>
defineProps({
  title: { type: String, required: true },
  text: { type: String, default: '' },
})
</script>

<template>
  <div class="empty">
    <b>{{ title }}</b>
    <p v-if="text">{{ text }}</p>
    <slot />
  </div>
</template>
