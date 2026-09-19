<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from './api'

const props = defineProps({
  path: { type: String, required: true },
  title: { type: String, default: '' },
})
const emit = defineEmits(['close'])

const info = ref(null)
const err = ref('')
const page = ref(0)
const grid = ref(false)
const bars = ref(true)
const scroller = ref(null)
const shell = ref(null)

const PREFS = 'polaris:reader'
const prefs = ref({ mode: 'single', fit: 'best', rtl: false, height: 100 })
try { Object.assign(prefs.value, JSON.parse(localStorage.getItem(PREFS) || '{}')) } catch {}
watch(prefs, v => {
  try { localStorage.setItem(PREFS, JSON.stringify(v)) } catch {}
}, { deep: true })

const mark = () => `polaris:at:${props.path}`
function remember(v) {
  try { localStorage.setItem(mark(), String(v)) } catch {}
}

const total = computed(() => info.value?.total || 0)
const isVideo = computed(() => info.value?.kind === 'video')
const src = i => `/api/work/page?path=${encodeURIComponent(props.path)}&i=${i}`
const thumb = i => `${src(i)}&w=200`

const spread = computed(() => {
  if (prefs.value.mode !== 'spread' || page.value === 0) return [page.value]
  const left = page.value % 2 ? page.value : page.value - 1
  return [left, left + 1].filter(i => i < total.value)
})
const shown = computed(() => prefs.value.rtl ? [...spread.value].reverse() : spread.value)

function go(delta) {
  const step = prefs.value.mode === 'spread' && page.value !== 0 ? 2 * delta : delta
  const next = Math.min(Math.max(page.value + step, 0), Math.max(total.value - 1, 0))
  page.value = next
}

function jump(i) {
  page.value = Math.min(Math.max(i, 0), Math.max(total.value - 1, 0))
  grid.value = false
  if (prefs.value.mode === 'scroll') nextTick(scrollToPage)
}

function scrollToPage() {
  scroller.value?.querySelector(`[data-i="${page.value}"]`)
    ?.scrollIntoView({ block: 'start' })
}

let ahead = []
watch([page, total], ([v, n]) => {
  if (!n) return
  remember(v)
  ahead = [v + 1, v + 2, v + 3, v - 1]
    .filter(i => i >= 0 && i < n)
    .map(i => Object.assign(new Image(), { src: src(i) }))
}, { immediate: true })

function tap(e) {
  if (prefs.value.mode === 'scroll' || isVideo.value) return
  const x = (e.clientX - e.currentTarget.getBoundingClientRect().left)
          / e.currentTarget.clientWidth
  if (x > 0.35 && x < 0.65) { bars.value = !bars.value; return }
  go((x < 0.5) === prefs.value.rtl ? 1 : -1)
}

const stage = ref(null)
let glide = null
function nudge(dir) {
  const el = prefs.value.mode === 'scroll' ? scroller.value : stage.value
  if (!el || glide) return
  const g = glide = { dir, moved: 0, at: performance.now(), el }
  const step = now => {
    if (glide !== g) return
    const d = dir * el.clientHeight * 1.2 * (now - g.at) / 1000
    g.at = now
    el.scrollTop += d
    g.moved += Math.abs(d)
    requestAnimationFrame(step)
  }
  requestAnimationFrame(step)
}
function stopGlide() {
  const g = glide
  glide = null
  const rest = g && g.el.clientHeight * 0.15 - g.moved
  if (rest > 0) g.el.scrollBy({ top: g.dir * rest, behavior: 'smooth' })
}
function onKeyUp(e) {
  if (e.key === 'ArrowUp' || e.key === 'ArrowDown') stopGlide()
}

watch(page, () => {
  if (prefs.value.mode !== 'scroll') stage.value?.scrollTo({ top: 0 })
})

