<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'
import EChart from '../components/EChart.vue'
const dashboard = ref(null), monitor = ref(null), connection = ref('连接中')
let socket
let retry
async function load() { dashboard.value = await api.get('/dashboard'); monitor.value = await api.get('/monitor') }
function connect() { const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'; socket = new WebSocket(`${protocol}//${location.host}/ws/monitor`); socket.onopen = () => { connection.value = '实时连接'; socket.send(JSON.stringify({ token: localStorage.getItem('ic_token') })) }; socket.onmessage = event => { monitor.value = JSON.parse(event.data) }; socket.onclose = () => { connection.value = '轮询备用'; retry = setTimeout(connect, 5000) }; socket.onerror = () => socket.close() }
onMounted(async () => { await load(); connect() })
onUnmounted(() => { clearTimeout(retry); if (socket) { socket.onclose = null; socket.close() } })
const chart = computed(() => ({ tooltip: { trigger: 'axis' }, legend: { data: ['利用率', '显存'], bottom: 0 }, grid: { left: 35, right: 15, top: 25, bottom: 48 }, xAxis: { type: 'category', data: monitor.value?.devices.map(x => x.name) || [], axisLine: { lineStyle: { color: '#e9ecf1' } } }, yAxis: { type: 'value', max: 100, splitLine: { lineStyle: { color: '#f0f1f4' } } }, series: [{ name: '利用率', type: 'bar', data: monitor.value?.devices.map(x => x.utilization) || [], itemStyle: { color: '#c91e32' } }, { name: '显存', type: 'bar', data: monitor.value?.devices.map(x => x.memory) || [], itemStyle: { color: '#e5a6af' } }] }))
</script>
<template>
  <div class="page-heading"><div><div class="eyebrow">OPERATIONS / OBSERVABILITY</div><h1>运营与监控</h1><p class="subtitle">汇总平台资源与任务状态，观察实时监控通信。</p></div><el-tag type="success">{{ connection }}</el-tag></div>
  <div class="notice">设备指标由 WebSocket 推送模拟数据；用户操作统计、部门分布与真实 VLA 设备需接入现有服务。</div>
  <div class="grid four"><div v-for="[label,value] in [['模型资产',dashboard?.counts.models],['数据集',dashboard?.counts.datasets],['部署申请',dashboard?.counts.deployments],['训练任务',dashboard?.counts.school_jobs]]" :key="label" class="card stat"><div class="stat-label">{{ label }}</div><div class="stat-number">{{ value ?? '—' }}</div><div class="stat-note">累计记录</div></div></div>
  <div class="grid two section-gap"><section class="card card-pad"><div class="card-header"><h2>资源利用率</h2><span class="muted small">WebSocket · 每 2 秒刷新</span></div><EChart :option="chart" height="295px" /></section><section class="card card-pad"><div class="card-header"><h2>计算设备</h2><span class="muted small">{{ monitor?.timestamp }}</span></div><div v-for="device in monitor?.devices || []" :key="device.name" class="device"><div class="flex-between"><strong>{{ device.name }}</strong><span class="muted small">{{ device.status }}</span></div><div class="device-bars"><span>利用率</span><el-progress :percentage="device.utilization" :stroke-width="6" /><span>显存</span><el-progress :percentage="device.memory" :stroke-width="6" color="#e5a6af" /></div></div></section></div>
</template>
<style scoped>.device { padding:8px 0 12px; border-bottom:1px solid #edf0f3; font-size:13px; }.device:last-child { border:0; }.device-bars { display:grid; grid-template-columns:40px 1fr; gap:7px 12px; align-items:center; margin-top:8px; font-size:11px; color:#9aa3b0; }</style>
