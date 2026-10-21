"""GEPA feedback collection: statistics, experience summary, reflection text."""

from typing import Any, Dict, List, Optional, Tuple

from .candidate import DataInstance, PromptCandidate


def collect_statistics(
    parent: PromptCandidate, instances: List[DataInstance]
) -> Dict[str, Any]:
    """Collect aggregate statistics from all case evaluation results."""
    stats: Dict[str, Any] = {
        "total_cases": len(instances),
        "failed_steps": {},
        "repeated_steps": {},
        "unnecessary_steps": {},
        "missing_steps": {},
        "trial_and_error_frequency": 0,
        "avg_step_count": 0,
        "avg_conciseness": 0.0,
        "avg_process": 0.0,
        "avg_result": 0.0,
    }

    step_counts: List[int] = []
    conciseness_scores: List[float] = []
    process_scores: List[float] = []
    result_scores: List[float] = []

    for instance in instances:
        if str(instance.id) not in parent.eval_results:
            continue
        eval_result = parent.eval_results[str(instance.id)]
        side_info = eval_result.side_info

        if not hasattr(side_info, "diagnostics") or not side_info.diagnostics:
            continue
        diagnostics = side_info.diagnostics.get("self_evolve_score", {})
        if not diagnostics:
            continue

        analysis_details = diagnostics.get("details", {}).get("analysis_details", {})

        # 过程（原始维度分，而非加权后的 sub_type_scores）
        process_details = analysis_details.get("process_details", {})
        if process_details:
            process_scores.append(diagnostics.get("process", 0))
            for step in process_details.get("missing_steps", []):
                step_id = step if isinstance(step, str) else str(step)
                if ":" in step_id:
                    step_id = step_id.split(":")[0]
                stats["missing_steps"][step_id] = stats["missing_steps"].get(step_id, 0) + 1

        # 精简
        conciseness_details = analysis_details.get("conciseness_details", {})
        if conciseness_details:
            conciseness_scores.append(diagnostics.get("conciseness", 0))
            step_counts.append(conciseness_details.get("step_count", 0))
            for idx in conciseness_details.get("repeated_step_indices", []):
                stats["repeated_steps"][f"step_{idx}"] = stats["repeated_steps"].get(f"step_{idx}", 0) + 1
            for idx in conciseness_details.get("unnecessary_step_indices", []):
                stats["unnecessary_steps"][f"step_{idx}"] = stats["unnecessary_steps"].get(f"step_{idx}", 0) + 1

        # 结果
        output_analyses = analysis_details.get("output_analyses", {})
        if output_analyses:
            result_scores.append(diagnostics.get("result", 0))

    if step_counts:
        stats["avg_step_count"] = sum(step_counts) / len(step_counts)
    if conciseness_scores:
        stats["avg_conciseness"] = sum(conciseness_scores) / len(conciseness_scores)
    if process_scores:
        stats["avg_process"] = sum(process_scores) / len(process_scores)
    if result_scores:
        stats["avg_result"] = sum(result_scores) / len(result_scores)

    return stats


def identify_high_frequency_issues(
    stats: Dict[str, Any], threshold: float = 0.3
) -> Dict[str, List[Tuple[str, float]]]:
    """Identify issues that appear in more than `threshold` proportion of cases."""
    high_freq: Dict[str, List[Tuple[str, float]]] = {
        "missing_steps": [],
        "repeated_steps": [],
        "unnecessary_steps": [],
    }
    total = stats["total_cases"]
    if total == 0:
        return high_freq
    for key in ("missing_steps", "repeated_steps", "unnecessary_steps"):
        for step_id, count in stats[key].items():
            freq = count / total
            if freq > threshold:
                high_freq[key].append((step_id, freq))
    return high_freq


