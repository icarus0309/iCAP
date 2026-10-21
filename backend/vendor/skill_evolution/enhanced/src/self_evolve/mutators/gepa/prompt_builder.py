"""GEPA prompt mutation builder – constructs and invokes the LLM for skill mutation."""

import time
from typing import Any


def mutate_prompt_with_reflection(
    gen_model_client: Any,
    parent_prompt: str,
    feedback_text: str,
) -> str:
    """Mutate a skill document using LLM with reflection feedback.

    Returns the mutated prompt text, or the original if mutation fails.
    """
    parent_len = len(parent_prompt)
    feedback_len = len(feedback_text)
    total_len = parent_len + feedback_len
    print(f"[输入统计] parent_prompt: {parent_len} 字符, feedback_text: {feedback_len} 字符, 总计: {total_len} 字符")

    system_prompt = """你是一个SKILL优化专家。你的任务是根据诊断信息优化SKILL文档，使其指导更清晰、更有效。

关键理解：
- 诊断信息中的"改进建议"是针对执行结果的观察（如"步骤太多""有重复"）
- 你需要把这些观察转化为SKILL文档中的指导（如"避免重复查询""合并相关步骤"）
- 改进的是SKILL对模型的指导，而不是改进输出格式

优化原则：
1. 必须完整保留YAML frontmatter（---之间的元数据）
2. 进行实质性修改（不是微调），确保修改后与原文档有显著差异
3. 严格禁止添加任何示例、案例或参考数据
4. 根据诊断信息改进SKILL指导：
   - 如果诊断说"缺少某个步骤"，在SKILL中明确要求这个步骤
   - 如果诊断说"有重复步骤"，在SKILL中明确指导如何避免重复
   - 如果诊断说"步骤太多"，在SKILL中明确指导如何合并或简化
5. 直接输出改进后的完整SKILL文档（必须以---开头），不要有任何解释"""

    user_prompt = f"""## 当前SKILL文档
{parent_prompt}

## 诊断信息与优化建议
{feedback_text}

## 优化要求
根据上述诊断信息，对SKILL文档进行改进。关键是把"执行中观察到的问题"转化为"SKILL中的指导"：

1. **分析问题根源**：诊断信息指出了什么问题（缺失步骤、重复、低效等）
2. **转化为指导**：在SKILL中添加或修改指导，使模型能避免这些问题
3. **确保完整性**：改进后的SKILL应该能指导模型完成所有必要步骤

例如：
- 如果诊断说"缺少工具调用"，SKILL应该明确要求这个工具调用
- 如果诊断说"有重复查询"，SKILL应该说"一次性获取所有信息，避免重复"
- 如果诊断说"步骤太多"，SKILL应该指导如何合并或简化步骤

直接输出改进后的完整SKILL文档："""

    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"[模型调用] 尝试 {attempt + 1}/{max_retries}")
            improved = gen_model_client.generate_with_system(system_prompt, user_prompt)
            print(f"[模型返回] 长度: {len(improved)} 字符")
            break
        except Exception as exc:
            print(f"[重试 {attempt + 1}/{max_retries}] 模型调用失败: {type(exc).__name__}: {str(exc)[:100]}")
            if attempt == max_retries - 1:
                print("模型调用失败，返回原文档")
                return parent_prompt
            time.sleep(2 ** attempt)

    # Clean and validate
    improved = _extract_skill_content(improved)
    print(f"[提取后] 长度: {len(improved)} 字符")

    if len(improved) < len(parent_prompt) * 0.5:
        print(f"改进后文档太短 ({len(improved)} < {len(parent_prompt) * 0.5:.0f})，返回原文档")
        return parent_prompt

    if len(improved) > len(parent_prompt) * 2:
        print(f"改进后文档太长 ({len(improved)} > {len(parent_prompt) * 2:.0f})，返回原文档")
        return parent_prompt

    # Check substantive change
    common_len = len(set(improved) & set(parent_prompt))
    total_unique = len(set(improved) | set(parent_prompt))
    diff_ratio = 1 - (common_len / total_unique) if total_unique > 0 else 0

    if diff_ratio < 0.05:
        print(f"改进后文档与原文档差异太小 (差异比例: {diff_ratio:.2%})，返回原文档")
        return parent_prompt

    print(f"[成功] 改进后文档长度: {len(improved)}, 差异比例: {diff_ratio:.2%}")
    return improved


def _extract_skill_content(text: str) -> str:
    """Extract the SKILL.md content from LLM output, removing prompt artifacts."""
    if not text or not isinstance(text, str):
        print("[警告] 提取内容失败：输入为空或非字符串类型")
        return ""

    text = text.strip()

    # Remove known prefixes
    prefixes = [
        "改进后的SKILL文档：", "改进后的SKILL文档:", "改进后的文档：", "改进后的文档:",
        "以下是改进后的SKILL文档：",
    ]
    for prefix in prefixes:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    # Find frontmatter start
    start_idx = text.find("---")
    if start_idx == -1:
        return text

    # Find the second --- for frontmatter end
    end_fm = text.find("---", start_idx + 3)
    if end_fm == -1:
        return text

    return text[start_idx:]
