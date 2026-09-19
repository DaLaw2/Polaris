<script setup>
import { n, pct } from '../admin/format'
import { left, rate } from './job'

defineProps({ job: { type: Object, required: true } })
</script>

<template>
  <span class="job-prog">
    <template v-if="job.total">
      <span :class="['track', { indet: job.state === 'queued', ok: job.state === 'done', bad: job.state === 'failed', warn: job.state === 'cancelled' }]">
        <i :style="{ width: pct(job.done, job.total) + '%' }"></i>
      </span>
      <span class="job-num">{{ n(job.done) }} / {{ n(job.total) }}</span>
    </template>
    <span v-else class="job-num">—</span>
    <span v-if="job.state === 'running'" class="job-rate">
      <template v-if="rate(job)">{{ rate(job).toFixed(1) }} 部/分鐘 · 剩餘 {{ left(job) }}</template>
      <template v-else>估算中</template>
    </span>
  </span>
</template>

<style>
.admin .job-prog { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; column-gap: var(--s-3); }
.admin .job-num { font-size: var(--fs-xs); color: var(--text2); font-variant-numeric: tabular-nums; text-align: right; }
.admin .job-rate { grid-column: 1 / -1; font-size: var(--fs-xs); color: var(--text3); font-variant-numeric: tabular-nums; }
</style>