def format_trajectory(trajectory: List[Any], max_steps: int = 5) -> str:
    """Format a trajectory list into readable text."""
    if not trajectory:
        return ""
    lines = []
    for i, step in enumerate(trajectory[:max_steps], 1):
        role = getattr(step, "role", "unknown")
        action_type = getattr(step, "action_type", "unknown")
        tool_name = getattr(step, "tool_name", "")
        content = getattr(step, "content", "")
        if isinstance(content, str) and len(content) > 100:
            content = content[:100] + "..."
        step_text = f"步骤{i}: [{role}] {action_type}"
        if tool_name:
            step_text += f" ({tool_name})"
        if content:
            step_text += f" - {content}"
        lines.append(step_text)
    if len(trajectory) > max_steps:
        lines.append(f"... 还有 {len(trajectory) - max_steps} 步")
    return "\n".join(lines)


def collect_issue_examples(
    parent: PromptCandidate,
    instances: List[DataInstance],
    high_freq_issues: Dict[str, List[Tuple[str, float]]],
    max_examples: int = 2,
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Collect concrete examples for each high-frequency issue."""
    issue_examples: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        "missing_steps": {},
        "repeated_steps": {},
        "unnecessary_steps": {},
    }

    for instance in instances:
        if str(instance.id) not in parent.eval_results:
            continue
        eval_result = parent.eval_results[str(instance.id)]
        side_info = eval_result.side_info
        actual_output = eval_result.output
        if isinstance(actual_output, dict):
            actual_output = actual_output.get("output", "")
        if not isinstance(actual_output, str):
            actual_output = str(actual_output)

        if not hasattr(side_info, "diagnostics") or not side_info.diagnostics:
            continue
        diagnostics = side_info.diagnostics.get("self_evolve_score", {})
        if not diagnostics:
            continue

        analysis_details = diagnostics.get("details", {}).get("analysis_details", {})
        trajectory = side_info.diagnostics.get("trajectory", [])
        trajectory_text = format_trajectory(trajectory) if trajectory else ""

        # --- missing steps ---
        process_details = analysis_details.get("process_details", {})
        if process_details:
            missing_steps = process_details.get("missing_steps", [])
            for step_id, _freq in high_freq_issues["missing_steps"]:
                found = any(
                    step_id in (s if isinstance(s, str) else str(s))
                    for s in missing_steps
                )
                if found:
                    issue_examples["missing_steps"].setdefault(step_id, [])
                    if len(issue_examples["missing_steps"][step_id]) < max_examples:
                        issue_examples["missing_steps"][step_id].append({
                            "instance_id": instance.id,
                            "input": instance.input[:200],
                            "expected_output": instance.expected_output[:200],
                            "actual_output": actual_output[:200],
                            "trajectory": trajectory_text,
                            "analysis": process_details.get("analysis", ""),
                            "optimization_plan": analysis_details.get("conciseness_details", {}).get("optimization_plan", ""),
                        })

        # --- repeated / unnecessary steps ---
        conciseness_details = analysis_details.get("conciseness_details", {})
        if conciseness_details:
            for key, indices_attr in [
                ("repeated_steps", "repeated_step_indices"),
                ("unnecessary_steps", "unnecessary_step_indices"),
            ]:
                for step_id, _freq in high_freq_issues[key]:
                    indices = conciseness_details.get(indices_attr, [])
                    if not isinstance(indices, list):
                        indices = [indices] if indices else []
                    found = any(str(idx) in step_id for idx in indices)
                    if found:
                        issue_examples[key].setdefault(step_id, [])
                        if len(issue_examples[key][step_id]) < max_examples:
                            issue_examples[key][step_id].append({
                                "instance_id": instance.id,
                                "input": instance.input[:200],
                                "expected_output": instance.expected_output[:200],
                                "actual_output": actual_output[:200],
                                "trajectory": trajectory_text,
                                "analysis": conciseness_details.get("analysis", ""),
                                "optimization_plan": conciseness_details.get("optimization_plan", ""),
                            })

    return issue_examples


def collect_experience_summary(
    gen_model_client: Any,
    parent: PromptCandidate,
    instances: List[DataInstance],
) -> str:
    """Generate a model-based experience summary from all case results."""
    if not gen_model_client:
        return ""

    if any((i.metadata or {}).get("execution_mode") == "direct" for i in instances):
        return collect_feedback_for_reflection(parent, instances)
    stats = collect_statistics(parent, instances)
    high_freq = identify_high_frequency_issues(stats, threshold=0.3)
    issue_examples = collect_issue_examples(parent, instances, high_freq)

    # Build stats text
    stats_text = f"""## 性能指标（基于 {stats['total_cases']} 个案例）
- 平均过程: {stats['avg_process']:.1f}
- 平均精简: {stats['avg_conciseness']:.1f}
- 平均结果: {stats['avg_result']:.1f}
- 平均步骤数: {stats['avg_step_count']:.1f}

## 高频问题（占比 > 30%）
"""
    if high_freq["missing_steps"]:
        stats_text += "**缺失步骤:**\n"
        for step_id, freq in high_freq["missing_steps"]:
            stats_text += f"- {step_id}: {freq*100:.0f}%\n"
        stats_text += "\n"
    if high_freq["repeated_steps"]:
        stats_text += "**重复步骤:**\n"
        for step_id, freq in high_freq["repeated_steps"]:
            stats_text += f"- {step_id}: {freq*100:.0f}%\n"
        stats_text += "\n"
    if high_freq["unnecessary_steps"]:
        stats_text += "**不必要步骤:**\n"
        for step_id, freq in high_freq["unnecessary_steps"]:
            stats_text += f"- {step_id}: {freq*100:.0f}%\n"
        stats_text += "\n"

    # Build examples text
    examples_text = "## 实际案例分析\n\n"
    labels = [
        ("missing_steps", "Missing Steps"),
        ("repeated_steps", "Repeated Steps"),
        ("unnecessary_steps", "Unnecessary Steps"),
    ]
    for key, title in labels:
        issues = issue_examples[key]
        if not issues:
            continue
        examples_text += f"### {title}\n\n"
        for step_id, ex_list in issues.items():
            examples_text += f"**{step_id}:**\n"
            for i, ex in enumerate(ex_list, 1):
                examples_text += f"\n案例 {i}:\n"
                examples_text += f"- 输入: {ex['input']}\n"
                examples_text += f"- 期望输出: {ex['expected_output']}\n"
                examples_text += f"- 实际输出: {ex['actual_output']}\n"
                if ex.get("trajectory"):
                    examples_text += f"- 执行轨迹:\n{ex['trajectory']}\n"
                examples_text += f"- 分析: {ex.get('analysis', '')}\n"
                if ex.get("optimization_plan"):
                    examples_text += f"- 优化方案: {ex['optimization_plan']}\n"
            examples_text += "\n"

    # Call model
    model_prompt = f"""你是一个经验总结专家。基于以下统计数据和实际案例的执行轨迹，分析当前SKILL的主要问题，并给出具体的改进建议。

## 统计数据（用作方向判断）
{stats_text}

## 实际案例分析（包含执行轨迹）
{examples_text}

## 任务要求
请根据上述信息，生成一个具体的经验汇总，包括：

1. **主要问题分析**：基于高频问题和实际轨迹，分析当前SKILL的主要问题
2. **根本原因**：这些问题的根本原因是什么（例如：SKILL中缺少某个指导、指导不够清晰等）
3. **具体改进建议**：给出真实具体的解决办法，包括：
   - 具体使用什么方式或工具来解决问题
   - 在SKILL中应该如何指导模型
   - 具体的步骤或流程改进

例如，不要说"改进查询步骤"，而要说"使用XXX工具一次性获取所有信息，避免多次查询"。

请输出简洁的经验汇总（不超过 300 字），重点放在具体可操作的改进建议上。只输出汇总内容，不要有任何前缀或解释。"""

    try:
        summary = gen_model_client.generate([{"role": "user", "content": model_prompt}])
        return f"{stats_text}\n{examples_text}\n### 模型分析\n{summary}"
    except Exception as exc:
        print(f"[警告] 生成经验汇总失败: {exc}")
        return f"{stats_text}\n{examples_text}"


def collect_feedback_for_reflection(
    parent: PromptCandidate, minibatch: List[DataInstance]
) -> str:
    """Collect diagnosis + multi-dimension score feedback for a minibatch."""
    feedback_text = "## 参考数据（从反馈集采样）\n\n"

    for i, instance in enumerate(minibatch, 1):
        sub_type = "default"
        if instance.metadata and "sub_type" in instance.metadata:
            sub_type = instance.metadata["sub_type"]

        feedback_text += f"### 案例 {i} (ID: {instance.id}, 类型: {sub_type})\n"
        feedback_text += f"**输入:** {instance.input}\n\n"
        feedback_text += f"**期望输出:** {instance.expected_output}\n\n"

        if str(instance.id) in parent.eval_results:
            eval_result = parent.eval_results[str(instance.id)]
            side_info = eval_result.side_info

            if hasattr(side_info, "feedback") and side_info.feedback:
                feedback_text += f"**诊断:** {side_info.feedback}\n\n"

            if hasattr(side_info, "diagnostics") and side_info.diagnostics:
                diagnostics = side_info.diagnostics.get("self_evolve_score", {})
                if diagnostics:
                    if diagnostics.get("execution_mode") == "direct":
                        feedback_text += f"**结果分数:** {diagnostics.get('result')}\n"
                        feedback_text += str(diagnostics.get("details", {}).get("analysis_details", {}).get("output_analyses", {})) + "\n"
                        continue
                    sub_type_scores = diagnostics.get("sub_type_scores", {})
                    if sub_type_scores:
                        scores_text = "、".join(
                            f"{k}={v:.1f}" for k, v in sub_type_scores.items()
                        )
                        feedback_text += f"**多维度分数:** {scores_text}\n\n"

                    analysis_details = diagnostics.get("details", {}).get("analysis_details", {})

                    # 过程
                    process_details = analysis_details.get("process_details", {})
                    if process_details:
                        feedback_text += "**过程分析:**\n"
                        missing_steps = process_details.get("missing_steps", [])
                        missing_strs = []
                        for step in missing_steps[:2]:
                            if isinstance(step, str):
                                missing_strs.append(step)
                            elif isinstance(step, dict):
                                missing_strs.append(str(step))
                        if missing_strs:
                            feedback_text += f"- 缺失步骤: {'; '.join(missing_strs)}\n"
                        if process_details.get("analysis"):
                            feedback_text += f"- 分析: {process_details['analysis']}\n"
                        feedback_text += "\n"

                    # 精简
                    conciseness_details = analysis_details.get("conciseness_details", {})
                    if conciseness_details:
                        feedback_text += "**精简分析:**\n"
                        if conciseness_details.get("repeated_step_indices"):
                            feedback_text += f"- 重复步骤: {conciseness_details['repeated_step_indices']}\n"
                        if conciseness_details.get("unnecessary_step_indices"):
                            feedback_text += f"- 不必要步骤: {conciseness_details['unnecessary_step_indices']}\n"
                        if conciseness_details.get("optimization_plan"):
                            feedback_text += f"- 优化方案: {conciseness_details['optimization_plan']}\n"
                        if conciseness_details.get("analysis"):
                            feedback_text += f"- 分析: {conciseness_details['analysis']}\n"
                        feedback_text += "\n"

                    # 结果（按子类型 text/tool/file）
                    output_analyses = analysis_details.get("output_analyses", {})
                    if output_analyses:
                        feedback_text += "**结果分析:**\n"
                        for otype, odetails in output_analyses.items():
                            if odetails.get("analysis"):
                                feedback_text += f"- [{otype}] {odetails['analysis']}\n"
                        feedback_text += "\n"

                    # Overall suggestions
                    self_evolve_score = side_info.diagnostics.get("self_evolve_score", {})
                    overall = self_evolve_score.get("dimension_suggestions", {}).get("overall", [])
                    if overall and len(overall) > 0:
                        feedback_text += f"**综合优化建议:**\n{overall[0]}\n\n"

        feedback_text += "---\n\n"

    return feedback_text
