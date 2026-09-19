<script setup>
defineProps({
  modelValue: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])
</script>

<template>
  <button type="button" class="sw" role="switch" :aria-checked="modelValue"
          :disabled="disabled" @click="emit('update:modelValue', !modelValue)"><i></i></button>
</template>
