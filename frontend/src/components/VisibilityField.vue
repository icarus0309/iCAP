<script setup>
defineProps({ modelValue: { type: String, default: 'private' } })
const emit = defineEmits(['update:modelValue'])
</script>

<template>
  <el-form-item label="可见范围">
    <el-radio-group class="visibility-choice" :model-value="modelValue" @change="value => emit('update:modelValue', value)">
      <el-radio-button value="private">仅自己可见</el-radio-button>
      <el-radio-button value="public">公开给所有用户</el-radio-button>
    </el-radio-group>
    <div class="visibility-hint">默认仅自己可见。公开后，平台中的其他用户也能查看。</div>
  </el-form-item>
</template>

<style scoped>
.visibility-choice { --el-color-primary: var(--red); }
.visibility-hint { width:100%; color:var(--muted); font-size:11px; line-height:1.5; margin-top:5px; }
</style>