const KEYS = {
  ArrowRight: () => go(prefs.value.rtl ? -1 : 1),
  ArrowLeft: () => go(prefs.value.rtl ? 1 : -1),
  ArrowDown: () => nudge(1),
  ArrowUp: () => nudge(-1),
  PageDown: () => go(1),
  PageUp: () => go(-1),
  Home: () => jump(0),
  End: () => jump(total.value - 1),
  g: () => { grid.value = !grid.value },
  f: () => fullscreen(),
  h: () => { bars.value = !bars.value },
  1: () => { prefs.value.mode = 'single' },
  2: () => { prefs.value.mode = 'spread' },
  3: () => { prefs.value.mode = 'scroll' },
  r: () => { prefs.value.rtl = !prefs.value.rtl },
}

function onKey(e) {
  if (e.target.tagName === 'INPUT') return
  if (e.key === 'Escape') {
    grid.value ? (grid.value = false) : emit('close')
    return
  }
  if (e.key === ' ' && !isVideo.value) {
    e.preventDefault()
    go(e.shiftKey ? -1 : 1)
    return
  }
  const fn = KEYS[e.key]
  if (e.repeat && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) { e.preventDefault(); return }
  if (fn && !isVideo.value) { e.preventDefault(); fn() }
  else if (fn && ['f', 'Escape'].includes(e.key)) fn()
}

async function fullscreen() {
  if (document.fullscreenElement) await document.exitFullscreen()
  else await shell.value?.requestFullscreen().catch(() => {})
}

const video = ref(null)
const last = ref(Number(localStorage.getItem(mark())) || 0)
const lastLabel = computed(() => isVideo.value
  ? `接續 ${Math.floor(last.value / 60)}:${String(last.value % 60).padStart(2, '0')}`
  : `接續第 ${last.value + 1} 頁`)
function resume() {
  if (isVideo.value) { if (video.value) video.value.currentTime = last.value }
  else jump(last.value)
  last.value = 0
}

const openHere = () => api('/api/open', { method: 'POST', body: { path: props.path } })
  .catch(e => { err.value = e.message })

let watcher = null
function observe() {
  watcher?.disconnect()
  if (prefs.value.mode !== 'scroll' || !scroller.value) return
  watcher = new IntersectionObserver(entries => {
    for (const e of entries) {
      if (e.isIntersecting) page.value = Number(e.target.dataset.i)
    }
  }, { root: scroller.value, threshold: 0.5 })
  for (const el of scroller.value.querySelectorAll('[data-i]')) watcher.observe(el)
}
watch(() => [prefs.value.mode, total.value], async () => {
  await nextTick()
  observe()
  if (prefs.value.mode === 'scroll') scrollToPage()
})

onMounted(async () => {
  window.addEventListener('keydown', onKey)
  window.addEventListener('keyup', onKeyUp)
  window.addEventListener('blur', stopGlide)
  try {
    info.value = await api(`/api/work/pages?path=${encodeURIComponent(props.path)}`)
    if (!isVideo.value && last.value >= info.value.total) last.value = 0
  } catch (e) { err.value = e.message }
  await nextTick()
  observe()
  if (prefs.value.mode === 'scroll') scrollToPage()
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('keyup', onKeyUp)
  window.removeEventListener('blur', stopGlide)
  glide = null
  watcher?.disconnect()
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {})
})

const VOLUME = 'polaris:volume'
function videoVolume(e) {
  try {
    const v = JSON.parse(localStorage.getItem(VOLUME) || 'null')
    if (v) Object.assign(e.target, { volume: v.volume, muted: v.muted })
  } catch {}
}
function saveVolume(e) {
  try {
    localStorage.setItem(VOLUME, JSON.stringify({ volume: e.target.volume, muted: e.target.muted }))
  } catch {}
}
function videoReady(e) {
  if (last.value >= e.target.duration - 5) last.value = 0
}
let saved = 0
function videoTime(e) {
  const t = Math.floor(e.target.currentTime)
  if (t !== saved) { saved = t; remember(t) }
}
</script>

