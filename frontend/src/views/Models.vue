<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import ModelLogo from '../components/ModelLogo.vue'
import VisibilityField from '../components/VisibilityField.vue'
import VisibilityTag from '../components/VisibilityTag.vue'

const router = useRouter()
const models = ref([])
const keyword = ref('')
const loading = ref(false)
const dialog = ref(false)
const saving = ref(false)
const editingId = ref(null)
const form = ref(emptyModel())
const filtered = computed(() => models.value.filter(model =>
  `${model.name} ${model.id} ${model.provider}`.toLowerCase().includes(keyword.value.toLowerCase()),
))

function emptyEndpoint() {
  return { endpoint_id: null, url: '', api_key: '', thinking_schema: '', self_deployed: false,
    max_context_length: 32768, top_k: 40, top_p: 1, temperature: 1 }
}
function emptyModel() {
  return { id: '', name: '', provider: '', logo_url: '', visibility: 'private', endpoints: [emptyEndpoint()] }
}
function edit(model) {
  editingId.value = model?.id || null
  form.value = model ? {
    id: model.id, name: model.name, provider: model.provider, logo_url: model.logo_url || '',
    visibility: model.visibility || 'private',
    endpoints: (model.endpoints.length ? model.endpoints : [emptyEndpoint()]).map(endpoint => ({
      ...endpoint, api_key: '',
      thinking_schema: endpoint.thinking_schema && Object.keys(endpoint.thinking_schema).length
        ? JSON.stringify(endpoint.thinking_schema, null, 2) : '',
    })),
  } : emptyModel()
  dialog.value = true
}
async function load() {
  loading.value = true
  try { models.value = await api.get('/models') } catch { /* API interceptor shows the error. */ }
  finally { loading.value = false }
}
async function save() {
  const body = { id: form.value.id.trim(), name: form.value.name.trim(),
    provider: form.value.provider.trim(), logo_url: form.value.logo_url.trim(),
    visibility: form.value.visibility, endpoints: [] }
  if (!body.id || !body.name || !body.provider) return ElMessage.warning('请填写模型 ID、名称和厂商')
  for (const [index, item] of form.value.endpoints.entries()) {
    if (!item.url.trim()) return ElMessage.warning(`请填写端点 ${index + 1} 的 URL`)
    let thinking_schema = {}
    if (item.thinking_schema.trim()) {
      try { thinking_schema = JSON.parse(item.thinking_schema) }
      catch { return ElMessage.warning(`端点 ${index + 1} 的思考 Schema 不是有效 JSON`) }
      if (!thinking_schema || typeof thinking_schema !== 'object' || Array.isArray(thinking_schema) ||
          !thinking_schema.enabled || !thinking_schema.disabled) {
        return ElMessage.warning(`端点 ${index + 1} 的思考 Schema 需要 enabled 和 disabled 对象`)
      }
    }
    body.endpoints.push({ endpoint_id: item.endpoint_id, url: item.url.trim(), api_key: item.api_key.trim(),
      thinking_schema, self_deployed: item.self_deployed, max_context_length: Number(item.max_context_length),
      top_k: Number(item.top_k), top_p: Number(item.top_p), temperature: Number(item.temperature) })
  }
  saving.value = true
  try {
    if (editingId.value) await api.put(`/models/${encodeURIComponent(editingId.value)}`, body)
    else await api.post('/models', body)
    dialog.value = false
    ElMessage.success('模型配置已保存')
    await load()
  } catch { /* API interceptor shows the error. */ }
  finally { saving.value = false }
}
function tryModel(model) { router.push({ path: '/agents', query: { tab: 'chat', model: model.id } }) }

onMounted(load)
</script>

