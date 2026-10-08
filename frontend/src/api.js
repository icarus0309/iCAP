import axios from 'axios'
import { ElMessage } from 'element-plus'

export const base = import.meta.env.VITE_API_BASE || '/api'
export const api = axios.create({ baseURL: base, timeout: 30000 })

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('ic_token')
  if (token && !config.publicAuth) config.headers.Authorization = `Bearer ${token}`
  return config
})
api.interceptors.response.use((response) => response.data, (error) => {
  const detail = error.response?.data?.detail
  if (!error.config?.silentError) ElMessage.error(typeof detail === 'string' ? detail : '请求失败，请检查后端服务')
  const onAuthPage = ['/login', '/register', '/password/reset'].includes(location.pathname)
  if (error.response?.status === 401 && !onAuthPage && !error.config?.publicAuth && !error.config?.skipAuthRedirect) {
    localStorage.removeItem('ic_token')
    localStorage.removeItem('ic_user')
    localStorage.removeItem('ic_user_id')
    localStorage.removeItem('ic_role')
    location.assign(`/login?redirect=${encodeURIComponent(location.pathname + location.search)}`)
  }
  return Promise.reject(error)
})

// Fetch is used for authenticated SSE streams: EventSource cannot send a Bearer header.
export async function readSSE(path, body, onEvent, signal) {
  const response = await fetch(`${base}${path}`, {
    method: body == null ? 'GET' : 'POST',
    headers: { Authorization: `Bearer ${localStorage.getItem('ic_token')}`, ...(body == null ? {} : { 'Content-Type': 'application/json' }) },
    body: body == null ? undefined : JSON.stringify(body),
    signal,
  })
  if (!response.ok) throw new Error(`流式请求失败：HTTP ${response.status}`)
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, '\n')
      let boundary
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const packet = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const event = packet.split('\n').find((line) => line.startsWith('event:'))?.slice(6).trim() || 'message'
        const data = packet.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join('\n')
        if (data) onEvent(event, JSON.parse(data))
      }
    }
  } finally {
    reader.releaseLock()
  }
}

export async function downloadFile(url, filename) {
  const response = await fetch(`${base}${url}`, { headers: { Authorization: `Bearer ${localStorage.getItem('ic_token')}` } })
  if (!response.ok) throw new Error('文件下载失败')
  const href = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = href
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(href)
}
