<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, readSSE } from '../api'
import ModelLogo from '../components/ModelLogo.vue'

const route = useRoute()
const router = useRouter()
const tab = ref(typeof route.query.tab === 'string' ? route.query.tab : 'chat')
const models = ref([])
const primaryId = ref('')
const secondaryId = ref('')
const showBattlePicker = ref(false)
const sessions = reactive({})
const prompt = ref('')
const busy = ref(false)
const controllers = new Set()
const messagePanes = new Map()
const pendingScroll = new Set()
const primaryModel = computed(() => models.value.find(model => model.id === primaryId.value))
const secondaryModel = computed(() => models.value.find(model => model.id === secondaryId.value))
const activeModels = computed(() => [primaryModel.value, secondaryModel.value].filter(Boolean))
const battleOptions = computed(() => models.value.filter(model => model.id !== primaryId.value))

const papers = ref([]), query = ref(''), integrations = ref([]), selectedTool = ref('research'), toolText = ref(''), toolOutput = ref('')
const tools = [{ value: 'research', label: 'Deep Research' }, { value: 'ppt', label: 'PPT 生成' }, { value: 'prompt', label: 'Prompt 优化' }, { value: 'translate', label: '论文翻译' }, { value: 'download', label: '模型高速下载' }]

function sessionFor(model) {
  if (!sessions[model.id]) {
    const defaults = model.endpoints?.[0] || {}
    sessions[model.id] = { messages: [], thinking: false,
      top_p: defaults.top_p ?? 1, top_k: defaults.top_k ?? 40, temperature: defaults.temperature ?? 1,
      overrides: { top_p: false, top_k: false, temperature: false } }
  }
  return sessions[model.id]
}
async function loadModels() {
  try {
    models.value = await api.get('/models')
    const requested = typeof route.query.model === 'string' ? route.query.model : ''
    if (models.value.some(model => model.id === requested)) primaryId.value = requested
    else if (!models.value.some(model => model.id === primaryId.value)) primaryId.value = models.value[0]?.id || ''
    if (secondaryId.value === primaryId.value || !models.value.some(model => model.id === secondaryId.value)) secondaryId.value = ''
    activeModels.value.forEach(sessionFor)
  } catch { /* API interceptor shows the error. */ }
}
function changePrimary(id) {
  primaryId.value = id
  if (secondaryId.value === id) secondaryId.value = ''
  const model = models.value.find(item => item.id === id)
  if (model) sessionFor(model)
  router.replace({ path: '/agents', query: { tab: 'chat', model: id } })
}
function changeSecondary(id) {
  secondaryId.value = id
  const model = models.value.find(item => item.id === id)
  if (model) sessionFor(model)
}
function closeBattle() { secondaryId.value = ''; showBattlePicker.value = false }
function bindMessages(modelId, element) {
  if (element) messagePanes.set(modelId, element)
  else messagePanes.delete(modelId)
}
function scrollMessages(modelId) {
  if (pendingScroll.has(modelId)) return
  pendingScroll.add(modelId)
  requestAnimationFrame(() => {
    pendingScroll.delete(modelId)
    const pane = messagePanes.get(modelId)
    if (pane) pane.scrollTop = pane.scrollHeight
  })
}

async function send() {
  const message = prompt.value.trim()
  if (!message || busy.value || !activeModels.value.length) return
  const targets = [...activeModels.value]
  prompt.value = ''
  busy.value = true
  await Promise.allSettled(targets.map(async model => {
    const session = sessionFor(model)
    const history = session.messages.filter(item => item.content && ['user', 'assistant'].includes(item.role))
      .slice(-20).map(item => ({ role: item.role, content: item.content }))
    session.messages.push({ role: 'user', content: message })
    const reply = reactive({ role: 'assistant', content: '', reasoning: '', error: '' })
    session.messages.push(reply)
    scrollMessages(model.id)
    const controller = new AbortController()
    controllers.add(controller)
    try {
      await readSSE('/agents/chat/stream', { model_id: model.id, message, history,
        thinking: session.thinking, top_p: session.overrides.top_p ? session.top_p : null,
        top_k: session.overrides.top_k ? session.top_k : null,
        temperature: session.overrides.temperature ? session.temperature : null }, (event, data) => {
        if (event === 'chunk') reply.content += data.text || ''
        if (event === 'reasoning') reply.reasoning += data.text || ''
        if (event === 'error') reply.error = data.message || '模型服务调用失败'
        if (event === 'done' && !reply.content) reply.error = '模型未返回正文'
        scrollMessages(model.id)
      }, controller.signal)
    } catch (error) { reply.error = error.message || '请求失败'; scrollMessages(model.id) }
    finally { controllers.delete(controller) }
  }))
  busy.value = false
}

