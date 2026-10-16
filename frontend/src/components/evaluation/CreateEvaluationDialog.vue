<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../../api'
import RulerPicker from './RulerPicker.vue'
import BenchmarkPicker from './BenchmarkPicker.vue'
import VisibilityField from '../VisibilityField.vue'

const props = defineProps({
  visible: { type: Boolean, required: true },
  models: { type: Array, required: true },     // GET /api/models
  datasets: { type: Array, required: true },   // GET /api/datasets
  rulers: { type: Array, required: true },
})
const emit = defineEmits(['update:visible', 'created'])

const submitting = ref(false)
const caps = ref(null)   // GET /api/agent-capabilities
const form = reactive({
  model_ids: [],
  dataset_id: '',
  benchmark: 'Ruler1',
  visibility: 'private',
  use_default: true,
  config: { sample_limit: 5, sample_limits: {}, sync_scores: false },
  agent: {
    benchmark: '',
    harness: '',
    environment: 'docker',
    model_api_protocol: 'auto',
    params: { task_concurrency: 1, k: null, max_retries: null },
  },
})

const rulerList = computed(() => (Array.isArray(props.rulers) ? props.rulers : []))
const isRuler = computed(() => rulerList.value.some((ruler) => ruler.id === form.benchmark))
const isAgent = computed(() => form.benchmark === 'Agent')
const selectedRuler = computed(() => rulerList.value.find((ruler) => ruler.id === form.benchmark) || null)
const isSingle = computed(() => form.model_ids.length === 1)
const dimensions = computed(() => selectedRuler.value?.dimensions || [])
const defaultLimit = computed(() => selectedRuler.value?.default_sample_limit || 5)

const benchmarkList = computed(() => caps.value?.benchmarks || [])
const harnessList = computed(() => caps.value?.harnesses || [])
const environmentList = computed(() => caps.value?.environments || [])
const protocolList = computed(() => caps.value?.protocols || [])
const selectedHarness = computed(() => harnessList.value.find((h) => h.id === form.agent.harness) || null)
// 协议与 harness preferred 不符时提示
const protocolMismatch = computed(() => {
  if (!selectedHarness.value || form.agent.model_api_protocol === 'auto') return false
  return !(selectedHarness.value.preferred_protocols || []).includes(form.agent.model_api_protocol)
})

// 逐维度取样条数：未单独设置时取尺子默认值
const limitOf = (dim) => form.config.sample_limits[dim.key] ?? defaultLimit.value
const caseCount = computed(() => dimensions.value.reduce((sum, dim) => sum + limitOf(dim), 0))
const limitsSummary = computed(() => dimensions.value.map((dim) => `${dim.label} ${limitOf(dim)}`).join(' · '))

// 尺子切换：取样条数按新尺子维度重建（维度和默认值都可能不同）
function resetLimits() {
  form.config.sample_limit = defaultLimit.value
  form.config.sample_limits = Object.fromEntries(dimensions.value.map((dim) => [dim.key, defaultLimit.value]))
}

watch(() => form.benchmark, () => {
  if (isRuler.value) resetLimits()
})
// 尺子规格从接口异步到达：仅在用户尚未改动时套用默认值
watch(dimensions, (list) => {
  if (!list.length) return
  const untouched = Object.keys(form.config.sample_limits).length === 0
  if (untouched) resetLimits()
}, { immediate: true })
watch(() => props.visible, (open) => {
  if (open) return
  submitting.value = false
})

onMounted(async () => {
  try { caps.value = await api.get('/agent-capabilities') } catch { caps.value = null }
})

function buildAgentPayload() {
  const params = {}
  if (form.agent.params.task_concurrency != null) params.task_concurrency = form.agent.params.task_concurrency
  if (form.agent.params.k != null) params.k = form.agent.params.k
  if (form.agent.params.max_retries != null) params.max_retries = form.agent.params.max_retries
  return {
    benchmark: form.agent.benchmark,
    harness: form.agent.harness,
    environment: form.agent.environment,
    model_api_protocol: form.agent.model_api_protocol,
    params: Object.keys(params).length ? params : null,
  }
}

