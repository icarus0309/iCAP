<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, readSSE } from '../api'
import EvalResultPanel from '../components/evaluation/EvalResultPanel.vue'
import CaseResultTable from '../components/evaluation/CaseResultTable.vue'
import AgentResultPanel from '../components/evaluation/AgentResultPanel.vue'
import { useAuth } from '../stores/auth'

const props = defineProps({ id: { type: String, required: true } })
const router = useRouter()
const auth = useAuth()
const task = ref(null)
const loading = ref(true)
const error = ref('')
const rulers = ref([])
let controller
let retry = 0
let timer

const names = { queued: '排队中', running: '运行中', paused: '已暂停', completed: '已完成', cancelled: '已取消', failed: '失败' }
const statusType = (status) => (status === 'completed' ? 'success' : ['cancelled', 'failed'].includes(status) ? 'danger' : 'warning')
const isActive = computed(() => ['queued', 'running', 'paused'].includes(task.value?.status))
const canManage = computed(() => auth.role === 'admin' || (task.value?.owner_user_id != null && String(task.value.owner_user_id) === auth.userId))
const result = computed(() => task.value?.result || null)
// 逐维度取样配置的展示文本；未配置时回落统一口径
const limitText = computed(() => {
  const config = task.value?.config
  if (!config) return ''
  const limits = config.sample_limits
  if (!limits || !Object.keys(limits).length) return `每维度取样 ${config.sample_limit} 条`
  const fromResult = Object.fromEntries((result.value?.dimensions || []).map((dim) => [dim.key, dim.label]))
  const fromSpec = Object.fromEntries((rulers.value.find((r) => r.id === task.value.benchmark)?.dimensions || [])
    .map((dim) => [dim.key, dim.label]))
  const labels = { ...fromSpec, ...fromResult }
  return `各维度取样 ${Object.entries(limits).map(([key, value]) => `${labels[key] || key} ${value}`).join('、')} 条`
})
const lastError = computed(() => (task.value?.logs || []).slice(-1)[0] || '任务执行失败')

async function fetchTask() {
  try {
    task.value = await api.get(`/evaluations/${props.id}`)
    error.value = ''
  } catch (err) {
    error.value = err?.response?.status === 404 ? '任务不存在或已被删除' : '加载任务失败'
  } finally {
    loading.value = false
  }
}

function connectSSE() {
  controller?.abort()
  controller = new AbortController()
  readSSE(`/evaluations/${props.id}/events`, null, (event, data) => {
    if (event === 'state') {
      task.value = data
      retry = 0
    }
  }, controller.signal)
    .then(() => { if (!controller?.signal.aborted) scheduleReconnect() })
    .catch((err) => { if (!controller?.signal.aborted && err.name !== 'AbortError') scheduleReconnect() })
}

function scheduleReconnect() {
  if (!task.value) return
  if (['completed', 'cancelled', 'failed'].includes(task.value.status)) return
  if (retry >= 5) return                       // 最多重连 5 次
  retry += 1
  timer = setTimeout(connectSSE, 3000 * retry) // 退避：3s/6s/9s/12s/15s
}

async function action(command) {
  const updated = await api.patch(`/evaluations/${props.id}`, { action: command })
  task.value = { ...task.value, ...updated }
  if (command === 'pause') ElMessage.success('已暂停，当前用例执行完后生效')
  if (command === 'cancel') { controller?.abort(); await fetchTask() }
}

function restart() {
  router.push({ path: '/evaluations', query: { create: '1', model: task.value?.model_id, dataset: task.value?.dataset_id, benchmark: task.value?.benchmark } })
}

onMounted(async () => {
  fetchTask()
  connectSSE()
  // 维度中文名来自尺子规格：任务未完成时也要能展示运行参数
  try {
    rulers.value = (await api.get('/rulers'))?.rulers || []
  } catch { rulers.value = [] }
})
onUnmounted(() => { controller?.abort(); clearTimeout(timer) })
</script>
<template>
  <div class="page-heading">
    <div>
      <div class="eyebrow">DATA / EVALUATION / DETAIL</div>
      <h1>
        {{ task ? `${task.ruler_label || (task.agent && task.agent.benchmark) || task.benchmark} · ${task.model_name}` : '评测详情' }}
      </h1>
      <p class="subtitle">
        <el-button text type="primary" size="small" @click="router.push('/evaluations')">‹ 返回列表</el-button>
        <span v-if="task">数据集 {{ task.dataset_name }} · 创建于 {{ task.created_at }}</span>
      </p>
    </div>
    <div v-if="task" class="flex-between" style="gap:12px">
      <el-tag :type="statusType(task.status)">{{ names[task.status] || task.status }}</el-tag>
      <span class="muted small">{{ task.progress }}%</span>
    </div>
  </div>

  <el-skeleton v-if="loading" :rows="4" animated />
  <el-empty v-else-if="error" :description="error" />

  <template v-else-if="task">
    <section class="card card-pad">
      <div class="card-header">
        <h2>执行进度</h2>
        <span class="muted small">SSE 实时更新（断连自动退避重连，最多 5 次）</span>
      </div>
      <el-progress :percentage="task.progress" :status="task.status === 'failed' ? 'exception' : undefined" />
      <div class="toolbar section-gap">
        <el-button v-if="canManage && !task.agent && ['queued', 'running'].includes(task.status)" @click="action('pause')">暂停</el-button>
        <el-button v-if="canManage && !task.agent && task.status === 'paused'" type="primary" @click="action('resume')">恢复</el-button>
        <el-button v-if="canManage && isActive" type="danger" plain @click="action('cancel')">取消任务</el-button>
        <el-button v-if="task.status === 'completed'" type="primary" @click="router.push('/reports')">查看报告</el-button>
        <el-button v-if="task.status === 'failed'" type="primary" @click="restart">重新发起</el-button>
      </div>
      <div class="notice" v-if="task.config">
        运行参数：{{ limitText }} · max_tokens {{ task.config.max_tokens }} · temperature {{ task.config.temperature }} ·
        榜单回写 {{ task.config.sync_scores ? '开启' : '关闭' }}
      </div>
      <div class="card-header" style="margin-top:18px"><h2>执行日志</h2><span class="muted small">{{ task.logs.length }} 行</span></div>
      <div class="log-box">{{ task.logs.join('\n') }}</div>
    </section>

    <el-alert v-if="task.status === 'failed'" class="section-gap" type="error" :closable="false"
              title="任务执行失败" :description="lastError" />
    <el-alert v-else-if="task.status === 'cancelled'" class="section-gap" type="info" :closable="false"
              title="任务已取消" description="取消的任务不生成评测报告，已产生的用例结果不保留。" />

    <template v-if="result">
      <AgentResultPanel v-if="task.agent" class="section-gap" :result="result" />
      <template v-else>
        <EvalResultPanel class="section-gap" :result="result" :ruler-label="task.ruler_label" />
        <CaseResultTable :cases="result.cases" />
      </template>
    </template>
    <div v-else-if="isActive" class="notice section-gap">
      任务完成后将在此展示评测结果。
    </div>
  </template>
</template>
