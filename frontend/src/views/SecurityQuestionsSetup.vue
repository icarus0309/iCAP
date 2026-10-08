<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import AuthLayout from '../components/AuthLayout.vue'
import SecurityQuestionsFields from '../components/SecurityQuestionsFields.vue'
import { useAuth } from '../stores/auth'

const auth = useAuth()
const router = useRouter()
const questions = ref([
  { question: '', answer: '' },
  { question: '', answer: '' },
  { question: '', answer: '' },
])
const busy = ref(false)
const errorMessage = ref('')

function messageFrom(error) {
  const detail = error.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  if (Array.isArray(detail)) return detail.map((item) => item.msg).filter(Boolean).join('；')
  return '请求失败，请稍后重试'
}

async function submit() {
  if (busy.value) return
  errorMessage.value = ''
  const values = questions.value.map((item) => ({ question: item.question.trim(), answer: item.answer.trim() }))
  if (values.some((item) => !item.question || !item.answer)) { errorMessage.value = '请填写全部 3 个密保问题及答案'; return }
  if (new Set(values.map((item) => item.question)).size !== 3) { errorMessage.value = '3 个密保问题不能重复'; return }
  if (new Set(values.map((item) => item.answer.normalize('NFKC').toLocaleLowerCase())).size !== 3) { errorMessage.value = '3 个密保答案不能重复'; return }
  busy.value = true
  try {
    await auth.setupSecurityQuestions({ security_questions: values })
    await auth.refreshProfile()
    await router.replace('/password/change')
  } catch (error) { errorMessage.value = messageFrom(error) }
  finally { busy.value = false }
}
</script>

<template>
  <AuthLayout>
    <div class="eyebrow">ACCOUNT SECURITY</div>
    <h2>设置密保问题</h2>
    <p class="subtitle">已有账号可补充密保问题，用于以后修改或找回密码。</p>
    <el-alert v-if="auth.securityQuestionsConfigured" title="该账号已设置密保问题，无需重复设置。" type="info" :closable="false" class="auth-alert" show-icon />
    <template v-else>
      <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" class="auth-alert" show-icon />
      <el-form label-position="top" @submit.prevent="submit">
        <SecurityQuestionsFields v-model="questions" />
        <el-button type="primary" native-type="submit" size="large" class="auth-submit" :loading="busy">保存密保问题</el-button>
      </el-form>
    </template>
    <div class="auth-footer"><router-link to="/">返回工作台</router-link></div>
  </AuthLayout>
</template>

<style scoped>
.auth-alert { margin-bottom: 18px; }
.auth-submit { width: 100%; margin-top: 12px; }
.auth-footer { margin-top: 25px; text-align: center; font-size: 13px; }
.auth-footer a { color: var(--red); font-weight: 700; }
</style>
