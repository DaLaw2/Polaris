<script setup>
import { RouterView } from 'vue-router'
</script>

<template>
  <RouterView />
</template>

<style>
:root {
  color-scheme: dark;

  --bg: #0b1120;
  --side: #0d1525;
  --surface: #111827;
  --surface-h: #1a2332;
  --surface2: #182234;
  --raised: #1e293b;

  --border: #1e293b;
  --border-soft: rgba(30, 41, 59, .5);
  --border-strong: #334155;
  --border-input: #475569;

  --text: #e2e8f0;
  --text2: #94a3b8;
  --text3: #7b8aa3;
  --text-on-accent: #ffffff;

  --accent: #3b82f6;
  --accent-fill: #2563eb;
  --accent-fill-h: #3b82f6;
  --accent2: #60a5fa;
  --accent-soft: rgba(59, 130, 246, .10);
  --accent-soft2: rgba(59, 130, 246, .16);
  --accent-ring: 0 0 0 2px rgba(59, 130, 246, .25);
  --on-accent: #ffffff;

  --green: #34d399;  --green-fill: #10b981;  --green-soft: rgba(16, 185, 129, .12);
  --orange: #fbbf24; --orange-fill: #f59e0b; --orange-soft: rgba(245, 158, 11, .12);
  --red: #f87171;    --red-fill: #dc2626;    --red-soft: rgba(239, 68, 68, .12);
  --pink: #a78bfa;   --pink-soft: rgba(139, 92, 246, .12);
  --slate-soft: rgba(100, 116, 139, .14);

  --type: 'Archivo', 'Noto Sans TC', 'Microsoft JhengHei', 'PingFang TC',
          system-ui, sans-serif;
  --mono: 'Cascadia Mono', 'JetBrains Mono', ui-monospace, Consolas,
          'Noto Sans TC', 'Microsoft JhengHei', monospace;

  --fs-xs: 12px;
  --fs-sm: 13px;
  --fs-md: 14px;
  --fs-lg: 16px;
  --fs-xl: 22px;
  --fs-stat: 28px;
  --lh: 1.55;

  --s-1: 4px;  --s-2: 8px;  --s-3: 12px;  --s-4: 16px;
  --s-5: 20px; --s-6: 24px; --s-8: 32px;  --s-10: 40px; --s-12: 48px;

  --r-sm: 4px;
  --r-md: 6px;
  --r-lg: 8px;
  --r-full: 999px;

  --shadow-pop: 0 10px 30px rgba(0, 0, 0, .45), 0 0 0 1px var(--border);

  --t-fast: 100ms ease;
  --t-base: 150ms ease;
  --t-slow: 250ms cubic-bezier(.2, .8, .2, 1);

  --side-w: 240px;
  --bar-h: 56px;
}

::selection { background: rgba(59, 130, 246, .3); }
::-webkit-scrollbar-thumb { background: rgba(148, 163, 184, .14); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(148, 163, 184, .28); }

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: var(--type);
  font-size: 14px;
  line-height: 1.5;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}

.num, .row, .pager, .facets, .chip { font-variant-numeric: tabular-nums; }

