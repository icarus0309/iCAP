<script setup>
import { computed } from 'vue'
import EChart from '../EChart.vue'

const props = defineProps({
  result: { type: Object, required: true },   // {total, dimensions[], cases[], summary, mode, finished_at}
  rulerLabel: { type: String, default: '' },
})

const radarOption = computed(() => ({
  tooltip: {},
  radar: {
    indicator: props.result.dimensions.map((dim) => ({ name: dim.label, max: 100 })),
    radius: '62%',
  },
  series: [{
    type: 'radar',
    data: [{
      value: props.result.dimensions.map((dim) => dim.score),
      name: '本模型',
      areaStyle: { color: 'rgba(201,30,50,.22)' },
      lineStyle: { color: '#c91e32' },
      itemStyle: { color: '#c91e32' },
    }],
  }],
}))
const passRate = (dim) => (dim.case_total ? Math.round((dim.case_passed / dim.case_total) * 100) : 0)
</script>
<template>
  <div class="grid two">
    <section class="card card-pad">
      <div class="card-header">
        <h2>{{ rulerLabel || '测评结果' }}</h2>
        <span class="chip">{{ result.mode === 'live' ? '真实评测' : '演示模式' }}</span>
      </div>
      <div class="stat" style="min-height:0">
        <div class="stat-label">综合得分</div>
        <div class="stat-number">{{ result.total }}</div>
        <div class="stat-note">满分 100 · 完成 {{ result.cases.length }} 条用例</div>
      </div>
      <div v-for="dim in result.dimensions" :key="dim.key" class="dim-row">
        <div class="flex-between small">
          <span>{{ dim.label }}<span class="muted"> · 权重 {{ Math.round(dim.weight * 100) }}%</span></span>
          <span><strong>{{ dim.score }}</strong> <span class="muted">（{{ dim.case_passed }}/{{ dim.case_total }} 通过 · {{ passRate(dim) }}%）</span></span>
        </div>
        <el-progress :percentage="dim.score" :stroke-width="7" :show-text="false" />
      </div>
    </section>
    <section class="card card-pad">
      <div class="card-header"><h2>能力雷达图</h2><span class="muted small">5 个维度，满分 100</span></div>
      <EChart :option="radarOption" height="320px" />
      <p class="subtitle section-gap" style="line-height:1.9">{{ result.summary }}</p>
      <div class="small muted">完成时间：{{ result.finished_at }}</div>
    </section>
  </div>
</template>
<style scoped>
.dim-row { margin: 14px 0; display: grid; gap: 6px; }
</style>
