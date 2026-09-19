import { createRouter, createWebHistory } from 'vue-router'

import Shell from './admin/Shell.vue'
import ModelsView from './views/ModelsView.vue'
import OverviewView from './views/OverviewView.vue'
import ScanView from './views/ScanView.vue'
import SearchView from './views/SearchView.vue'
import VocabView from './views/VocabView.vue'
import WorksView from './views/WorksView.vue'

function oldConsole(to) {
  if (to.query.t === 'proc' || to.query.t === 'run') return { path: '/scan', query: { t: 'jobs' } }
}

function oldSettings(to) {
  if (to.query.t === 'models') return { path: '/models', query: {} }
  return { path: '/scan', query: to.query.t === 'params' ? { t: 'params' } : {} }
}

const OVERLAY = ['read', 'work']

function overlayOnly(a, b) {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)])
  return [...keys].every(k => OVERLAY.includes(k) || a[k] === b[k])
}

export default createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      component: Shell,
      children: [
        { path: '', name: 'overview', component: OverviewView, beforeEnter: oldConsole },
        { path: 'scan', name: 'scan', component: ScanView },
        { path: 'vocab', name: 'vocab', component: VocabView },
        { path: 'works', name: 'works', component: WorksView },
        { path: 'models', name: 'models', component: ModelsView },
        { path: 'settings', redirect: oldSettings },
      ],
    },
    { path: '/search', name: 'search', component: SearchView },
    { path: '/:rest(.*)', redirect: '/' },
  ],
  scrollBehavior(to, from, saved) {
    if (saved) return saved
    if (to.path === from.path && overlayOnly(to.query, from.query)) return false
    return { top: 0 }
  },
})
