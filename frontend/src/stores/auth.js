import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../api'

export const useAuth = defineStore('auth', () => {
  const token = ref(localStorage.getItem('ic_token') || '')
  const username = ref(localStorage.getItem('ic_user') || '')
  async function login(credentials) {
    const result = await api.post('/auth/login', credentials)
    token.value = result.access_token
    username.value = result.username
    localStorage.setItem('ic_token', token.value)
    localStorage.setItem('ic_user', username.value)
  }
  function logout() {
    token.value = ''
    username.value = ''
    localStorage.removeItem('ic_token')
    localStorage.removeItem('ic_user')
  }
  return { token, username, login, logout }
})
