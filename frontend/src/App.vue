<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuth } from './stores/auth'
import { ElMessage } from 'element-plus'
import { routes } from './router'

const route = useRoute()
const router = useRouter()
const auth = useAuth()
const mobileNav = ref(false)
const groups = [
  { name: '工作台', links: [['/', '平台概览', '◫']] },
  { name: 'MAAS 服务', links: [['/models', '模型广场与选型', '◇'], ['/leaderboard', '模型榜单', '▥'], ['/deployments', '部署申请', '▣']] },
  { name: '数据与评测', links: [['/datasets', '数据集仓库', '▤'], ['/data-tools', '数据标注与治理', '▥'], ['/evaluations', '评测任务', '◷'], ['/reports', '评测报告', '▧']] },
  { name: '智能应用', links: [['/agents', 'Agent 工作台', '✧'], ['/skill-evolution', 'Skill 自进化', '⟳'], ['/school', 'AI School', '◈']] },
  { name: '管理', links: [['/operations', '运营与监控', '▦']] },
]
const isAuthPage = computed(() => route.meta.public === true)
const signingOut = ref(false)
onMounted(async () => {
  if (auth.token && !await auth.refreshProfile()) await router.replace('/login')
})
async function signOut() {
  if (signingOut.value) return
  signingOut.value = true
  try {
    const revoked = await auth.logout()
    await router.replace('/login')
    if (!revoked) ElMessage.warning('本机已退出，但服务器未确认注销，请稍后检查连接')
  } finally { signingOut.value = false }
}
function openAccountPage(path) { router.push(path) }

// ---- 顶栏面包屑的「返回上级」 ----
// meta.parent 指向更上一级页面的路径；未声明即无上级（仅工作台）。
const parentPath = computed(() => route.meta.parent || '')
const parentTitle = computed(() => {
  if (!parentPath.value) return ''
  const parent = routes.find((item) => item.path === parentPath.value)
  return parent?.meta?.title || '上一级'
})
function goUp() { if (parentPath.value) router.push(parentPath.value) }

// 详情页等子路由按前缀命中侧边导航的父级标签（'/' 不参与前缀匹配，否则会命中所有路径）。
function isActiveLink(path) {
  if (route.path === path) return true
  return path !== '/' && route.path.startsWith(`${path}/`)
}
</script>

<template>
  <router-view v-if="isAuthPage" />
  <div v-else class="shell">
    <div v-if="mobileNav" class="nav-shade" @click="mobileNav = false" />
    <aside class="sidebar" :class="{ open: mobileNav }">
      <router-link class="brand" to="/" @click="mobileNav = false">
        <div class="brand-mark">I<span>·</span></div>
        <div><strong>InnovationCore</strong><small>AI 创新平台</small></div>
      </router-link>
      <div class="nav-scroll">
        <div v-for="group in groups" :key="group.name" class="nav-group">
          <div class="nav-heading">{{ group.name }}</div>
          <router-link v-for="[path, title, icon] in group.links" :key="path" :to="path" class="nav-link" :class="{ active: isActiveLink(path) }" @click="mobileNav = false">
            <span class="nav-icon">{{ icon }}</span><span>{{ title }}</span><span v-if="isActiveLink(path)" class="active-dot" />
          </router-link>
        </div>
      </div>
      <div class="sidebar-footer"><span class="signal" /> 系统运行中 <span class="sidebar-version">v0.1</span></div>
    </aside>
    <div class="workspace">
      <header class="topbar">
        <div class="top-left">
          <button class="menu-button" @click="mobileNav = true">☰</button>
          <button v-if="parentPath" class="crumb-back" type="button" :title="`返回${parentTitle}`" :aria-label="`返回${parentTitle}`" @click="goUp">←</button>
          <span class="crumb" :class="{ 'crumb-nested': !!parentPath }">
            <button v-if="parentPath" class="crumb-up" type="button" @click="goUp">{{ route.meta.group || '工作台' }}</button>
            <span v-else>{{ route.meta.group || '工作台' }}</span>
            <b>/</b>
            <strong>{{ route.meta.title }}</strong>
          </span>
        </div>
        <div class="top-right"><span class="avatar">{{ auth.username?.slice(0, 1).toUpperCase() || 'A' }}</span><span class="user-name">{{ auth.username || 'admin' }}</span><el-dropdown v-if="auth.role !== 'admin'" @command="openAccountPage"><span class="account-menu">账户设置 ▾</span><template #dropdown><el-dropdown-menu><el-dropdown-item v-if="!auth.securityQuestionsConfigured" command="/security-questions/setup">设置密保</el-dropdown-item><el-dropdown-item command="/password/change">修改密码</el-dropdown-item></el-dropdown-menu></template></el-dropdown><el-button text :loading="signingOut" @click="signOut">退出</el-button></div>
      </header>
      <main class="page"><router-view /></main>
    </div>
  </div>
</template>
