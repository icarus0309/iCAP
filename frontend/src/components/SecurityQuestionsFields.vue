<script setup>
const props = defineProps({ modelValue: { type: Array, required: true } })
const emit = defineEmits(['update:modelValue'])

const questionOptions = [
  '你童年最喜欢的一本书叫什么？',
  '你第一次旅行去的城市是哪里？',
  '你小时候最喜欢的游戏是什么？',
  '你第一位好友的昵称是什么？',
  '你最喜欢的一道家常菜是什么？',
  '你曾经养过的第一只宠物叫什么？',
  '你小学最喜欢的一门课程是什么？',
  '你记忆中第一个参加的比赛是什么？',
]

function update(index, key, value) {
  const next = props.modelValue.map((item, position) => position === index ? { ...item, [key]: value } : { ...item })
  emit('update:modelValue', next)
}

function selectedElsewhere(question, index) {
  return props.modelValue.some((item, position) => position !== index && item.question === question)
}
</script>

<template>
  <div class="security-questions">
    <div class="section-heading">设置 3 个密保问题</div>
    <p class="question-note">忘记或修改密码时，需要答对其中至少 2 题。请为每题设置不同的私密短语，不要使用能从社交资料查到的真实答案，并妥善保存。</p>
    <div v-for="(item, index) in modelValue" :key="index" class="question-item">
      <el-form-item :label="`问题 ${index + 1}`">
        <el-select :model-value="item.question" :placeholder="`选择第 ${index + 1} 个问题`" size="large" @update:model-value="update(index, 'question', $event)">
          <el-option v-for="question in questionOptions" :key="question" :label="question" :value="question" :disabled="selectedElsewhere(question, index)" />
        </el-select>
      </el-form-item>
      <el-form-item :label="`答案 ${index + 1}`">
        <el-input :model-value="item.answer" type="password" show-password autocomplete="off" maxlength="128" placeholder="请输入答案" size="large" @update:model-value="update(index, 'answer', $event)" />
      </el-form-item>
    </div>
  </div>
</template>

<style scoped>
.section-heading { font-size: 16px; color: #283244; font-weight: 750; margin: 25px 0 8px; }
.question-note { color: #748095; font-size: 12px; line-height: 1.6; margin: 0 0 17px; }
.question-item { border-top: 1px solid #edf0f4; padding-top: 15px; }
.question-item :deep(.el-select) { width: 100%; }
</style>
