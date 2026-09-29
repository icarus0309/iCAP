<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuth } from './stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuth()
const mobileNav = ref(false)
const groups = [
  { name: '工作台', links: [['/', '平台概览', '◫']] },
  { name: 'MAAS 服务', links: [['/models', '模型广场与选型', '◇'], ['/leaderboard', '模型榜单', '▥'], ['/deployments', '部署申请', '▣']] },
  { name: '数据与评测', links: [['/datasets', '数据集仓库', '▤'], ['/data-tools', '数据标注与治理', '▥'], ['/evaluations', '评测任务', '◷'], ['/reports', '评测报告', '▧']] },
  { name: '智能应用', links: [['/agents', 'Agent 工作台', '✧'], ['/school', 'AI School', '◈']] },
  { name: '管理', links: [['/operations', '运营与监控', '▦']] },
]
const isLogin = computed(() => route.path === '/login')
function signOut() { auth.logout(); router.push('/login') }
</script>

<template>
  <router-view v-if="isLogin" />
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
          <router-link v-for="[path, title, icon] in group.links" :key="path" :to="path" class="nav-link" :class="{ active: route.path === path }" @click="mobileNav = false">
            <span class="nav-icon">{{ icon }}</span><span>{{ title }}</span><span v-if="route.path === path" class="active-dot" />
          </router-link>
        </div>
      </div>
      <div class="sidebar-footer"><span class="signal" /> 系统运行中 <span class="sidebar-version">v0.1 demo</span></div>
    </aside>
    <div class="workspace">
      <header class="topbar">
        <div class="top-left"><button class="menu-button" @click="mobileNav = true">☰</button><span class="crumb">{{ route.meta.group || '工作台' }} <b>/</b> <strong>{{ route.meta.title }}</strong></span></div>
        <div class="top-right"><span class="demo-pill">演示环境</span><span class="avatar">{{ auth.username?.slice(0, 1).toUpperCase() || 'A' }}</span><span class="user-name">{{ auth.username || 'admin' }}</span><el-button text @click="signOut">退出</el-button></div>
      </header>
      <main class="page"><router-view /></main>
    </div>
  </div>
</template>
