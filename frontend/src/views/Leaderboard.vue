<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api'
import EChart from '../components/EChart.vue'
const board = ref('Ruler1')
const rows = ref([])
const boards = ['Ruler1', 'Ruler2', 'VLM', 'Arena']
async function load() { rows.value = (await api.get('/leaderboard', { params: { board: board.value } })).rows }
onMounted(load)
watch(board, load)
const chart = computed(() => ({ tooltip: { trigger: 'axis' }, grid: { left: 36, right: 15, top: 15, bottom: 65 }, xAxis: { type: 'category', data: rows.value.map(r => r.name), axisLabel: { rotate: 25, color: '#8792a2' }, axisLine: { lineStyle: { color: '#e8ebf0' } } }, yAxis: { type: 'value', max: 100, splitLine: { lineStyle: { color: '#f0f1f4' } } }, series: [{ type: 'bar', barWidth: 30, data: rows.value.map((r,i) => ({ value: r.score, itemStyle: { color: i === 0 ? '#c91e32' : '#e89aa5', borderRadius: [5,5,0,0] } })) }] }))
</script>
<template>
  <div class="page-heading"><div><div class="eyebrow">MAAS / LEADERBOARD</div><h1>模型榜单</h1><p class="subtitle">通用、领域、多模态与竞技场四类能力视图。</p></div></div>
  <div class="notice">榜单由模型登记时的五维示例分数按权重生成，尚未接入真实 Ruler 评测结果或历史多期趋势。</div>
  <el-tabs v-model="board"><el-tab-pane v-for="item in boards" :key="item" :label="item" :name="item" /></el-tabs>
  <div class="grid two"><section class="card card-pad"><div class="card-header"><h2>{{ board }} 排名</h2><span class="muted small">满分 100</span></div><el-table :data="rows" style="width:100%"><el-table-column label="排名" width="75"><template #default="{row}"><strong :class="{first: row.rank === 1}">#{{ row.rank }}</strong></template></el-table-column><el-table-column prop="name" label="模型" min-width="150" /><el-table-column prop="provider" label="机构" min-width="90" /><el-table-column label="综合分" width="92"><template #default="{row}"><b>{{ row.score }}</b></template></el-table-column></el-table></section><section class="card card-pad"><div class="card-header"><h2>分数对比</h2><span class="chip">{{ board }}</span></div><EChart :option="chart" height="330px" /></section></div>
</template>
<style scoped>.first { color: #c91e32; }</style>
