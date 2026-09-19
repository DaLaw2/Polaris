<script setup>
import { onUnmounted, ref } from 'vue'

const props = defineProps({
  label: { type: String, required: true },
  confirm: { type: String, default: '確定刪除？' },
  busyLabel: { type: String, default: '' },
  run: { type: Function, required: true },
  disabled: { type: Boolean, default: false },
})

const armed = ref(false)
const busy = ref(false)
let timer = null

function disarm() {
  clearTimeout(timer)
  armed.value = false
}

async function click() {
  if (!armed.value) {
    armed.value = true
    timer = setTimeout(disarm, 3000)
    return
  }
  disarm()
  busy.value = true
  try { await props.run() } finally { busy.value = false }
}

onUnmounted(() => clearTimeout(timer))
</script>

<template>
  <button type="button" :class="['btn', 'arm', { danger: armed, armed }]"
          :disabled="disabled || busy"
          @click="click" @blur="disarm" @keydown.esc="disarm">
    <span class="arm-w">
      <span :class="{ 'arm-off': armed || (busy && busyLabel) }">{{ label }}</span>
      <span :class="{ 'arm-off': !armed || busy }">{{ confirm }}</span>
      <span v-if="busyLabel" :class="{ 'arm-off': !busy }">{{ busyLabel }}</span>
    </span>
  </button>
</template>