async function submit() {
  if (!form.model_ids.length) return ElMessage.warning('请选择被测模型')
  if (isAgent.value) {
    if (!form.agent.benchmark) return ElMessage.warning('请选择 benchmark')
    if (!form.agent.harness) return ElMessage.warning('请选择 harness')
  } else if (!form.dataset_id) {
    return ElMessage.warning('请选择数据集')
  }
  if (isSingle.value && isRuler.value && !form.use_default) {
    const invalid = dimensions.value.find((dim) => {
      const value = form.config.sample_limits[dim.key]
      return !Number.isInteger(value) || value < 1 || value > 5
    })
    if (invalid) return ElMessage.warning(`请填写「${invalid.label}」维度的取样条数（1-5）`)
  }
  submitting.value = true
  try {
    const paid = { benchmark: form.benchmark, visibility: form.visibility }
    if (!isAgent.value) paid.dataset_id = form.dataset_id
    let created
    if (isSingle.value) {
      const body = { ...paid, model_id: form.model_ids[0] }
      if (isRuler.value && !form.use_default) {
        // sample_limit 与逐维度配置保持一致，供后端兜底与详情页展示
        body.config = { ...form.config, sample_limit: form.config.sample_limits[dimensions.value[0].key] }
      }
      if (isAgent.value) body.agent = buildAgentPayload()
      created = await api.post('/evaluations', body)
    } else {
      const body = { ...paid, model_ids: form.model_ids }
      if (isAgent.value) body.agent = buildAgentPayload()
      created = await api.post('/evaluations/batch', body)
    }
    emit('created', created)
    emit('update:visible', false)
    ElMessage.success(isSingle.value ? '评测任务已创建' : `已提交 ${created.length} 个评测任务`)
  } finally {
    submitting.value = false
  }
}