<template>
  <div class="reader" ref="shell" :class="{ bare: !bars }" @contextmenu.prevent>
    <header class="reader-bar" v-show="bars">
      <button class="reader-x" @click="emit('close')" title="關閉（Esc）">✕</button>
      <span class="reader-title">{{ title || info?.name }}</span>

      <template v-if="!isVideo && total">
        <span class="reader-count">{{ page + 1 }} / {{ total }}</span>
        <span class="reader-sep"></span>
        <div class="reader-modes">
          <button v-for="m in [['single', '單頁'], ['spread', '跨頁'], ['scroll', '捲動']]"
                  :key="m[0]" :class="{ on: prefs.mode === m[0] }"
                  @click="prefs.mode = m[0]">{{ m[1] }}</button>
        </div>
        <div class="reader-modes">
          <button v-for="f in [['best', '最佳'], ['height', '合高'], ['width', '合寬'], ['raw', '原尺寸']]"
                  :key="f[0]" :class="{ on: prefs.fit === f[0] }"
                  @click="prefs.fit = f[0]">{{ f[1] }}</button>
        </div>
        <label v-if="prefs.fit === 'height'" class="reader-h" title="圖片高度佔畫面的比例">
          <input type="range" min="50" max="200" step="5" v-model.number="prefs.height" />
          <span>{{ prefs.height }}%</span>
        </label>
        <button class="reader-tog" :class="{ on: prefs.rtl }"
                @click="prefs.rtl = !prefs.rtl" title="右開（R）">右開</button>
        <button class="reader-tog" @click="grid = !grid" title="頁面總覽（G）">總覽</button>
      </template>

      <span class="reader-sep"></span>
      <button v-if="info && last > 0" class="reader-tog" @click="resume" title="上次看到的位置">{{ lastLabel }}</button>
      <button class="reader-tog" @click="openHere" title="用系統的程式開啟">在桌面開啟</button>
      <button class="reader-tog" @click="fullscreen" title="全螢幕（F）">全螢幕</button>
    </header>

    <p class="reader-err" v-if="err">{{ err }}</p>

    <video v-if="isVideo" ref="video" class="reader-video" controls autoplay
           :src="src(0)" @loadstart="videoVolume" @volumechange="saveVolume"
           @loadedmetadata="videoReady" @timeupdate="videoTime"></video>

    <div v-else-if="prefs.mode !== 'scroll' && total" ref="stage" class="reader-stage"
         :class="['fit-' + prefs.fit]" :style="{ '--h': prefs.height + '%' }" @click="tap">
      <img v-for="i in shown" :key="i" :src="src(i)" :alt="`${i + 1}`" draggable="false" />
    </div>

    <div v-else-if="total" class="reader-scroll" ref="scroller" :class="['fit-' + prefs.fit]"
         :style="{ '--h': prefs.height + 'vh' }">
      <img v-for="i in total" :key="i - 1" :data-i="i - 1" :src="src(i - 1)"
           loading="lazy" :alt="`${i}`" />
    </div>

    <p class="reader-empty" v-else-if="info">這個作品裡沒有可以顯示的頁面。</p>

    <footer class="reader-foot" v-if="!isVideo && total" v-show="bars">
      <div class="reader-track" @click="jump(Math.round(
             ($event.clientX - $event.currentTarget.getBoundingClientRect().left)
             / $event.currentTarget.clientWidth * (total - 1)))">
        <i :style="{ width: (total > 1 ? page / (total - 1) * 100 : 100) + '%' }"></i>
      </div>
    </footer>

    <div class="reader-grid" v-if="grid" @click.self="grid = false">
      <button v-for="i in total" :key="i - 1" :class="{ on: i - 1 === page }"
              @click="jump(i - 1)">
        <img :src="thumb(i - 1)" loading="lazy" :alt="`${i}`" />
        <span>{{ i }}</span>
      </button>
    </div>
  </div>
</template>

