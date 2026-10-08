<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AuthLayout from '../components/AuthLayout.vue'
import { useAuth } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuth()
const form = reactive({ username: '', password: '' })
const busy = ref(false)
const errorMessage = ref('')
const now = ref(Date.now())
const lockedUntil = ref(0)
const lockedUsername = ref('')
let ticker

const lockWait = computed(() => Math.max(0, Math.ceil((lockedUntil.value - now.value) / 1000)))
const lockLabel = computed(() => `${Math.floor(lockWait.value / 60)}:${String(lockWait.value % 60).padStart(2, '0')}`)
const activeLockWait = computed(() => form.username.trim() === lockedUsername.value ? lockWait.value : 0)

function messageFrom(error) {
  const detail = error.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  if (Array.isArray(detail)) return detail.map((item) => item.msg).filter(Boolean).join('；')
  return '请求失败，请稍后重试'
}

function destination() {
  const redirect = route.query.redirect
  return typeof redirect === 'string' && redirect.startsWith('/') && !redirect.startsWith('//') ? redirect : '/'
}

async function submit() {
  if (busy.value) return
  errorMessage.value = ''
  const username = form.username.trim()
  if (!username) { errorMessage.value = '请输入用户名'; return }
  if (!form.password) { errorMessage.value = '请输入密码'; return }
  if (activeLockWait.value) return
  busy.value = true
  try {
    await auth.login({ username, password: form.password })
    await router.replace(destination())
  } catch (error) {
    errorMessage.value = messageFrom(error)
    if (error.response?.status === 423) {
      const retry = error.response?.data?.retry_after || Number(error.response?.headers?.['retry-after']) || 600
      lockedUntil.value = Date.now() + retry * 1000
      lockedUsername.value = username
    }
  } finally { busy.value = false }
}

onMounted(() => { ticker = window.setInterval(() => { now.value = Date.now() }, 1000) })
onUnmounted(() => window.clearInterval(ticker))
</script>

<template>
  <AuthLayout>
    <div class="eyebrow">WELCOME BACK</div>
    <h2>登录平台</h2>
    <p class="subtitle">使用用户名和密码进入工作台</p>
    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" class="auth-alert" show-icon />
    <el-alert v-if="activeLockWait" :title="`登录已锁定，${lockLabel} 后可重试`" type="warning" :closable="false" class="auth-alert" show-icon />
    <el-form :model="form" label-position="top" @submit.prevent="submit">
      <el-form-item label="用户名">
        <el-input v-model="form.username" autocomplete="username" placeholder="请输入用户名" size="large" />
      </el-form-item>
      <el-form-item label="密码">
        <el-input v-model="form.password" autocomplete="current-password" placeholder="请输入密码" type="password" show-password size="large" />
      </el-form-item>
      <div class="password-link"><router-link :to="{ path: '/password/reset', query: form.username.trim() ? { username: form.username.trim() } : {} }">忘记密码？</router-link></div>
      <el-button type="primary" native-type="submit" size="large" class="auth-submit" :loading="busy" :disabled="activeLockWait > 0">进入工作台 →</el-button>
    </el-form>
    <div class="auth-footer">还没有账号？<router-link :to="{ path: '/register', query: route.query.redirect ? { redirect: route.query.redirect } : {} }">注册账号</router-link></div>
  </AuthLayout>
</template>

<style scoped>
.auth-alert { margin-bottom: 18px; }
.password-link { text-align: right; margin-top: -8px; font-size: 13px; }
.password-link a, .auth-footer a { color: var(--red); font-weight: 700; }
.auth-submit { width: 100%; margin-top: 22px; }
.auth-footer { margin-top: 25px; text-align: center; color: #748095; font-size: 13px; }
</style>
