<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import EChart from '../components/EChart.vue'

const router = useRouter()
const data = ref(null)
onMounted(async () => { data.value = await api.get('/dashboard') })
const stats = computed(() => [
  ['已接入模型', data.value?.counts.models ?? '—', '覆盖文本与多模态', '◇'],
  ['数据集资产', data.value?.counts.datasets ?? '—', '统一登记与版本管理', '▤'],
  ['评测任务', data.value?.counts.evaluations ?? '—', '全流程可追踪', '◷'],
  ['已生成报告', data.value?.counts.reports ?? '—', '结果沉淀与分享', '▧'],
])
const chart = computed(() => ({
  tooltip: { trigger: 'axis' }, grid: { left: 35, right: 15, top: 22, bottom: 48 },
  xAxis: { type: 'category', data: data.value?.model_scores.map(x => x.name) || [], axisLabel: { rotate: 22, color: '#8994a5', fontSize: 11 }, axisLine: { lineStyle: { color: '#e7ebf0' } } },
  yAxis: { type: 'value', min: 0, max: 100, splitLine: { lineStyle: { color: '#f0f1f4' } }, axisLabel: { color: '#9aa4b1' } },
  series: [{ type: 'bar', barWidth: 24, data: data.value?.model_scores.map(x => x.score) || [], itemStyle: { color: '#c91e32', borderRadius: [5, 5, 0, 0] } }],
}))
const domains = [
  { icon: '◇', name: 'MaaS 服务', detail: '模型管理、能力榜单与部署申请', to: '/models', color: '#fff1f2' },
  { icon: '▤', name: '数据与评测', detail: '数据资产、评测任务与报告', to: '/evaluations', color: '#edf5ff' },
  { icon: '✧', name: 'Agent 应用', detail: '智能对话、论文助手与创作工具', to: '/agents', color: '#f2efff' },
  { icon: '◈', name: 'AI School', detail: '架构学习、训练与推理沙箱', to: '/school', color: '#ebf7ef' },
]
</script>
<template>
  <div class="page-heading"><div><div class="eyebrow">INNOVATIONCORE AI / OVERVIEW</div><h1>平台概览</h1><p class="subtitle">从模型接入到场景落地，在一个工作台完成创新闭环。</p></div><el-button type="primary" @click="router.push('/evaluations')">+ 创建评测任务</el-button></div>
  <div class="notice">模型目录和智能对话已连接真实 API；评测执行器、论文目录及部分创作工具仍为演示实现。</div>
  <div class="grid four"><div v-for="[label, value, note, icon] in stats" :key="label" class="card stat"><span class="stat-icon">{{ icon }}</span><div class="stat-label">{{ label }}</div><div class="stat-number">{{ value }}</div><div class="stat-note">{{ note }}</div></div></div>
  <div class="grid two section-gap"><section class="card card-pad"><div class="card-header"><h2>模型能力概览</h2><span v-if="data?.model_scores.length" class="muted small">本地登记分数 / 100</span></div><EChart v-if="data?.model_scores.length" :option="chart" height="275px" /><div v-else class="empty">当前模型没有实际评测分数。<el-button text type="primary" @click="router.push('/models')">查看模型广场 →</el-button></div></section><section class="card card-pad"><div class="card-header"><h2>业务能力矩阵</h2><span class="muted small">4 大核心模块</span></div><div class="domain-list"><div v-for="domain in domains" :key="domain.name" class="domain-row" @click="router.push(domain.to)"><span class="domain-icon" :style="{ background: domain.color }">{{ domain.icon }}</span><span class="domain-copy"><strong>{{ domain.name }}</strong><small>{{ domain.detail }}</small></span><span class="muted">↗</span></div></div></section></div>
  <div class="grid two section-gap"><section class="card card-pad"><div class="card-header"><h2>最近评测</h2><el-button text @click="router.push('/evaluations')">查看全部 →</el-button></div><div v-if="!data?.recent_evaluations.length" class="empty">暂无评测任务，创建一个任务体验实时进度。</div><ul v-else class="flow-list"><li v-for="task in data.recent_evaluations" :key="task.id"><span class="flow-index">E</span><span style="flex:1">{{ task.model_name }} <span class="muted small">/ {{ task.benchmark }}</span></span><el-tag size="small" :type="task.status === 'completed' ? 'success' : 'warning'">{{ task.status }}</el-tag></li></ul></section><section class="card card-pad"><div class="card-header"><h2>典型工作流</h2><span class="muted small">从资产到价值</span></div><ul class="flow-list"><li><span class="flow-index">1</span> 注册模型与数据集</li><li><span class="flow-index">2</span> 提交评测，追踪日志与进度</li><li><span class="flow-index">3</span> 对比榜单，生成并发布报告</li><li><span class="flow-index">4</span> 申请部署，进入 Agent 与实训场景</li></ul></section></div>
</template>
<style scoped>
.domain-list { display: grid; gap: 3px; }.domain-row { display: flex; align-items: center; gap: 13px; padding: 10px 8px; cursor: pointer; border-radius: 8px; }.domain-row:hover { background: #f7f8fa; }.domain-icon { width: 42px; height: 42px; display: grid; place-items: center; font-size: 22px; border-radius: 9px; }.domain-copy { flex: 1; display: grid; gap: 4px; }.domain-copy strong { font-size: 13px; }.domain-copy small { color: #9099a9; font-size: 11px; }
</style>