async function search() { papers.value = (await api.get('/agents/papers', { params: { q: query.value } })).rows }
async function runTool() { if (!toolText.value.trim()) return ElMessage.warning('请先输入内容'); const result = await api.post(`/agents/tools/${selectedTool.value}`, { content: toolText.value }); toolOutput.value = result.output }

watch(() => route.query.model, async id => {
  if (typeof id === 'string' && !models.value.some(model => model.id === id)) await loadModels()
  if (typeof id === 'string' && models.value.some(model => model.id === id)) changePrimary(id)
})
onMounted(async () => {
  await loadModels()
  try { await Promise.all([search(), api.get('/agents/integrations').then(rows => { integrations.value = rows })]) }
  catch { /* API interceptor shows the error. */ }
})
onUnmounted(() => { for (const controller of controllers) controller.abort() })
</script>

<template>
  <el-tabs v-model="tab" class="agent-tabs"><el-tab-pane label="智能对话" name="chat" /><el-tab-pane label="论文助手" name="papers" /><el-tab-pane label="创作工具" name="tools" /><el-tab-pane label="服务集成" name="integrations" /></el-tabs>

  <section v-if="tab === 'chat'" class="chat-shell">
    <div class="chat-toolbar">
      <div class="model-picker"><span class="muted small">当前模型</span><el-select :model-value="primaryId" :disabled="busy" placeholder="选择模型" @update:model-value="changePrimary"><el-option v-for="model in models" :key="model.id" :label="model.name" :value="model.id" /></el-select></div>
      <div class="battle-picker">
        <el-button v-if="!showBattlePicker && !secondaryId" :disabled="!battleOptions.length || busy" @click="showBattlePicker=true">+ 添加对战模型</el-button>
        <template v-else><el-select v-model="secondaryId" :disabled="busy" clearable placeholder="选择另一个模型" @change="changeSecondary"><el-option v-for="model in battleOptions" :key="model.id" :label="model.name" :value="model.id" /></el-select><el-button text :disabled="busy" @click="closeBattle">取消对战</el-button></template>
      </div>
    </div>
    <div v-if="!activeModels.length" class="card empty">暂无可用模型，请先在模型广场注册模型。</div>
    <div v-else class="chat-layout" :class="{ battle: !!secondaryModel }">
      <div v-for="(model, paneIndex) in activeModels" :key="model.id" class="card chat-pane">
        <div class="pane-header"><ModelLogo :model="model" /><div><strong>{{ model.name }}</strong><span class="muted small">{{ model.provider }} · {{ model.id }}{{ secondaryModel ? ` · 模型 ${paneIndex ? 'B' : 'A'}` : '' }}</span></div></div>
        <div class="settings">
          <div class="slider-field"><div><span>Top-P</span><b>{{ sessionFor(model).top_p.toFixed(2) }}</b></div><el-slider v-model="sessionFor(model).top_p" :min="0" :max="1" :step="0.01" :disabled="busy" @change="sessionFor(model).overrides.top_p=true" /></div>
          <div class="slider-field"><div><span>Top-K</span><b>{{ sessionFor(model).top_k }}</b></div><el-slider v-model="sessionFor(model).top_k" :min="0" :max="1000" :step="1" :disabled="busy" @change="sessionFor(model).overrides.top_k=true" /></div>
          <div class="slider-field"><div><span>温度</span><b>{{ sessionFor(model).temperature.toFixed(2) }}</b></div><el-slider v-model="sessionFor(model).temperature" :min="0" :max="2" :step="0.01" :disabled="busy" @change="sessionFor(model).overrides.temperature=true" /></div>
          <div class="think-field"><span>思考模式</span><el-switch v-model="sessionFor(model).thinking" :disabled="busy || !model.endpoints?.every(e => e.thinking_schema?.enabled)" /></div>
          <div v-if="model.endpoints.length > 1" class="parameter-note">多个端点依次轮询；拖动滑块后将覆盖所有端点的对应参数。</div>
          <div v-if="model.provider.toLowerCase() === 'deepseek'" class="parameter-note">DeepSeek 当前接口未提供 Top-K；关闭思考时 Top-P 固定为 1，开启思考时温度不生效且 Top-P 有效范围为 0.95–1。</div>
        </div>
        <div :ref="element => bindMessages(model.id, element)" class="chat-messages">
          <div v-if="!sessionFor(model).messages.length" class="chat-placeholder">向 {{ model.name }} 提问，回复会显示在这里。</div>
          <div v-for="(item, index) in sessionFor(model).messages" :key="index" class="message" :class="item.role">
            <div class="message-avatar">{{ item.role === 'user' ? '我' : 'AI' }}</div>
            <div class="message-body"><details v-if="item.reasoning" class="reasoning"><summary>思考过程</summary>{{ item.reasoning }}</details><div class="message-text">{{ item.error || item.content || '正在生成…' }}</div></div>
          </div>
        </div>
      </div>
    </div>
    <div v-if="activeModels.length" class="card chat-compose"><el-input v-model="prompt" type="textarea" :rows="3" :disabled="busy" :placeholder="secondaryModel ? '输入同一个问题，同时发送给两个模型' : '输入消息，与当前模型对话'" @keydown.ctrl.enter="send" /><div class="compose-footer"><span class="muted small">Ctrl + Enter 发送{{ secondaryModel ? ' · 两个模型同时回答' : '' }}</span><el-button type="primary" :loading="busy" @click="send">发送消息 →</el-button></div></div>
  </section>

  <div v-if="tab === 'papers'" class="card card-pad"><div class="card-header"><h2>论文检索</h2><span class="chip">示例目录</span></div><div class="toolbar"><el-input v-model="query" placeholder="搜索标题、领域或摘要" clearable @keyup.enter="search" /><el-button type="primary" @click="search">搜索</el-button></div><div v-for="paper in papers" :key="paper.id" class="paper"><div class="flex-between"><h3>{{ paper.title }}</h3><span class="chip">{{ paper.category }}</span></div><p class="subtitle">{{ paper.abstract }}</p><div class="muted small">{{ paper.year }} · {{ paper.source }}</div></div><div v-if="!papers.length" class="empty">没有匹配的论文</div></div>
  <div v-if="tab === 'tools'" class="grid two"><section class="card card-pad"><div class="card-header"><h2>选择工具</h2><span class="chip">模拟适配器</span></div><el-select v-model="selectedTool" style="width:100%;margin-bottom:18px"><el-option v-for="item in tools" :key="item.value" :label="item.label" :value="item.value" /></el-select><el-input v-model="toolText" type="textarea" :rows="7" placeholder="描述你的研究主题、演示目标、提示词或论文内容..." /><el-button type="primary" style="margin-top:18px" @click="runTool">运行工具</el-button></section><section class="card card-pad"><div class="card-header"><h2>执行结果</h2><span class="muted small">仅供流程演示</span></div><div v-if="toolOutput" class="tool-output">{{ toolOutput }}</div><div v-else class="empty">选择工具并提交内容后，在这里查看响应。</div></section></div>
  <div v-if="tab === 'integrations'" class="card card-pad"><div class="card-header"><h2>第三方服务</h2><span class="muted small">独立适配接口</span></div><div class="grid three"><div v-for="entry in integrations" :key="entry.name" class="integration"><span class="integration-icon">✧</span><strong>{{ entry.name }}</strong><el-tag size="small" type="info">{{ entry.status }}</el-tag></div></div></div>
