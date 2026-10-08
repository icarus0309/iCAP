<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AuthLayout from '../components/AuthLayout.vue'
import SecurityQuestionsFields from '../components/SecurityQuestionsFields.vue'
import { useAuth } from '../stores/auth'

const auth = useAuth()
const route = useRoute()
const router = useRouter()
const form = reactive({
  username: '', phone: '', password: '', confirmPassword: '',
  security_questions: [
    { question: '', answer: '' },
    { question: '', answer: '' },
    { question: '', answer: '' },
  ],
})
const busy = ref(false)
const errorMessage = ref('')
const phonePattern = /^1[3-9]\d{9}$/
const usernamePattern = /^[\p{Script=Han}A-Za-z0-9_]{3,32}$/u

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
  const phone = form.phone.trim()
  if (!usernamePattern.test(username)) { errorMessage.value = '用户名需为 3 至 32 个汉字、字母、数字或下划线'; return }
  if (!phonePattern.test(phone)) { errorMessage.value = '请输入有效的中国大陆手机号'; return }
  if (form.password.length < 8 || form.password.length > 128) { errorMessage.value = '密码长度需为 8 至 128 个字符'; return }
  if (form.password !== form.confirmPassword) { errorMessage.value = '两次输入的密码不一致'; return }
  const questions = form.security_questions.map((item) => ({ question: item.question.trim(), answer: item.answer.trim() }))
  if (questions.some((item) => !item.question || !item.answer)) { errorMessage.value = '请填写全部 3 个密保问题及答案'; return }
  if (new Set(questions.map((item) => item.question)).size !== 3) { errorMessage.value = '3 个密保问题不能重复'; return }
  if (new Set(questions.map((item) => item.answer.normalize('NFKC').toLocaleLowerCase())).size !== 3) { errorMessage.value = '3 个密保答案不能重复'; return }
  busy.value = true
  try {
    await auth.register({ username, phone, password: form.password, security_questions: questions })
    await router.replace(destination())
  } catch (error) { errorMessage.value = messageFrom(error) }
  finally { busy.value = false }
}
</script>

<template>
  <AuthLayout>
    <div class="eyebrow">CREATE ACCOUNT</div>
    <h2>注册账号</h2>
    <p class="subtitle">设置用户名、密码和密保问题后即可使用，无需短信验证</p>
    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" class="auth-alert" show-icon />
    <el-form :model="form" label-position="top" @submit.prevent="submit">
      <el-form-item label="用户名">
        <el-input v-model="form.username" autocomplete="username" maxlength="32" placeholder="3 至 32 个汉字、字母、数字或下划线" size="large" />
      </el-form-item>
      <el-form-item label="中国大陆手机号">
        <el-input v-model="form.phone" autocomplete="tel" inputmode="tel" maxlength="11" placeholder="仅用于账号资料，不发送验证码" size="large" />
      </el-form-item>
      <el-form-item label="设置密码">
        <el-input v-model="form.password" autocomplete="new-password" type="password" show-password placeholder="8 至 128 个字符" size="large" />
      </el-form-item>
      <el-form-item label="确认密码">
        <el-input v-model="form.confirmPassword" autocomplete="new-password" type="password" show-password placeholder="请再次输入密码" size="large" />
      </el-form-item>
      <SecurityQuestionsFields v-model="form.security_questions" />
      <el-button type="primary" native-type="submit" size="large" class="auth-submit" :loading="busy">完成注册 →</el-button>
    </el-form>
    <div class="auth-footer">已有账号？<router-link :to="{ path: '/login', query: route.query.redirect ? { redirect: route.query.redirect } : {} }">返回登录</router-link></div>
  </AuthLayout>
</template>

<style scoped>
.auth-alert { margin-bottom: 18px; }
.auth-submit { width: 100%; margin-top: 12px; }
.auth-footer { margin-top: 25px; text-align: center; color: #748095; font-size: 13px; }
.auth-footer a { color: var(--red); font-weight: 700; }
</style>
