<script setup>
defineProps({
  rulers: { type: Array, required: true },   // GET /api/rulers → rulers[]
  modelValue: { type: String, default: 'Ruler1' },
})
const emit = defineEmits(['update:modelValue'])
const percent = (weight) => `${Math.round((weight || 0) * 100)}%`
</script>
<template>
  <div class="grid two">
    <button
      v-for="ruler in rulers"
      :key="ruler.id"
      type="button"
      class="card card-pad ruler-card"
      :class="{ active: ruler.id === modelValue }"
      @click="emit('update:modelValue', ruler.id)"
    >
      <div class="flex-between">
        <strong class="ruler-title">{{ ruler.label }} · {{ ruler.scope }}</strong>
        <span class="chip">{{ ruler.available_cases }} 条样例</span>
      </div>
      <p class="subtitle" style="margin:10px 0 14px">{{ ruler.description }}</p>
      <div class="ruler-dims">
        <span v-for="dim in ruler.dimensions" :key="dim.key" class="ruler-dim">
          {{ dim.label }} {{ percent(dim.weight) }}
        </span>
      </div>
      <div class="small muted" style="margin-top:12px">
        默认每维度取样 {{ ruler.default_sample_limit }} 条 · {{ ruler.dimensions.length }} 个维度
      </div>
    </button>
  </div>
</template>
