<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, downloadFile } from '../api'
import { useAuth } from '../stores/auth'
import VisibilityField from '../components/VisibilityField.vue'
import VisibilityTag from '../components/VisibilityTag.vue'
const auth = useAuth()
const rows = ref([]), dialog = ref(false), detail = ref(null), uploading = ref(false), fileInput = ref(null)
const form = reactive({ name: '', category: '通用', version: 'v1.0', description: '', visibility: 'private' })
function canDelete(row) { return !!row && (auth.role === 'admin' || (row.owner_user_id != null && String(row.owner_user_id) === auth.userId)) }
function openCreate() { form.visibility = 'private'; dialog.value = true }
async function load() { rows.value = await api.get('/datasets'); if (detail.value) detail.value = rows.value.find(x => x.id === detail.value.id) || null }
onMounted(load)
async function create() { if (!form.name.trim()) return ElMessage.warning('请输入数据集名称'); await api.post('/datasets', form); dialog.value = false; form.name = ''; form.description = ''; form.visibility = 'private'; await load(); ElMessage.success('数据集已登记') }
async function remove(row) { await ElMessageBox.confirm(`删除 ${row.name} 及其上传文件？`, '确认操作', { type: 'warning' }); await api.delete(`/datasets/${row.id}`); await load() }
async function upload(event) { const file = event.target.files?.[0]; if (!file || !detail.value) return; if (file.size > 25 * 1024 * 1024) { ElMessage.warning('单文件上限 25 MiB'); event.target.value = ''; return } uploading.value = true; try { const data = new FormData(); data.append('file', file); await api.post(`/datasets/${detail.value.id}/files`, data); await load(); ElMessage.success('文件已上传') } finally { uploading.value = false; event.target.value = '' } }
async function download(row) { try { await downloadFile(`/datasets/${detail.value.id}/files/${row.id}`, row.name) } catch { ElMessage.error('下载失败') } }
</script>
<template>
  <div class="page-heading"><div><div class="eyebrow">DATA / DATASETS</div><h1>数据集仓库</h1><p class="subtitle">登记、浏览和下载评测数据，管理分类和版本信息。</p></div><el-button type="primary" @click="openCreate">+ 登记数据集</el-button></div>
  <div class="grid three"><div v-for="row in rows" :key="row.id" class="card card-pad dataset-card"><div class="flex-between"><div class="dataset-icon">▤</div><el-dropdown v-if="canDelete(row)"><span style="cursor:pointer">•••</span><template #dropdown><el-dropdown-menu><el-dropdown-item @click="remove(row)">删除</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div><h3>{{ row.name }}</h3><p class="subtitle">{{ row.description }}</p><div class="dataset-foot"><div class="dataset-badges"><span class="chip">{{ row.category }}</span><VisibilityTag :visibility="row.visibility" /></div><span class="muted small">{{ row.version }} · {{ row.size }} 条示例 · {{ row.files.length }} 文件</span></div><el-button style="width:100%;margin-top:16px" @click="detail=row">查看文件</el-button></div></div>
  <div v-if="!rows.length" class="card empty">暂无数据集</div>
  <el-dialog v-model="dialog" title="登记数据集" width="min(490px,95vw)"><el-form label-position="top"><el-form-item label="名称"><el-input v-model="form.name" /></el-form-item><div class="grid two"><el-form-item label="分类"><el-select v-model="form.category"><el-option v-for="x in ['通用','通信','多模态','VLA','训练']" :key="x" :label="x" :value="x" /></el-select></el-form-item><el-form-item label="版本"><el-input v-model="form.version" /></el-form-item></div><el-form-item label="描述"><el-input v-model="form.description" type="textarea" /></el-form-item><VisibilityField v-model="form.visibility" /></el-form><template #footer><el-button @click="dialog=false">取消</el-button><el-button type="primary" @click="create">保存</el-button></template></el-dialog>
  <el-drawer :model-value="!!detail" :title="detail?.name || '文件列表'" size="min(480px,100vw)" @close="detail=null"><p class="subtitle">{{ detail?.description }}</p><div v-if="canDelete(detail)" class="notice" style="margin-top:20px">上传文件仅存储和下载；本演示执行器不解析文件内容。</div><input v-if="canDelete(detail)" ref="fileInput" type="file" hidden @change="upload" /><el-button v-if="canDelete(detail)" type="primary" :loading="uploading" @click="fileInput?.click()">上传文件（≤25 MiB）</el-button><div v-if="!detail?.files.length" class="empty">暂无上传文件</div><ul v-else class="flow-list section-gap"><li v-for="file in detail.files" :key="file.id"><span class="flow-index">↧</span><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis">{{ file.name }}<br /><small class="muted">{{ (file.bytes/1024).toFixed(1) }} KiB</small></span><el-button size="small" @click="download(file)">下载</el-button></li></ul></el-drawer>
</template>
<style scoped>.dataset-card h3 { margin: 18px 0 8px; font-size: 16px; }.dataset-card .subtitle { min-height: 41px; }.dataset-icon { width: 41px; height: 41px; display:grid; place-items:center; border-radius:9px; background:#edf5ff; color:#5186c4; font-size:22px; }.dataset-foot { display:flex; justify-content:space-between; align-items:center; margin-top:20px; gap:8px; flex-wrap:wrap; }.dataset-badges { display:flex; align-items:center; gap:6px; }</style>
