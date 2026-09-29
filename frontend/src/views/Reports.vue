<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
const rows = ref([]), active = ref(null)
async function load() { rows.value = await api.get('/reports') }
onMounted(load)
async function toggle(row) { await api.patch(`/reports/${row.id}/publish`); await load(); active.value = rows.value.find(x => x.id === row.id) || null; ElMessage.success('发布状态已更新') }
</script>
<template>
  <div class="page-heading"><div><div class="eyebrow">DATA / REPORTS</div><h1>评测报告</h1><p class="subtitle">查看评测结论，并管理报告的内部发布状态。</p></div></div>
  <div class="notice">报告中的分数来自模拟执行器，仅用于演示报告流转。</div>
  <section class="card card-pad"><div class="card-header"><h2>报告列表</h2><span class="muted small">{{ rows.length }} 篇</span></div><el-table :data="rows" empty-text="评测任务完成后自动生成报告"><el-table-column prop="title" label="标题" min-width="210" /><el-table-column label="示例得分" width="110"><template #default="{row}"><strong>{{ row.score }}</strong></template></el-table-column><el-table-column prop="created_at" label="生成时间" min-width="180" /><el-table-column label="发布状态" width="105"><template #default="{row}"><el-tag size="small" :type="row.published ? 'success' : 'info'">{{ row.published ? '已发布' : '草稿' }}</el-tag></template></el-table-column><el-table-column label="操作" width="170"><template #default="{row}"><el-button text type="primary" @click="active=row">预览</el-button><el-button text @click="toggle(row)">{{ row.published ? '撤回' : '发布' }}</el-button></template></el-table-column></el-table></section>
  <el-dialog :model-value="!!active" :title="active?.title || '报告预览'" width="min(620px,95vw)" @close="active=null"><template v-if="active"><div class="report-cover"><div class="eyebrow">INNOVATIONCORE / EVALUATION REPORT</div><h2>{{ active.title }}</h2><p>示例综合得分 <strong>{{ active.score }}</strong> / 100</p></div><h3>摘要</h3><p class="subtitle" style="line-height:1.9">{{ active.summary }}</p><el-divider /><div class="small muted">任务 ID：{{ active.evaluation_id }} · 生成时间：{{ active.created_at }}</div></template><template #footer><el-button @click="active=null">关闭</el-button><el-button v-if="active" type="primary" @click="toggle(active)">{{ active.published ? '撤回发布' : '发布报告' }}</el-button></template></el-dialog>
</template>
<style scoped>.report-cover { background:#faf3f4; border-left:4px solid #c91e32; padding:28px; margin-bottom:25px; }.report-cover h2 { margin:9px 0 18px; }.report-cover p { margin:0; color:#7d8797; }.report-cover strong { font-size:28px; color:#c91e32; }</style>