<template>
  <div class="page-heading">
    <div><div class="eyebrow">MAAS / MODEL HUB</div><h1>模型广场</h1><p class="subtitle">选择模型进入智能对话，或注册自己的模型服务。</p></div>
    <div class="heading-actions"><el-button @click="load" :loading="loading">刷新目录</el-button><el-button type="primary" @click="edit(null)">模型注册</el-button></div>
  </div>
  <div class="toolbar"><el-input v-model="keyword" clearable placeholder="搜索模型名称、ID 或厂商" /><span class="muted small">共 {{ filtered.length }} 个模型</span></div>
  <div v-if="filtered.length" class="grid three">
    <div v-for="model in filtered" :key="model.id" class="card model-card">
      <div class="model-top"><ModelLogo :model="model" /><div class="model-title"><div class="model-name">{{ model.name }}</div><div class="model-provider">{{ model.provider }} · {{ model.id }}</div></div></div>
      <div class="model-meta"><span class="chip">{{ model.modality }}</span><VisibilityTag :visibility="model.visibility" /><el-tag size="small" type="info">{{ model.endpoints.length }} 个 API 端点</el-tag></div>
      <div class="model-detail">最大上下文 {{ model.context_length?.toLocaleString() || '—' }} tokens</div>
      <div class="model-actions"><el-button v-if="model.can_edit !== false" text @click="edit(model)">编辑配置</el-button><el-button type="primary" @click="tryModel(model)">立即体验</el-button></div>
    </div>
  </div>
  <div v-else-if="!loading" class="card empty">{{ keyword ? '没有匹配的模型' : '暂无模型，请注册模型服务' }}</div>

  <el-dialog v-model="dialog" :title="editingId ? '编辑模型配置' : '模型注册'" width="min(820px, 96vw)" top="5vh" class="model-dialog">
    <el-form label-position="top">
      <div class="form-grid">
        <el-form-item label="模型名称"><el-input v-model="form.name" maxlength="100" /></el-form-item>
        <el-form-item label="模型 ID"><el-input v-model="form.id" :disabled="!!editingId" maxlength="150" /><div v-if="editingId" class="field-hint">ID 注册后不可修改，用于唯一标识模型和调用 API。</div></el-form-item>
        <el-form-item label="厂商"><el-input v-model="form.provider" maxlength="60" /></el-form-item>
        <el-form-item label="Logo URL（选填）"><el-input v-model="form.logo_url" placeholder="https://..." /></el-form-item>
      </div>
      <VisibilityField v-model="form.visibility" />
      <div v-if="form.visibility === 'public'" class="field-hint">其他登录用户可以使用这个模型对话，调用可能产生 API 费用。</div>
      <div class="endpoint-heading"><h3>API 端点</h3><el-button @click="form.endpoints.push(emptyEndpoint())">+ 添加端点</el-button></div>
      <div v-for="(endpoint, index) in form.endpoints" :key="endpoint.endpoint_id || index" class="endpoint-box">
        <div class="endpoint-title"><strong>端点 {{ index + 1 }}</strong><el-button v-if="form.endpoints.length > 1" text type="danger" @click="form.endpoints.splice(index, 1)">移除</el-button></div>
        <div class="form-grid">
          <el-form-item label="API URL"><el-input v-model="endpoint.url" placeholder="https://api.example.com/v1" /></el-form-item>
          <el-form-item label="API Key"><el-input v-model="endpoint.api_key" type="password" show-password :placeholder="endpoint.has_api_key ? '已配置；留空则沿用原密钥' : '填写端点密钥；无密钥可留空'" autocomplete="new-password" /></el-form-item>
          <el-form-item label="部署方式"><el-switch v-model="endpoint.self_deployed" active-text="自行部署" inactive-text="厂商提供" /></el-form-item>
          <el-form-item label="最大上下文长度（tokens）"><el-input-number v-model="endpoint.max_context_length" :min="1" :max="2000000" style="width:100%" /></el-form-item>
          <el-form-item label="Top-K"><el-input-number v-model="endpoint.top_k" :min="0" :max="1000" style="width:100%" /></el-form-item>
          <el-form-item label="Top-P"><el-input-number v-model="endpoint.top_p" :min="0" :max="1" :step="0.05" :precision="2" style="width:100%" /></el-form-item>
          <el-form-item label="温度"><el-input-number v-model="endpoint.temperature" :min="0" :max="2" :step="0.1" :precision="2" style="width:100%" /></el-form-item>
        </div>
        <el-form-item label="思考开关 Schema（JSON）"><el-input v-model="endpoint.thinking_schema" type="textarea" :rows="5" placeholder='{"enabled":{"thinking":{"type":"enabled"}},"disabled":{"thinking":{"type":"disabled"}}}' /><div class="field-hint">分别填写打开和关闭思考时附加到请求体的 JSON 字段。</div></el-form-item>
      </div>
    </el-form>
    <template #footer><el-button @click="dialog=false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存模型</el-button></template>
  </el-dialog>
</template>

<style scoped>
.heading-actions { display:flex; gap:8px; }
.model-card { min-height:225px; display:flex; flex-direction:column; }
.model-title { min-width:0; }
.model-name { overflow-wrap:anywhere; }
.model-provider { overflow-wrap:anywhere; }
.model-meta { margin:18px 0 8px; flex-wrap:wrap; }
.model-detail { color:var(--muted); font-size:12px; }
.model-actions { margin-top:auto; padding-top:20px; display:flex; justify-content:flex-end; gap:6px; }
.form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:0 16px; }
.endpoint-heading,.endpoint-title { display:flex; align-items:center; justify-content:space-between; }
.endpoint-heading { margin:10px 0; }.endpoint-heading h3 { margin:0; font-size:16px; }
.endpoint-box { padding:18px; margin:12px 0; border:1px solid var(--line); border-radius:10px; background:#fbfcfe; }
.endpoint-title { margin-bottom:14px; font-size:13px; }
.field-hint { color:var(--muted); font-size:11px; line-height:1.5; margin-top:4px; }
@media(max-width:700px) { .form-grid { grid-template-columns:1fr; } .heading-actions { width:100%; justify-content:flex-end; } }
</style>