<style>
.reader {
  position: fixed; inset: 0; z-index: 200;
  background: #05070c; color: var(--text); user-select: none;
  display: flex; flex-direction: column;
}
.reader img, .reader video { -webkit-user-drag: none; }
.reader-bar {
  display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap;
  padding: 0.5rem 0.85rem; background: rgba(7, 10, 17, 0.94);
  border-bottom: 1px solid var(--border); flex: none;
}
.reader-title {
  font-weight: 600; max-width: 34ch; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
.reader-count { font-variant-numeric: tabular-nums; color: var(--accent); font-weight: 600; }
.reader-sep { flex: 1; }
.reader-x { font-size: 1.1rem; padding: 0 0.35rem; color: var(--text2); }
.reader-x:hover { color: var(--text); }
.reader-modes { display: flex; border: 1px solid var(--border); border-radius: 7px; overflow: hidden; }
.reader-modes button { padding: 0.28rem 0.7rem; font-size: 0.8rem; color: var(--text2); }
.reader-modes button:hover { color: var(--text); background: var(--surface2); }
.reader-modes button.on { background: var(--accent-fill); color: var(--on-accent); font-weight: 600; }
.reader-tog {
  border: 1px solid var(--border); border-radius: 7px;
  padding: 0.28rem 0.7rem; font-size: 0.8rem; color: var(--text2);
}
.reader-tog:hover { color: var(--text); border-color: var(--accent); }
.reader-tog.on { color: var(--on-accent); background: var(--accent-fill); border-color: var(--accent-fill); font-weight: 600; }
.reader-h { display: flex; align-items: center; gap: 0.4rem; font-size: 0.8rem; color: var(--text2); font-variant-numeric: tabular-nums; }
.reader-h input { width: 7rem; accent-color: var(--accent); }
.reader-h span { min-width: 3.2em; }
.reader-err { padding: 0.6rem 0.9rem; color: var(--red); }
.reader-empty { margin: auto; color: var(--text2); }

.reader-stage {
  flex: 1; min-height: 0; display: flex; align-items: center;
  justify-content: center; gap: 0; overflow: auto; cursor: pointer;
}
.reader-stage.fit-best img { max-height: 100%; max-width: 100%; object-fit: contain; }
.reader-stage.fit-height { align-items: flex-start; justify-content: safe center; }
.reader-stage.fit-height img { height: var(--h); width: auto; max-width: none; margin: auto 0; }
.reader-stage.fit-width { align-items: flex-start; }
.reader-stage.fit-width img { width: 100%; height: auto; }
.reader-stage.fit-raw { align-items: flex-start; }
.reader-stage.fit-raw img { max-width: none; }
.reader-stage img { display: block; }

.reader-scroll {
  flex: 1; min-height: 0; overflow-y: auto;
  display: flex; flex-direction: column; align-items: center;
}
.reader-scroll img { display: block; max-width: 100%; }
.reader-scroll.fit-width img { width: 100%; max-width: 1200px; }
.reader-scroll.fit-best img { max-height: 100vh; width: auto; }
.reader-scroll.fit-height img { height: var(--h); width: auto; max-width: none; }

.reader-video { flex: 1; min-height: 0; width: 100%; background: #000; }

.reader-foot { flex: none; padding: 0.5rem 0.85rem 0.7rem; }
.reader-track {
  height: 6px; background: var(--surface2); border-radius: 3px;
  cursor: pointer; overflow: hidden;
}
.reader-track > i { display: block; height: 100%; background: var(--accent); transition: width .15s; }

.reader-grid {
  position: absolute; inset: 0; z-index: 10; overflow-y: auto;
  background: rgba(5, 7, 12, 0.97); padding: 1rem;
  display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
  gap: 0.6rem; align-content: start;
}
.reader-grid button {
  display: flex; flex-direction: column; gap: 0.2rem; align-items: center;
  border: 1px solid transparent; border-radius: 6px; padding: 0.2rem;
}
.reader-grid button:hover { border-color: var(--border); }
.reader-grid button.on { border-color: var(--accent); }
.reader-grid img { width: 100%; aspect-ratio: 3/4; object-fit: cover; border-radius: 4px; }
.reader-grid span { font-size: 0.7rem; color: var(--text2); font-variant-numeric: tabular-nums; }
.reader-grid button.on span { color: var(--accent); }

.reader.bare .reader-stage { background: #05070c; }

@media (prefers-reduced-motion: reduce) {
  .reader-track > i { transition: none; }
}
</style>
