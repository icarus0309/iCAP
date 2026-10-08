import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', component: () => import('./views/Login.vue'), meta: { title: '登录', public: true } },
  { path: '/register', component: () => import('./views/Register.vue'), meta: { title: '注册', public: true } },
  { path: '/password/reset', component: () => import('./views/PasswordSecurity.vue'), meta: { title: '找回密码', public: true } },
  { path: '/password/change', component: () => import('./views/PasswordSecurity.vue'), meta: { title: '修改密码', public: true, requiresAuth: true } },
  { path: '/security-questions/setup', component: () => import('./views/SecurityQuestionsSetup.vue'), meta: { title: '设置密保问题', public: true, requiresAuth: true } },
  { path: '/', component: () => import('./views/Overview.vue'), meta: { title: '平台概览', group: '总览' } },
  { path: '/models', component: () => import('./views/Models.vue'), meta: { title: '模型广场与选型', group: 'MaaS 服务' } },
  { path: '/leaderboard', component: () => import('./views/Leaderboard.vue'), meta: { title: '模型榜单', group: 'MaaS 服务' } },
  { path: '/deployments', component: () => import('./views/Deployments.vue'), meta: { title: '部署申请', group: 'MaaS 服务' } },
  { path: '/datasets', component: () => import('./views/Datasets.vue'), meta: { title: '数据集仓库', group: '数据与评测' } },
  { path: '/data-tools', component: () => import('./views/DataTools.vue'), meta: { title: '数据标注与治理', group: '数据与评测' } },
  { path: '/evaluations', component: () => import('./views/Evaluations.vue'), meta: { title: '评测任务', group: '数据与评测' } },
  { path: '/reports', component: () => import('./views/Reports.vue'), meta: { title: '评测报告', group: '数据与评测' } },
  { path: '/agents', component: () => import('./views/Agents.vue'), meta: { title: 'Agent 工作台', group: 'Agent 应用' } },
  { path: '/school', component: () => import('./views/School.vue'), meta: { title: 'AI School', group: 'AI School' } },
  { path: '/operations', component: () => import('./views/Operations.vue'), meta: { title: '运营与监控', group: '运营中心' } },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({ history: createWebHistory(), routes })
router.beforeEach((to) => {
  if ((!to.meta.public || to.meta.requiresAuth) && !localStorage.getItem('ic_token')) return { path: '/login', query: { redirect: to.fullPath } }
  if (to.meta.public && !to.meta.requiresAuth && localStorage.getItem('ic_token')) return '/'
  document.title = `${to.meta.title} · InnovationCore AI`
})
export default router
