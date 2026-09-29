<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
const rows = ref([]), models = ref([]), dialog = ref(false)
const form = reactive({ model_id: '', hardware: '910B', reason: '' })
const statusName = { pending: '待审批', approved: '已批准', rejected: '已拒绝' }
async function load() { [rows.value, models.value] = await Promise.all([api.get('/deployments'), api.get('/models')]) }
onMounted(load)
async function create() { if (!form.model_id || form.reason.trim().length < 3) return ElMessage.warning('请选择模型并填写申请理由'); await api.post('/deployments', form); dialog.value = false; form.reason = ''; await load(); ElMessage.success('申请已提交') }
async function decide(row, action) { await api.patch(`/deployments/${row.id}`, { action }); await load(); ElMessage.success('审批状态已更新') }
</script>
<template>
  <div class="page-heading"><div><div class="eyebrow">MAAS / DEPLOYMENT</div><h1>部署申请</h1><p class="subtitle">从模型选型到部署审批，保留每一条申请记录。</p></div><el-button type="primary" @click="dialog=true">+ 新建申请</el-button></div>
  <div class="notice">本页记录和审批流程可用；批准后不会自动调度实际 NPU/GPU 资源。</div>
  <section class="card card-pad"><div class="card-header"><h2>申请记录</h2><span class="muted small">{{ rows.length }} 条</span></div><el-table :data="rows" empty-text="暂无申请"><el-table-column prop="model_name" label="模型" min-width="150" /><el-table-column prop="hardware" label="目标硬件" width="125" /><el-table-column prop="reason" label="申请理由" min-width="220" show-overflow-tooltip /><el-table-column prop="created_at" label="提交时间" min-width="175" /><el-table-column label="状态" width="105"><template #default="{row}"><el-tag :type="row.status === 'approved' ? 'success' : row.status === 'rejected' ? 'danger' : 'warning'" size="small">{{ statusName[row.status] }}</el-tag></template></el-table-column><el-table-column label="操作" width="180"><template #default="{row}"><template v-if="row.status === 'pending'"><el-button size="small" text type="success" @click="decide(row, 'approve')">批准</el-button><el-button size="small" text type="danger" @click="decide(row, 'reject')">拒绝</el-button></template><span v-else class="muted small">已处理</span></template></el-table-column></el-table></section>
  <el-dialog v-model="dialog" title="提交部署申请" width="min(490px, 95vw)"><el-form label-position="top"><el-form-item label="目标模型"><el-select v-model="form.model_id" placeholder="请选择模型" style="width:100%"><el-option v-for="m in models" :key="m.id" :label="m.name" :value="m.id" /></el-select></el-form-item><el-form-item label="目标硬件"><el-select v-model="form.hardware" style="width:100%"><el-option label="Ascend 910B" value="910B" /><el-option label="GPU" value="GPU" /><el-option label="其他" value="其他" /></el-select></el-form-item><el-form-item label="申请理由"><el-input v-model="form.reason" type="textarea" :rows="4" maxlength="500" show-word-limit /></el-form-item></el-form><template #footer><el-button @click="dialog=false">取消</el-button><el-button type="primary" @click="create">提交申请</el-button></template></el-dialog>
</template>
