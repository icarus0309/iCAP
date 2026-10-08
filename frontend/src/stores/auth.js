import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../api'

export const useAuth = defineStore('auth', () => {
  const token = ref(localStorage.getItem('ic_token') || '')
  const username = ref(localStorage.getItem('ic_user') || '')
  const userId = ref(localStorage.getItem('ic_user_id') || '')
  const role = ref(localStorage.getItem('ic_role') || '')
  const securityQuestionsConfigured = ref(localStorage.getItem('ic_has_security_questions') === 'true')
  function saveProfile(result) {
    username.value = result.username || result.email || result.phone || ''
    userId.value = result.id == null ? '' : String(result.id)
    role.value = result.role || ''
    if (typeof result.has_security_questions === 'boolean') {
      securityQuestionsConfigured.value = result.has_security_questions
      localStorage.setItem('ic_has_security_questions', String(result.has_security_questions))
    }
    localStorage.setItem('ic_user', username.value)
    if (userId.value) localStorage.setItem('ic_user_id', userId.value)
    else localStorage.removeItem('ic_user_id')
    if (role.value) localStorage.setItem('ic_role', role.value)
    else localStorage.removeItem('ic_role')
  }
  function saveSession(result) {
    token.value = result.access_token
    localStorage.setItem('ic_token', token.value)
    saveProfile(result)
  }
  async function login(credentials) {
    saveSession(await api.post('/auth/login', credentials, { publicAuth: true, silentError: true }))
  }
  async function register(details) {
    saveSession(await api.post('/auth/register', details, { publicAuth: true, silentError: true }))
  }
  function getSecurityQuestions(username) {
    return api.get('/auth/security-questions', { params: { username }, publicAuth: true, silentError: true })
  }
  function resetPassword(details) {
    return api.post('/auth/password/reset', details, { publicAuth: true, silentError: true })
  }
  function changePassword(details) {
    return api.post('/auth/password/change', details, { silentError: true })
  }
  async function setupSecurityQuestions(details) {
    const result = await api.put('/auth/security-questions', details, { silentError: true })
    securityQuestionsConfigured.value = true
    localStorage.setItem('ic_has_security_questions', 'true')
    return result
  }
  async function refreshProfile() {
    if (!token.value) return false
    try {
      saveProfile(await api.get('/auth/me', { silentError: true, skipAuthRedirect: true }))
      return true
    } catch (error) {
      if (error.response?.status === 401) {
        clearSession()
        return false
      }
      return true
    }
  }
  function clearSession() {
    token.value = ''
    username.value = ''
    userId.value = ''
    role.value = ''
    securityQuestionsConfigured.value = false
    localStorage.removeItem('ic_token')
    localStorage.removeItem('ic_user')
    localStorage.removeItem('ic_user_id')
    localStorage.removeItem('ic_role')
    localStorage.removeItem('ic_has_security_questions')
  }
  async function logout() {
    let revoked = true
    try {
      if (token.value) await api.post('/auth/logout', null, { silentError: true, skipAuthRedirect: true })
    } catch { revoked = false }
    finally { clearSession() }
    return revoked
  }
  return { token, username, userId, role, securityQuestionsConfigured, login, register, getSecurityQuestions, resetPassword, changePassword, setupSecurityQuestions, refreshProfile, logout }
})
