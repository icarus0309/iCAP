<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import AuthLayout from '../components/AuthLayout.vue'
import { useAuth } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuth()
const isChange = computed(() => route.path === '/password/change')
const username = ref(isChange.value ? auth.username : typeof route.query.username === 'string' ? route.query.username : '')
const questions = ref([])
const answers = reactive({})
const loadedFor = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const busy = ref(false)
const loadingQuestions = ref(false)
const success = ref(false)
const errorMessage = ref('')
const ready = computed(() => questions.value.length === 3 && loadedFor.value === username.value.trim())

function messageFrom(error) {
  const detail = error.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  if (Array.isArray(detail)) return detail.map((item) => item.msg).filter(Boolean).join('；')
  return '请求失败，请稍后重试'
}

async function loadQuestions() {
  const value = username.value.trim()
  errorMessage.value = ''
  loadedFor.value = ''
  questions.value = []
  if (!value) { errorMessage.value = '请输入用户名'; return }
  loadingQuestions.value = true
  try {
    const result = await auth.getSecurityQuestions(value)
    if (username.value.trim() !== value) return
    questions.value = Array.isArray(result.questions) ? result.questions : []
    if (questions.value.length !== 3) {
      errorMessage.value = '该用户名暂无可用的密保问题，请确认用户名。已有账号可登录后先设置密保问题。'
      return
    }
    for (const key of Object.keys(answers)) delete answers[key]
    loadedFor.value = value
  } catch (error) { errorMessage.value = messageFrom(error) }
  finally { loadingQuestions.value = false }
}

async function submit() {
  if (busy.value) return
  errorMessage.value = ''
  if (!ready.value) { errorMessage.value = '请先获取密保问题'; return }
  const supplied = questions.value
    .map((item) => ({ index: item.index, answer: (answers[item.index] || '').trim() }))
    .filter((item) => item.answer)
  if (supplied.length < 2) { errorMessage.value = '请回答至少 2 个密保问题'; return }
  if (newPassword.value.length < 8 || newPassword.value.length > 128) { errorMessage.value = '新密码长度需为 8 至 128 个字符'; return }
  if (newPassword.value !== confirmPassword.value) { errorMessage.value = '两次输入的新密码不一致'; return }
  busy.value = true
  try {
    const payload = { new_password: newPassword.value, answers: supplied }
    if (isChange.value) await auth.changePassword(payload)
    else await auth.resetPassword({ username: username.value.trim(), ...payload })
    if (auth.token) await auth.logout()
    if (isChange.value) {
      ElMessage.success('密码已修改，请重新登录')
      await router.replace('/login')
    } else success.value = true
  } catch (error) { errorMessage.value = messageFrom(error) }
  finally { busy.value = false }
}

onMounted(() => { if (username.value.trim()) loadQuestions() })
</script>

<template>
  <AuthLayout>
    <div class="eyebrow">ACCOUNT SECURITY</div>
    <h2>{{ isChange ? '修改密码' : '找回密码' }}</h2>
    <p class="subtitle">答对 3 个密保问题中的至少 2 个，即可设置新密码。</p>
    <el-alert v-if="success" title="密码已重置，请使用新密码登录。" type="success" :closable="false" class="auth-alert" show-icon />
    <template v-if="!success">
      <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" class="auth-alert" show-icon />
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="用户名">
          <el-input v-model="username" :readonly="isChange" autocomplete="username" placeholder="请输入注册时的用户名" size="large" />
        </el-form-item>
        <el-button v-if="!isChange" :loading="loadingQuestions" class="lookup-button" @click="loadQuestions">获取密保问题</el-button>
        <template v-if="ready">
          <div class="section-heading">回答密保问题</div>
          <p class="question-note">任选至少 2 题作答。答案需与注册时填写的一致。</p>
          <el-form-item v-for="item in questions" :key="item.index" :label="item.question">
            <el-input v-model="answers[item.index]" autocomplete="off" type="password" show-password maxlength="128" placeholder="输入此题的答案" size="large" />
          </el-form-item>
          <el-form-item label="新密码">
            <el-input v-model="newPassword" autocomplete="new-password" type="password" show-password placeholder="8 至 128 个字符" size="large" />
          </el-form-item>
          <el-form-item label="确认新密码">
            <el-input v-model="confirmPassword" autocomplete="new-password" type="password" show-password placeholder="请再次输入新密码" size="large" />
          </el-form-item>
          <el-button type="primary" native-type="submit" size="large" class="auth-submit" :loading="busy">{{ isChange ? '确认修改' : '重置密码' }}</el-button>
        </template>
        <router-link v-else-if="isChange" class="setup-link" to="/security-questions/setup">尚未设置密保问题？先去设置</router-link>
      </el-form>
    </template>
    <div class="auth-footer"><router-link :to="auth.token ? '/' : '/login'">{{ auth.token ? '返回工作台' : '返回登录' }}</router-link></div>
  </AuthLayout>
</template>

<style scoped>
.auth-alert { margin-bottom: 18px; }
.lookup-button { width: 100%; margin: 0 0 18px; }
.section-heading { font-weight: 750; margin: 8px 0; }
.question-note { color: #748095; font-size: 12px; line-height: 1.6; margin: 0 0 18px; }
.auth-submit { width: 100%; margin-top: 12px; }
.setup-link { display: inline-block; margin-top: 12px; }
.auth-footer { margin-top: 25px; text-align: center; color: #748095; font-size: 13px; }
.setup-link, .auth-footer a { color: var(--red); font-weight: 700; }
</style>
