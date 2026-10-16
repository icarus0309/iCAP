<script setup>
defineProps({
  benchmarks: { type: Array, required: true },   // capabilities.benchmarks[]
  modelValue: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])
</script>
<template>
  <div class="grid two">
    <button
      v-for="bench in benchmarks"
      :key="bench.id"
      type="button"
      class="card card-pad ruler-card"
      :class="{ active: bench.id === modelValue }"
      @click="emit('update:modelValue', bench.id)"
    >
      <div class="flex-between">
        <strong class="ruler-title">{{ bench.label }}</strong>
        <span class="muted small mono">{{ bench.id }}</span>
      </div>
      <p class="subtitle" style="margin:10px 0 14px">{{ bench.description }}</p>
      <div class="ruler-dims">
        <span v-for="h in bench.suggested_harnesses" :key="h" class="ruler-dim">建议 {{ h }}</span>
      </div>
    </button>
  </div>
</template>
