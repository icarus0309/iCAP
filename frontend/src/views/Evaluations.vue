<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import CreateEvaluationDialog from '../components/evaluation/CreateEvaluationDialog.vue'
import VisibilityTag from '../components/VisibilityTag.vue'
import { useAuth } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuth()
function canManage(row) { return auth.role === 'admin' || (row.owner_user_id != null && String(row.owner_user_id) === auth.userId) }
const rows = ref([]), models = ref([]), datasets = ref([]), rulers = ref([]), dialog = ref(false)
let timer
const names = { queued: '排队中', running: '运行中', paused: '已暂停', completed: '已完成', cancelled: '已取消', failed: '失败' }
const statusType = (status) => (status === 'completed' ? 'success' : ['cancelled', 'failed'].includes(status) ? 'danger' : 'warning')
const benchmarks = ['Ruler1', 'Ruler2', 'VLM', 'AISF', 'Dev', 'ASR', 'MT', 'TTS']

async function load() {
  const [tasks, modelRows, datasetRows, rulerResponse] = await Promise.all([
    api.get('/evaluations'), api.get('/models'), api.get('/datasets'), api.get('/rulers'),
  ])
  rows.value = tasks
  models.value = modelRows
  datasets.value = datasetRows
  // GET /api/rulers 返回 {rulers: [...]}，需取出数组
  rulers.value = rulerResponse?.rulers || []
}
function openDialog() { dialog.value = true }
function onCreated(created) {
  load()
  const task = Array.isArray(created) ? created[0] : created
  if (task?.id) router.push(`/evaluations/${task.id}`)
}
async function action(row, command) { await api.patch(`/evaluations/${row.id}`, { action: command }); await load() }
function open(row) { router.push(`/evaluations/${row.id}`) }

onMounted(async () => {
  await load()
  if (route.query.create) dialog.value = true
  timer = setInterval(load, 4500)
})
onUnmounted(() => clearInterval(timer))
</script>
<template>
  <div class="page-heading">
    <div>
      <div class="eyebrow">DATA / EVALUATION</div>
      <h1>评测任务</h1>
      <p class="subtitle">选择测评尺子发起任务，实时查看进度、维度得分与用例级明细。</p>
    </div>
    <el-button type="primary" @click="openDialog">+ 发起评测</el-button>
  </div>
  <div class="notice">
    尺子一（通用能力）与尺子二（通信领域）由内置测评引擎真实执行：25 条内置样例、按维度加权计分并生成报告。
    VLM / AISF / Dev / ASR / MT / TTS 仍为模拟执行，结果仅用于演示。
  </div>
  <section class="card card-pad">
    <div class="card-header"><h2>任务列表</h2><span class="muted small">{{ rows.length }} 个任务</span></div>
    <el-table :data="rows" empty-text="暂无任务，点击右上角发起评测">
      <el-table-column prop="model_name" label="模型" min-width="150" />
      <el-table-column prop="dataset_name" label="数据集" min-width="170" />
      <el-table-column label="测评尺子" min-width="150">
        <template #default="{ row }">{{ row.ruler_label || `${row.benchmark}（模拟）` }}</template>
      </el-table-column>
      <el-table-column label="可见范围" width="110"><template #default="{ row }"><VisibilityTag :visibility="row.visibility" /></template></el-table-column>
      <el-table-column label="进度" min-width="150">
        <template #default="{ row }"><el-progress :percentage="row.progress" :stroke-width="6" /></template>
      </el-table-column>
      <el-table-column label="综合分" width="90">
        <template #default="{ row }">
          <strong v-if="row.result">{{ row.result.total }}</strong>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }"><el-tag size="small" :type="statusType(row.status)">{{ names[row.status] }}</el-tag></template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" min-width="175" />
      <el-table-column label="操作" width="200">
        <template #default="{ row }">
          <el-button text type="primary" size="small" @click="open(row)">详情</el-button>
          <el-button v-if="canManage(row) && ['queued', 'running'].includes(row.status)" text size="small" @click="action(row, 'pause')">暂停</el-button>
          <el-button v-if="canManage(row) && row.status === 'paused'" text size="small" @click="action(row, 'resume')">恢复</el-button>
          <el-button v-if="canManage(row) && ['queued', 'running', 'paused'].includes(row.status)" text type="danger" size="small" @click="action(row, 'cancel')">取消</el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>

  <CreateEvaluationDialog v-model:visible="dialog" :models="models" :datasets="datasets" :rulers="rulers" @created="onCreated" />
</template>
