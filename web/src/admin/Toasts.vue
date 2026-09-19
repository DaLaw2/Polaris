<script setup>
import { useToast } from './toast'

const { toasts, dismiss, fire } = useToast()
</script>

<template>
  <Teleport to="body">
    <TransitionGroup tag="div" name="toast" class="toasts">
      <div v-for="t in toasts" :key="t.id" :class="['toast', t.tone]" role="status">
        <i class="toast-dot"></i>
        <span class="toast-text">{{ t.text }}</span>
        <button v-if="t.action" class="toast-act" @click="fire(t)">{{ t.action.label }}</button>
        <button class="toast-x" aria-label="關閉" @click="dismiss(t.id)">✕</button>
      </div>
    </TransitionGroup>
  </Teleport>
</template>