</template>

<style scoped>
.agent-tabs { margin-top:-18px; }
.chat-shell { height:calc(100vh - 180px); min-height:520px; display:flex; flex-direction:column; gap:12px; }
.chat-toolbar,.model-picker,.battle-picker { display:flex; align-items:center; gap:10px; }
.chat-toolbar { justify-content:space-between; flex-wrap:wrap; }
.model-picker .el-select { width:240px; }.battle-picker .el-select { width:230px; }
.chat-layout { display:grid; grid-template-columns:minmax(0,1fr); gap:12px; flex:1; min-height:0; }
.chat-layout.battle { grid-template-columns:repeat(2,minmax(0,1fr)); }
.chat-pane { display:flex; flex-direction:column; min-width:0; min-height:0; overflow:hidden; }
.pane-header { display:flex; align-items:center; gap:14px; padding:16px 18px; border-bottom:1px solid var(--line); }
.pane-header strong { display:block; font-size:15px; }.pane-header .muted { display:block; margin-top:4px; overflow-wrap:anywhere; }
.settings { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px 20px; padding:10px 18px 4px; border-bottom:1px solid var(--line); }
.slider-field { min-width:0; font-size:12px; }.slider-field > div:first-child { display:flex; justify-content:space-between; color:var(--muted); }.slider-field b { color:var(--ink); font-weight:650; }
.slider-field :deep(.el-slider) { margin:0 5px; }.think-field { display:flex; align-items:center; gap:10px; font-size:12px; grid-column:1/-1; padding-bottom:8px; }
.parameter-note { grid-column:1/-1; color:var(--muted); font-size:11px; padding-bottom:8px; }
.chat-messages { flex:1; min-height:0; overflow:auto; padding:20px; display:flex; flex-direction:column; gap:18px; background:#fdfdfe; }
.chat-placeholder { margin:auto; color:var(--muted); font-size:13px; text-align:center; }
.message { display:flex; align-items:flex-start; gap:10px; max-width:92%; }.message.user { align-self:flex-end; flex-direction:row-reverse; }
.message-avatar { display:grid; place-items:center; background:#293b56; color:#fff; border-radius:9px; width:29px; height:29px; flex:0 0 29px; font-size:11px; font-weight:700; }.message.user .message-avatar { background:#c91e32; }
.message-body { min-width:0; }.message-text,.reasoning { background:#fff; border:1px solid #ecedf0; padding:12px 15px; border-radius:4px 12px 12px 12px; white-space:pre-wrap; overflow-wrap:anywhere; line-height:1.7; font-size:13px; }
.message.user .message-text { background:#fff1f2; border-color:#f8d9de; border-radius:12px 4px 12px 12px; }
.reasoning { color:#647084; background:#f8f9fc; margin-bottom:8px; }.reasoning summary { cursor:pointer; font-weight:650; margin-bottom:6px; }
.chat-compose { padding:14px 18px; }.compose-footer { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-top:10px; }
.paper { padding:18px 0; border-top:1px solid #edf0f3; }.paper h3 { font-size:15px; margin:0 0 9px; }.paper p { margin-bottom:10px; }
.tool-output { background:#f7f8fa; padding:20px; border-radius:8px; line-height:1.8; white-space:pre-wrap; font-size:13px; }
.integration { border:1px solid #e9edf1; border-radius:9px; padding:20px; display:grid; gap:12px; justify-items:start; font-size:13px; }.integration-icon { font-size:25px; color:#c91e32; }
@media(max-width:1100px) { .settings { grid-template-columns:repeat(2,minmax(0,1fr)); } }
@media(max-width:760px) { .chat-layout.battle { grid-template-columns:1fr; }.chat-messages { height:350px; flex:none; }.chat-shell { height:auto; min-height:0; }.chat-layout { min-height:450px; }.chat-toolbar { align-items:stretch; }.model-picker,.battle-picker { width:100%; }.model-picker .el-select,.battle-picker .el-select { flex:1; } }
</style>
