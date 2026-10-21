"""对比轨迹知识提取（迁移自 skill_distill_multi_dim）。

把「小模型结果失败、大模型结果成功」的两条轨迹按 key_step 对齐 → 逐 step 定位
首次关键分歧 → 复核 → 提取一条可执行的自然文本知识经验（非 patch/edit 模式）。

模块职责：
- ``key_steps_output_evaluator``: evo ``TrajectoryStep`` → distill evaluation trajectory 形状
- ``key_steps_judge``: 轨迹 → key_step 对齐 / key_steps 校验
- ``knowledge_prompts``: 首次分歧 / 复核 / 知识提取提示词
- ``knowledge_extractor``: 对齐 → 分歧 → 复核 → 自然文本知识 主流程
"""

from .knowledge_extractor import extract_case_knowledge

__all__ = ["extract_case_knowledge"]

from .direct_knowledge_extractor import extract_direct_knowledge
