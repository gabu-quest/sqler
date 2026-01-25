import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'dashboard',
      component: () => import('@/views/DashboardView.vue'),
      meta: { title: 'Dashboard' }
    },
    {
      path: '/locations',
      name: 'locations',
      component: () => import('@/views/LocationsView.vue'),
      meta: { title: 'Locations' }
    },
    {
      path: '/writers',
      name: 'writers',
      component: () => import('@/views/WritersView.vue'),
      meta: { title: 'Writers' }
    },
    {
      path: '/articles',
      name: 'articles',
      component: () => import('@/views/ArticlesView.vue'),
      meta: { title: 'Articles' }
    }
  ]
})

router.beforeEach((to) => {
  document.title = `${to.meta.title || 'SQLer'} - SQLer Demo`
})

export default router
