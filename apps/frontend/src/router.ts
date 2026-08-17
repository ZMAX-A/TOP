import { createRouter, createWebHistory } from 'vue-router'

import { sessionToken } from '@/api/client'
import { auth } from '@/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      redirect: '/projects',
    },
    {
      path: '/projects',
      name: 'projects',
      component: () => import('@/views/ProjectsView.vue'),
    },
    {
      path: '/changes',
      name: 'changes',
      component: () => import('@/views/ChangesInboxView.vue'),
    },
    {
      path: '/admin',
      name: 'admin',
      component: () => import('@/views/AdminView.vue'),
      meta: { systemAdmin: true },
    },
    {
      path: '/projects/:projectId',
      name: 'project',
      component: () => import('@/views/ProjectView.vue'),
    },
    {
      path: '/projects/:projectId/changes/:requestId',
      name: 'change-detail',
      component: () => import('@/views/ChangeDetailView.vue'),
    },
    {
      path: '/projects/:projectId/settings',
      name: 'project-settings',
      component: () => import('@/views/ProjectSettingsView.vue'),
    },
    {
      path: '/projects/:projectId/runs',
      name: 'project-runs',
      component: () => import('@/views/RunOperationsView.vue'),
    },
    {
      path: '/projects/:projectId/quality',
      name: 'project-quality',
      component: () => import('@/views/QualityAnalyticsView.vue'),
    },
    {
      path: '/projects/:projectId/runs/:runId',
      name: 'run-detail',
      component: () => import('@/views/RunDetailView.vue'),
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/projects',
    },
  ],
})

router.beforeEach(async (to) => {
  if (to.meta.public) {
    if (to.name === 'login' && sessionToken()) {
      const restored = auth.state.user !== null || (await auth.restore())
      if (restored) return { name: 'projects' }
    }
    return true
  }
  if (!sessionToken()) return { name: 'login', query: { redirect: to.fullPath } }
  if (!auth.state.user && !(await auth.restore())) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.meta.systemAdmin && auth.state.user?.system_role !== 'SYSTEM_ADMIN') {
    return { name: 'projects' }
  }
  return true
})

export default router
