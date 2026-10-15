<script setup>
defineProps({
  cases: { type: Array, required: true },   // task.result.cases
})

const verdictType = { pass: 'success', partial: 'warning', fail: 'danger' }
const verdictText = { pass: '通过', partial: '部分', fail: '未通过' }
</script>
<template>
  <section class="card card-pad section-gap">
    <div class="card-header">
      <h2>用例明细</h2>
      <span class="muted small">{{ cases.length }} 条 · 判分为静态规则，不执行模型生成的代码</span>
    </div>
    <el-table :data="cases" empty-text="任务完成即生成用例明细" max-height="560">
      <el-table-column label="#" width="56">
        <template #default="{ $index }">{{ $index + 1 }}</template>
      </el-table-column>
      <el-table-column prop="dimension" label="维度" width="110" />
      <el-table-column prop="type" label="类型" width="86" />
      <el-table-column label="提示" min-width="200">
        <template #default="{ row }">
          <el-tooltip :content="row.prompt" placement="top" :show-after="300">
            <span class="clip">{{ row.prompt }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="模型回答" min-width="200">
        <template #default="{ row }">
          <el-tooltip :content="row.answer" placement="top" :show-after="300">
            <span class="clip">{{ row.answer }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="参考答案" min-width="170">
        <template #default="{ row }">
          <el-tooltip :content="row.reference" placement="top" :show-after="300">
            <span class="clip muted">{{ row.reference }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="判定" width="96">
        <template #default="{ row }">
          <el-tag size="small" :type="verdictType[row.verdict] || 'info'">{{ verdictText[row.verdict] || row.verdict }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="得分" width="90">
        <template #default="{ row }">{{ row.points }} / {{ row.max_points }}</template>
      </el-table-column>
    </el-table>
  </section>
</template>
<style scoped>
.clip { display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; }
</style>
