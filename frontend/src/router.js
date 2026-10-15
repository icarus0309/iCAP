import { createRouter, createWebHistory } from 'vue-router'

/**
 * meta.parent 指向上级页面的路径，用于顶栏面包屑的「返回上级」箭头与上级标题跳转。
 * 只给二级页面的子页面声明（如 /evaluations/:id → /evaluations）；一级页面不声明，
 * 因此顶栏只显示「分组 / 页面」，不出现返回箭头。
 */
const routes = [
  { path: '/login', component: () => import('./views/Login.vue'), meta: { title: '登录' } },
  { path: '/', component: () => import('./views/Overview.vue'), meta: { title: '平台概览', group: '工作台' } },
  { path: '/models', component: () => import('./views/Models.vue'), meta: { title: '模型广场与选型', group: 'MaaS 服务' } },
  { path: '/leaderboard', component: () => import('./views/Leaderboard.vue'), meta: { title: '模型榜单', group: 'MaaS 服务' } },
  { path: '/deployments', component: () => import('./views/Deployments.vue'), meta: { title: '部署申请', group: 'MaaS 服务' } },
  { path: '/datasets', component: () => import('./views/Datasets.vue'), meta: { title: '数据集仓库', group: '数据与评测' } },
  { path: '/data-tools', component: () => import('./views/DataTools.vue'), meta: { title: '数据标注与治理', group: '数据与评测' } },
  { path: '/evaluations', component: () => import('./views/Evaluations.vue'), meta: { title: '评测任务', group: '数据与评测' } },
  { path: '/evaluations/:id', component: () => import('./views/EvaluationDetail.vue'),
    props: (route) => ({ id: route.params.id }),
    meta: { title: '评测详情', group: '数据与评测', parent: '/evaluations' } },
  { path: '/reports', component: () => import('./views/Reports.vue'), meta: { title: '评测报告', group: '数据与评测' } },
  { path: '/agents', component: () => import('./views/Agents.vue'), meta: { title: 'Agent 工作台', group: 'Agent 应用' } },
  { path: '/school', component: () => import('./views/School.vue'), meta: { title: 'AI School', group: 'AI School' } },
  { path: '/operations', component: () => import('./views/Operations.vue'), meta: { title: '运营与监控', group: '运营中心' } },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({ history: createWebHistory(), routes })
export { routes }
router.beforeEach((to) => {
  if (to.path !== '/login' && !localStorage.getItem('ic_token')) return '/login'
  if (to.path === '/login' && localStorage.getItem('ic_token')) return '/'
  document.title = `${to.meta.title} · InnovationCore AI`
})
export default router