button { font: inherit; color: inherit; cursor: pointer; background: none; border: none; }
input, select {
  font: inherit;
  background: var(--surface2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.45rem 0.6rem;
  outline: none;
}
input:focus, select:focus { border-color: var(--accent); }

.app { max-width: 1680px; margin: 0 auto; padding: 1rem 1.25rem 3rem; }
.app img { user-select: none; -webkit-user-drag: none; }

.btn {
  border: 1px solid var(--border); border-radius: 7px; background: var(--surface2);
  padding: 0.42rem 0.95rem; font-size: 0.85rem; font-weight: 500; flex: none;
}
.btn:hover:enabled { border-color: var(--accent); color: var(--accent2); }
.btn.primary {
  background: var(--accent-fill); border-color: var(--accent-fill);
  color: var(--on-accent); font-weight: 600;
}
.btn.primary:hover:enabled {
  background: var(--accent-fill-h); border-color: var(--accent-fill-h); color: var(--on-accent);
}
.btn:disabled { opacity: 0.35; cursor: default; }

.go {
  text-decoration: none; color: var(--accent); border-bottom: 1px solid var(--border);
  padding-bottom: 0.15rem;
}
.go:hover { border-bottom-color: var(--accent); }
.chip.on { background: var(--accent-fill); border-color: var(--accent-fill); color: var(--on-accent); }

.app > header {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding: 0.5rem 0 1rem;
  position: sticky;
  top: 0;
  background: linear-gradient(var(--bg) 80%, transparent);
  z-index: 30;
}
.brand { display: flex; align-items: baseline; gap: 0.5rem; flex-shrink: 0; }
.brand h1 { font-size: 1.35rem; font-weight: 700; letter-spacing: -0.01em; }
.brand .sub { color: var(--text2); font-size: 0.8rem; }

.searchbox { position: relative; flex: 1; display: flex; gap: 0.5rem; max-width: 720px; }
.searchbox input { flex: 1; padding: 0.55rem 0.8rem; }
.searchbox .go {
  background: var(--accent-fill); color: var(--on-accent); border-radius: 8px;
  padding: 0 1rem; font-weight: 600;
}
.searchbox .go:disabled { opacity: 0.5; cursor: default; }

.ac {
  position: absolute; top: calc(100% + 4px); left: 0; right: 0;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; overflow: hidden; z-index: 50;
  box-shadow: 0 12px 32px rgba(0,0,0,0.5);
}
.ac-row {
  display: flex; align-items: baseline; gap: 0.6rem;
  padding: 0.45rem 0.75rem; cursor: pointer;
}
.ac-row.on { background: var(--accent-fill); color: var(--on-accent); }
.ac-en { color: var(--text2); font-size: 0.8rem; flex: 1; }
.ac-row.on .ac-en { color: rgba(255,255,255,.8); }
.ac-cat {
  font-size: 0.7rem; padding: 0 0.35rem; border-radius: 4px;
  border: 1px solid var(--border); color: var(--text2);
}
.ac-n {
  font-size: 0.75rem; color: var(--text2);
  font-variant-numeric: tabular-nums; min-width: 2.5em; text-align: right;
}
.ac-row.on .ac-cat, .ac-row.on .ac-n { color: rgba(255,255,255,.8); }

.head-actions { display: flex; gap: 0.5rem; margin-left: auto; }
.select { padding: 0.5rem 0.6rem; }
.icon {
  border: 1px solid var(--border); border-radius: 8px;
  padding: 0.45rem 0.7rem; font-size: 1rem;
}
.icon:hover, .icon.on { border-color: var(--accent); color: var(--accent2); }

.chip {
  border: 1px solid var(--border); border-radius: 999px;
  padding: 0.3rem 0.7rem; font-size: 0.82rem;
  background: var(--surface); transition: all 0.15s;
}
.chip:hover { border-color: var(--accent); }
.chip.active { background: var(--surface2); border-color: var(--accent); }
.chip.active.not { border-color: var(--red); color: var(--red); }
.chip.active.sim { border-color: var(--orange); color: var(--orange); }
.chip em { font-style: normal; opacity: 0.55; margin-left: 0.3rem; }
.chip.active em { font-style: normal; color: var(--text2); margin-left: 0.25rem; }
.chip.clear { border-color: transparent; color: var(--text2); }
.chip.clear:hover { color: var(--red); }

.quickbar, .chipbar {
  display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem;
  margin-bottom: 0.6rem;
}
.chipbar-label { color: var(--text2); font-size: 0.78rem; margin-right: 0.2rem; }
.quickbar .spacer { flex: 1; }
.mode-toggle {
  display: flex; border: 1px solid var(--border); border-radius: 999px; overflow: hidden;
  font-size: 0.78rem;
}
.mode-toggle span { padding: 0.28rem 0.7rem; cursor: pointer; color: var(--text2); }
.mode-toggle span.on { background: var(--accent-fill); color: var(--on-accent); }

.error {
  background: color-mix(in srgb, var(--red) 14%, transparent);
  border: 1px solid var(--red);
  border-radius: 8px; padding: 0.6rem 0.9rem; margin-bottom: 1rem; font-size: 0.9rem;
}

.layout { display: grid; grid-template-columns: 250px 1fr; gap: 1.5rem; align-items: start; }
@media (max-width: 1000px) { .layout { grid-template-columns: 1fr; } }

.facets {
  position: sticky; top: 4.5rem;
  max-height: calc(100vh - 6rem); overflow-y: auto;
  padding-right: 0.4rem;
}
.facets::-webkit-scrollbar { width: 6px; }
.facets::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

.facet-note {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 0.7rem; font-size: 0.8rem; color: var(--text2);
}
.facet { margin-bottom: 1.1rem; }
.facet h3 {
  display: flex; align-items: center; justify-content: space-between;
  font-size: 0.75rem; font-weight: 600; letter-spacing: 0.08em;
  color: var(--text2); text-transform: uppercase;
  margin-bottom: 0.4rem;
}
.facet .more { font-size: 0.7rem; color: var(--accent2); text-transform: none; letter-spacing: 0; }
.facet-input { width: 100%; font-size: 0.82rem; margin-bottom: 0.4rem; }

.facet-values { list-style: none; }
.facet-values li { display: flex; align-items: center; border-radius: 6px; }
.facet-values li:hover { background: var(--surface); }
.facet-values li.on { background: var(--surface2); }
.fv {
  flex: 1; display: flex; justify-content: space-between; gap: 0.5rem;
  padding: 0.28rem 0.45rem; font-size: 0.82rem; text-align: left; min-width: 0;
}
.fv-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
li.on .fv-label { color: var(--accent2); font-weight: 600; }
.fv-count { color: var(--text2); font-size: 0.72rem; flex-shrink: 0; font-variant-numeric: tabular-nums; }
.fv-not {
  opacity: 0; padding: 0 0.4rem; color: var(--text2); font-weight: 700;
}
.facet-values li:hover .fv-not { opacity: 1; }
.fv-not:hover { color: var(--red); }

.resbar { color: var(--text2); font-size: 0.85rem; margin-bottom: 0.7rem; }

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(215px, 1fr));
  gap: 1.15rem 1rem;
}
.card { cursor: pointer; display: flex; flex-direction: column; }
.cover {
  position: relative; aspect-ratio: 3 / 4; background: var(--surface2);
  border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
  transition: border-color 0.15s;
}
.card:hover .cover { border-color: var(--accent); }
.cover img { width: 100%; height: 100%; object-fit: cover; display: block; }
.cover .pages {
  position: absolute; top: 6px; right: 6px;
  background: rgba(0,0,0,0.7); border-radius: 4px;
  padding: 1px 5px; font-size: 0.7rem; color: #ddd;
}
.cover .score {
  position: absolute; top: 6px; left: 6px;
  background: var(--accent-fill); border-radius: 4px;
  padding: 1px 5px; font-size: 0.7rem; color: var(--on-accent); font-weight: 600;
}
.cover .hover {
  position: absolute; inset: auto 0 0 0;
  display: flex; gap: 0.3rem; padding: 0.4rem;
  background: linear-gradient(transparent, rgba(0,0,0,0.85));
  opacity: 0; transition: opacity 0.15s;
}
.card:hover .hover { opacity: 1; }
.cover .hover button {
  flex: 1; font-size: 0.72rem; padding: 0.25rem;
  background: rgba(255,255,255,0.12); border-radius: 5px;
}
.cover .hover button:hover { background: var(--accent-fill); color: var(--on-accent); }
.cover .hover button.lead {
  background: var(--accent-fill); color: var(--on-accent); font-weight: 600;
}

