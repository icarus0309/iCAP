<script setup>
import { computed } from 'vue'
import deepseekLogo from '../assets/deepseek.svg'

const props = defineProps({ model: { type: Object, required: true } })
const source = computed(() => props.model.logo_url ||
  (props.model.provider?.toLowerCase() === 'deepseek' ? deepseekLogo : ''))
const initials = computed(() => (props.model.provider || props.model.name || 'AI').slice(0, 2).toUpperCase())
</script>

<template>
  <span class="model-logo">
    <img v-if="source" :src="source" :alt="`${model.provider} Logo`" />
    <span v-else>{{ initials }}</span>
  </span>
</template>

<style scoped>
.model-logo { display:inline-flex; align-items:center; justify-content:center; flex:none; width:100px; height:44px; padding:5px; border-radius:10px; background:#f4f6fa; color:#40506b; font-size:16px; font-weight:800; }
.model-logo img { max-width:100%; max-height:100%; object-fit:contain; }
</style>