function close() {
  emit('update:visible', false)
}
</script>
<template>
  <el-dialog :model-value="visible" title="发起评测任务" :width="isAgent ? 'min(780px,95vw)' : 'min(680px,95vw)'" @close="close">
    <el-form label-position="top">
      <el-form-item label="被测模型（可多选，最多 4 个；多选时并行对比）">
        <el-select v-model="form.model_ids" multiple :multiple-limit="4" placeholder="选择模型" style="width:100%">
          <el-option v-for="model in models" :key="model.id" :label="model.name" :value="model.id" />
        </el-select>
      </el-form-item>
      <el-form-item v-if="!isAgent" label="数据集">
        <el-select v-model="form.dataset_id" placeholder="选择数据集" style="width:100%">
          <el-option v-for="dataset in datasets" :key="dataset.id" :label="dataset.name" :value="dataset.id" />
        </el-select>
      </el-form-item>
      <div v-else class="notice">Agent 评测使用 benchmark 内置数据，无需选择数据集。</div>
      <el-form-item label="测评类型">
        <el-select v-model="form.benchmark" style="width:100%">
          <el-option-group label="真实评测 · 尺子">
            <el-option v-for="ruler in rulerList" :key="ruler.id" :label="`${ruler.label}（${ruler.scope}）`" :value="ruler.id" />
          </el-option-group>
          <el-option-group label="真实评测 · Agent">
            <el-option label="Agent 评测（需 docker）" value="Agent" />
          </el-option-group>
          <el-option-group label="模拟执行">
            <el-option v-for="other in ['VLM', 'AISF', 'ASR', 'MT', 'TTS']" :key="other" :label="`${other}（模拟执行）`" :value="other" />
          </el-option-group>
        </el-select>
      </el-form-item>
      <VisibilityField v-model="form.visibility" />
    </el-form>

    <template v-if="isRuler">
      <div class="card-header" style="margin:6px 0 12px">
        <h2>测评尺子</h2>
        <span class="muted small">点击卡片切换尺子</span>
      </div>
      <RulerPicker v-model="form.benchmark" :rulers="rulerList" />
      <el-collapse v-if="isSingle" class="section-gap">
        <el-collapse-item name="advanced">
          <template #title>
            <span>高级参数</span>
            <span class="muted small" style="margin-left:10px">
              {{ form.use_default
                ? `使用尺子默认参数（每维度 ${defaultLimit} 条 · 共 ${dimensions.length * defaultLimit} 条）`
                : `${limitsSummary} · 共 ${caseCount} 条用例` }}
            </span>
          </template>
          <el-form label-position="top">
            <el-form-item>
              <el-switch v-model="form.use_default" active-text="使用默认参数" inactive-text="自定义参数" />
            </el-form-item>
            <template v-if="!form.use_default">
              <el-form-item label="各维度取样条数（每条目 1-5，超出实际样例数时按实际条数执行）">
                <div class="limit-grid">
                  <div v-for="dim in dimensions" :key="dim.key" class="limit-row">
                    <span class="limit-label">
                      {{ dim.label }}<span class="muted small"> · 权重 {{ Math.round(dim.weight * 100) }}%</span>
                    </span>
                    <el-input-number v-model="form.config.sample_limits[dim.key]" :min="1" :max="5"
                                     size="small" controls-position="right" style="width:110px" />
                    <span class="muted small">条</span>
                  </div>
                </div>
                <div class="muted small" style="margin-top:8px">
                  当前共 {{ caseCount }} 条用例（{{ dimensions.length }} 个维度）
                </div>
              </el-form-item>
              <el-form-item>
                <el-checkbox v-model="form.config.sync_scores" :disabled="form.benchmark !== 'Ruler1'">
                  测评完成后回写模型榜单分数（仅尺子一测评生效）
                </el-checkbox>
              </el-form-item>
            </template>
          </el-form>
        </el-collapse-item>
      </el-collapse>
      <div v-else class="notice">多模型对比将使用默认参数（每维度 {{ selectedRuler?.default_sample_limit }} 条，不开启榜单回写）。</div>
    </template>

    <template v-else-if="isAgent">
      <div class="card-header" style="margin:6px 0 12px">
        <h2>Benchmark</h2>
        <span class="muted small">点击卡片选择</span>
      </div>
      <BenchmarkPicker v-model="form.agent.benchmark" :benchmarks="benchmarkList" />
      <div class="grid two section-gap">
        <el-form-item label="Harness">
          <el-select v-model="form.agent.harness" placeholder="选择 harness" style="width:100%">
            <el-option v-for="h in harnessList" :key="h.id" :label="`${h.label} · ${h.id}`" :value="h.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="Environment">
          <el-select v-model="form.agent.environment" style="width:100%">
            <el-option v-for="e in environmentList" :key="e.id" :label="e.label" :value="e.id" />
          </el-select>
        </el-form-item>
      </div>
      <el-form-item label="模型 API 协议">
        <el-select v-model="form.agent.model_api_protocol" style="width:100%">
          <el-option v-for="p in protocolList" :key="p.value" :label="p.label" :value="p.value" />
        </el-select>
      </el-form-item>
      <el-alert v-if="protocolMismatch" class="section-gap" type="warning" :closable="false"
                title="协议与所选 harness 的推荐协议不一致" description="协议需与你注册的模型端点匹配，否则可能运行失败。" />
      <div v-if="selectedHarness" class="notice" style="margin-top:12px">
        {{ selectedHarness.label }} 推荐协议：{{ selectedHarness.preferred_protocols.join(' / ') }}
      </div>
      <el-collapse class="section-gap">
        <el-collapse-item name="agent-advanced">
          <template #title>
            <span>高级参数</span>
            <span class="muted small" style="margin-left:10px">并发、尝试次数与重试（留空用默认值）</span>
          </template>
          <el-form label-position="top">
            <div class="grid three">
              <el-form-item label="并发任务数（task_concurrency）">
                <el-input-number v-model="form.agent.params.task_concurrency" :min="1" size="small" controls-position="right" style="width:100%" />
              </el-form-item>
              <el-form-item label="每任务最大尝试次数（k）">
                <el-input-number v-model="form.agent.params.k" :min="1" size="small" controls-position="right" style="width:100%" />
              </el-form-item>
              <el-form-item label="最大重试次数（max_retries）">
                <el-input-number v-model="form.agent.params.max_retries" :min="0" size="small" controls-position="right" style="width:100%" />
              </el-form-item>
            </div>
          </el-form>
        </el-collapse-item>
      </el-collapse>
    </template>
    <div v-else class="notice">该测评类型尚未接入真实测评引擎，任务提交后由模拟执行器推进，结果仅用于演示。</div>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">提交任务</el-button>
    </template>
  </el-dialog>
</template>
<style scoped>
.limit-grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); width: 100%; }
.limit-row { display: flex; align-items: center; gap: 10px; }
.limit-label { flex: 1; min-width: 0; }
</style>
