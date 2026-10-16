<script setup>
import { computed } from 'vue'

const props = defineProps({
  result: { type: Object, required: true },   // {total, summary, dimensions[], extra{}, finished_at}
})

const resolveScore = computed(() => {
  const dim = (props.result.dimensions || []).find((d) => d.key === 'resolve')
  return dim ? Math.round(dim.score * 100) : 0
})
const extra = computed(() => props.result.extra || {})
const fmt = (v) => (v === null || v === undefined || v === '' ? '—' : v)
</script>
<template>
  <div class="grid two">
    <section class="card card-pad">
      <div class="card-header">
        <h2>任务解决率</h2>
        <span class="chip">真实评测</span>
      </div>
      <div class="stat" style="min-height:0">
        <div class="stat-label">综合解决率</div>
        <div class="stat-number">{{ resolveScore }}%</div>
        <div class="stat-note">完成 {{ fmt(extra.evaluated) }}/{{ fmt(extra.total) }} 条任务</div>
      </div>
    </section>
    <section class="card card-pad">
      <div class="card-header"><h2>运行指标</h2></div>
      <el-descriptions :column="1" size="small">
        <el-descriptions-item label="平均时延">{{ fmt(extra.avg_latency_seconds) }} s</el-descriptions-item>
        <el-descriptions-item label="平均步数">{{ fmt(extra.avg_steps) }}</el-descriptions-item>
        <el-descriptions-item label="总 Token">{{ fmt(extra.total_tokens) }}</el-descriptions-item>
        <el-descriptions-item label="错误数">{{ fmt(extra.error) }}</el-descriptions-item>
      </el-descriptions>
      <div v-if="extra.run_dir" class="small muted section-gap">结果目录：<span class="mono">{{ extra.run_dir }}</span></div>
    </section>
  </div>
  <section class="card card-pad section-gap">
    <div class="card-header"><h2>评测摘要</h2></div>
    <p class="subtitle" style="white-space:pre-wrap">{{ result.summary }}</p>
    <div class="small muted">完成时间：{{ result.finished_at }}</div>
  </section>
</template>
