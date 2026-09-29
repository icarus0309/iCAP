<script setup>
import { onMounted, onUnmounted, ref, watch, nextTick } from 'vue'
import { init, use } from 'echarts/core'
import { BarChart, RadarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, RadarComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

use([BarChart, RadarChart, GridComponent, TooltipComponent, LegendComponent, RadarComponent, CanvasRenderer])

const props = defineProps({ option: { type: Object, required: true }, height: { type: String, default: '310px' } })
const element = ref(null)
let chart
let observer
onMounted(async () => {
  await nextTick()
  chart = init(element.value)
  chart.setOption(props.option)
  observer = new ResizeObserver(() => chart?.resize())
  observer.observe(element.value)
})
watch(() => props.option, (option) => chart?.setOption(option, true), { deep: true })
onUnmounted(() => { observer?.disconnect(); chart?.dispose() })
</script>
<template><div ref="element" :style="{ height, width: '100%' }" /></template>