.body { padding: 0.55rem 0.1rem 0; display: flex; flex-direction: column; gap: 0.3rem; }
.title {
  font-size: 0.85rem; font-weight: 600; line-height: 1.3;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.meta { display: flex; flex-wrap: wrap; gap: 0.4rem; font-size: 0.74rem; }
.link { color: var(--accent2); text-align: left; }
.link:hover { text-decoration: underline; }
.link.dim { color: var(--text2); }
.badge.dim { opacity: 0.55; font-style: italic; }
.badges { display: flex; flex-wrap: wrap; gap: 0.25rem; }
.badge {
  font-size: 0.66rem; padding: 0.1rem 0.4rem; border-radius: 4px;
  background: var(--surface2); color: var(--text2);
}
.badge-color { background: color-mix(in srgb, var(--pink) 18%, transparent); color: var(--pink); }
.badge-gray { background: color-mix(in srgb, var(--text2) 18%, transparent); color: var(--text2); }
.badge-partial { background: color-mix(in srgb, var(--orange) 18%, transparent); color: var(--orange); }
.rating-explicit { background: color-mix(in srgb, var(--red) 18%, transparent); color: var(--red); }
.rating-questionable { background: color-mix(in srgb, var(--orange) 18%, transparent); color: var(--orange); }
.rating-sensitive { background: color-mix(in srgb, var(--green) 18%, transparent); color: var(--green); }
.rating-general { background: color-mix(in srgb, var(--green) 18%, transparent); color: var(--green); }
.badge-lang { background: color-mix(in srgb, var(--pink) 18%, transparent); color: var(--pink); }

.tags { display: flex; flex-wrap: wrap; gap: 0.2rem; }
.card .tags { gap: 0.1rem 0.5rem; }
.card .tag {
  background: none; padding: 0; border-radius: 0; font-size: 0.71rem;
  color: var(--text2);
}
.card .tag:hover { color: var(--accent2); }
.tag {
  font-size: 0.68rem; padding: 0.1rem 0.35rem; border-radius: 4px;
  background: var(--surface2); color: var(--text2);
}
.empty { padding: 4rem 1rem; text-align: center; color: var(--text2); }
.empty p { margin-bottom: 1rem; }

.pager { display: flex; justify-content: center; gap: 0.3rem; margin-top: 2rem; flex-wrap: wrap; }
.pager button {
  min-width: 2rem; padding: 0.35rem 0.55rem; border-radius: 7px;
  border: 1px solid var(--border); font-size: 0.82rem;
}
.pager button:hover:not(:disabled) { border-color: var(--accent); }
.pager button.on { background: var(--accent-fill); border-color: var(--accent-fill); color: var(--on-accent); }
.pager button:disabled { opacity: 0.35; cursor: default; }
.pager .gap { color: var(--text2); padding: 0.35rem 0.2rem; }

.drawer-scrim { position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 60; }
.drawer {
  position: fixed; top: 0; right: 0; bottom: 0; width: min(440px, 100vw);
  background: var(--surface); border-left: 1px solid var(--border);
  z-index: 70; display: flex; flex-direction: column;
  box-shadow: -8px 0 32px rgba(0,0,0,0.5);
}
.drawer-head {
  display: flex; align-items: flex-start; gap: 0.5rem;
  padding: 1rem; border-bottom: 1px solid var(--border);
}
.drawer-head h2 { font-size: 0.95rem; font-weight: 600; line-height: 1.4; flex: 1; word-break: break-all; }
.drawer-body { overflow-y: auto; padding: 1rem; display: flex; flex-direction: column; gap: 0.7rem; }
.drawer-cover { width: 100%; border-radius: 8px; max-height: 300px; object-fit: contain; background: var(--surface2); }

.row { display: flex; align-items: center; gap: 0.5rem; font-size: 0.85rem; flex-wrap: wrap; }
.row b { color: var(--text2); font-weight: 500; min-width: 3rem; font-size: 0.78rem; }
.muted { color: var(--text2); }
.mini { font-size: 0.75rem; padding: 0.15rem 0.35rem; border-radius: 5px; opacity: 0.6; }
.mini:hover:enabled { opacity: 1; background: var(--surface2); }
.mini:disabled, .mini.ok:disabled { opacity: 0.25; cursor: default; }
.mini.ok { color: var(--green); opacity: 1; }
.edit { font-size: 0.82rem; padding: 0.25rem 0.5rem; flex: 1; min-width: 8rem; }
.addrow { display: flex; gap: 0.35rem; margin: 0.35rem 0; }

.block { display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem; }
.block.err { color: var(--red); font-size: 0.82rem; }
.block.err .label { width: 100%; font-weight: 600; }
.block > b { color: var(--text2); font-weight: 500; font-size: 0.78rem; width: 100%; }
.cloud { display: flex; flex-wrap: wrap; gap: 0.25rem; width: 100%; }
.cloud .tag { cursor: pointer; }
.cloud .tag:hover { background: var(--accent-fill); color: var(--on-accent); }
.tag-char { background: var(--pink-soft); color: var(--pink); }
.tag-copy { background: var(--green-soft); color: var(--green); }
.tag-edit { display: inline-flex; align-items: center; gap: 0.1rem; padding: 0; }
.tag-edit.removed { opacity: 0.4; text-decoration: line-through; }
.tag-main { padding: 0.1rem 0.1rem 0.1rem 0.35rem; font-size: 0.68rem; }
.tag-x { padding: 0.1rem 0.3rem; font-size: 0.6rem; color: var(--text2); }
.tag-x:hover { color: var(--red); }

.overrides { width: 100%; display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.75rem; }
.ov-rm { color: var(--red); }
.ov-add { color: var(--green); }

.drawer-foot { margin-top: 0.5rem; display: flex; flex-direction: column; gap: 0.5rem; }
.wide {
  border: 1px solid var(--border); border-radius: 8px;
  padding: 0.5rem; font-size: 0.85rem;
}
.wide:hover { border-color: var(--accent); }
.path {
  font-size: 0.68rem; color: var(--text2); word-break: break-all;
  background: var(--bg); padding: 0.4rem; border-radius: 6px;
}

.loc {
  font-family: var(--mono);
  font-size: 0.76rem; word-break: break-all; color: var(--text);
}
.row .loc { color: var(--text2); }
</style>
