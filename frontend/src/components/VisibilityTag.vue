<script setup>
defineProps({ visibility: { type: String, default: '' } })
</script>

<template>
  <el-tag v-if="visibility === 'public' || visibility === 'private'" size="small" :type="visibility === 'public' ? 'success' : 'info'">
    {{ visibility === 'public' ? '公开' : '仅自己可见' }}
  </el-tag>
</template>
