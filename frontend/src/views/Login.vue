<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '../stores/auth'

const router = useRouter()
const auth = useAuth()
const form = reactive({ username: 'admin', password: 'demo1234' })
const busy = ref(false)
async function submit() {
  busy.value = true
  try { await auth.login(form); router.push('/') } finally { busy.value = false }
}
</script>
<template>
  <div class="login-shell">
    <div class="login-story">
      <div class="login-brand"><span class="brand-mark">I<span>·</span></span> InnovationCore AI</div>
      <div class="login-content"><div class="login-kicker">ENTERPRISE AI PLATFORM</div><h1>让每一次模型创新<br />都有迹可循。</h1><p>模型服务化 · 数据与评测 · Agent 应用 · AI 实训</p><div class="login-lines"><span>01 / 接入</span><span>02 / 评测</span><span>03 / 应用</span></div></div>
      <div class="login-bottom">INNOVATIONCORE / AI PLATFORM</div>
    </div>
    <div class="login-form-wrap"><div class="login-form"><div class="eyebrow">WELCOME BACK</div><h2>登录平台</h2><p class="subtitle">探索企业级 AI 创新工作台</p><el-form :model="form" label-position="top" @submit.prevent="submit"><el-form-item label="用户名"><el-input v-model="form.username" autocomplete="username" placeholder="请输入用户名" size="large" /></el-form-item><el-form-item label="密码"><el-input v-model="form.password" autocomplete="current-password" placeholder="请输入密码" type="password" show-password size="large" @keyup.enter="submit" /></el-form-item><el-button type="primary" size="large" class="login-button" :loading="busy" @click="submit">进入工作台 →</el-button></el-form><div class="login-hint">演示账号：admin / demo1234<br />真实系统请通过环境变量更换账号和签名密钥。</div></div></div>
  </div>
</template>
<style scoped>
.login-shell { display: grid; grid-template-columns: 54% 46%; min-height: 100vh; background: #fff; }
.login-story { background: #b9152a; color: white; padding: 43px 61px; display: flex; flex-direction: column; position: relative; overflow: hidden; }
.login-story::before { content: ''; position: absolute; width: 590px; height: 590px; border: 1px solid #fff3; border-radius: 50%; right: -235px; top: 15%; box-shadow: 0 0 0 100px #ffffff0a, 0 0 0 220px #ffffff08; }
.login-brand { font-size: 17px; font-weight: 750; display: flex; align-items: center; gap: 12px; z-index: 1; }
.login-brand .brand-mark { background: white; color: #bd1a2c; box-shadow: none; width: 34px; height: 34px; font-size: 19px; }
.login-brand .brand-mark span { color: #bd1a2c88; }
.login-content { margin: auto 0; z-index: 1; }
.login-kicker { font-size: 11px; letter-spacing: 3px; color: #ffc4ca; font-weight: 750; }
.login-content h1 { font-size: clamp(38px, 4vw, 62px); line-height: 1.18; letter-spacing: -2.2px; margin: 18px 0 22px; }
.login-content p { color: #ffd9de; font-size: 16px; }
.login-lines { display: flex; gap: 26px; border-top: 1px solid #ffffff55; margin-top: 72px; padding-top: 18px; font-size: 11px; letter-spacing: 1.4px; color: #ffe2e5; }
.login-bottom { z-index: 1; letter-spacing: 2px; font-size: 10px; color: #ffdee2; }
.login-form-wrap { display: grid; place-items: center; padding: 30px; }
.login-form { width: min(390px, 100%); }
.login-form h2 { font-size: 32px; margin-bottom: 8px; }
.login-form .subtitle { margin-bottom: 38px; }
.login-form :deep(.el-form-item) { margin-bottom: 22px; }
.login-form :deep(.el-form-item__label) { font-weight: 650; color: #3b465a; }
.login-button { width: 100%; margin-top: 14px; }
.login-hint { margin-top: 29px; background: #f7f8fa; border-radius: 8px; padding: 15px; color: #8993a2; font-size: 12px; line-height: 1.8; }
@media(max-width: 760px) { .login-shell { display: block; } .login-story { min-height: 210px; padding: 25px; } .login-content { margin: 30px 0 5px; } .login-content h1 { font-size: 30px; margin: 8px 0; } .login-content p { font-size: 12px; } .login-lines, .login-bottom { display: none; } .login-form-wrap { padding: 42px 25px; } }
</style>
