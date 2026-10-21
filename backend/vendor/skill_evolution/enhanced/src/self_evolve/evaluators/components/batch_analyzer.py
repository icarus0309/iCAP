"""Batch analyzer – generates global experience summary from all case results."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from ....model_client import ModelClient
from ...schemas import ScoreBreakdown, SessionAnalysisResult, TrajectoryStep


class BatchAnalyzer:
    """Analyzes all case results holistically and generates a global experience summary.

    Adapted from GEPAOptimizer.collect_experience_summary(), but works directly
    with ScoreBreakdown / SessionAnalysisResult types instead of PromptCandidate internals.
    """

    def __init__(self, model_client: Optional[ModelClient] = None):
        self.model_client = model_client

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def analyze(
        self,
        *,
        session_results: Dict[str, SessionAnalysisResult],
        score_breakdowns: Dict[str, ScoreBreakdown],
        trajectories: Dict[str, List[TrajectoryStep]],
    ) -> str:
        """Generate a global experience summary across all cases.

        Returns an empty string when no model_client is available.
        """
        if not self.model_client:
            return ""

        if any(s.execution_mode == "direct" for s in score_breakdowns.values()):
            examples = [_make_example(cid, sb, session_results[cid], "")
                        for cid, sb in score_breakdowns.items() if cid in session_results and not sb.evaluation_error]
            return self.model_client.generate([{"role": "user", "content":
                "仅根据输入、本次最终输出和 judge 原文提炼可迁移判断规则、错误模式及适用边界。"
                "证据不足不作结论，不推测内部推理，不虚构工具步骤，不复制个案答案。\n" +
                json.dumps(examples, ensure_ascii=False)}]) if examples else ""
        stats = self._collect_statistics(score_breakdowns, session_results)
        high_freq = self._identify_high_frequency_issues(stats, threshold=0.3)
        examples = self._collect_issue_examples(
            score_breakdowns, session_results, trajectories, high_freq
        )
        stats_text = self._build_stats_text(stats, high_freq)
        examples_text = self._build_examples_text(examples)
        summary = self._call_model(stats_text, examples_text)
        return f"{stats_text}\n{examples_text}\n### 模型分析\n{summary}"

    # ------------------------------------------------------------------
    # Statistics collection
    # ------------------------------------------------------------------

    def _collect_statistics(
        self,
        score_breakdowns: Dict[str, ScoreBreakdown],
        session_results: Dict[str, SessionAnalysisResult],
    ) -> Dict[str, Any]:
        stats: Dict[str, Any] = {
            "total_cases": len(score_breakdowns),
            "missing_steps": {},
            "repeated_steps": {},
            "unnecessary_steps": {},
            "avg_step_count": 0,
            "avg_conciseness": 0.0,
            "avg_process": 0.0,
            "avg_result": 0.0,
        }

        step_counts: List[int] = []
        conciseness_scores: List[float] = []
        process_scores: List[float] = []
        result_scores: List[float] = []

        for case_id, sb in score_breakdowns.items():
            sr = session_results.get(case_id)
            if sr is None:
                continue

            # Aggregate scores
            process_scores.append(sb.process)
            conciseness_scores.append(sb.conciseness)
            result_scores.append(sb.result)

            # Process details
            pd = sr.process_details
            if pd:
                missing_steps = pd.get("missing_steps", [])
                for step in missing_steps:
                    step_id = step if isinstance(step, str) else str(step)
                    if ":" in step_id:
                        step_id = step_id.split(":")[0]
                    stats["missing_steps"][step_id] = stats["missing_steps"].get(step_id, 0) + 1

            # Conciseness details
            cd = sr.conciseness_details
            if cd:
                step_counts.append(cd.get("step_count", 0))
                for idx in cd.get("repeated_step_indices", []):
                    stats["repeated_steps"][f"step_{idx}"] = stats["repeated_steps"].get(f"step_{idx}", 0) + 1
                for idx in cd.get("unnecessary_step_indices", []):
                    stats["unnecessary_steps"][f"step_{idx}"] = stats["unnecessary_steps"].get(f"step_{idx}", 0) + 1

        if step_counts:
            stats["avg_step_count"] = sum(step_counts) / len(step_counts)
        if conciseness_scores:
            stats["avg_conciseness"] = sum(conciseness_scores) / len(conciseness_scores)
        if process_scores:
            stats["avg_process"] = sum(process_scores) / len(process_scores)
        if result_scores:
            stats["avg_result"] = sum(result_scores) / len(result_scores)

        return stats

    # ------------------------------------------------------------------
    # High-frequency issue detection
    # ------------------------------------------------------------------

    def _identify_high_frequency_issues(
        self, stats: Dict[str, Any], threshold: float = 0.3
    ) -> Dict[str, List[Tuple[str, float]]]:
        high_freq: Dict[str, List[Tuple[str, float]]] = {
            "missing_steps": [],
            "repeated_steps": [],
            "unnecessary_steps": [],
        }
        total = stats["total_cases"]
        if total == 0:
            return high_freq
        for key, dest in [
            ("missing_steps", "missing_steps"),
            ("repeated_steps", "repeated_steps"),
            ("unnecessary_steps", "unnecessary_steps"),
        ]:
            for step_id, count in stats[key].items():
                freq = count / total
                if freq > threshold:
                    high_freq[dest].append((step_id, freq))
        return high_freq

    # ------------------------------------------------------------------
    # Concrete example collection
    # ------------------------------------------------------------------

    def _collect_issue_examples(
        self,
        score_breakdowns: Dict[str, ScoreBreakdown],
        session_results: Dict[str, SessionAnalysisResult],
        trajectories: Dict[str, List[TrajectoryStep]],
        high_freq: Dict[str, List[Tuple[str, float]]],
        max_examples: int = 2,
    ) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        issue_examples: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
            "missing_steps": {},
            "repeated_steps": {},
            "unnecessary_steps": {},
        }

        for case_id, sb in score_breakdowns.items():
            sr = session_results.get(case_id)
            if sr is None:
                continue
            traj_text = self._format_trajectory(trajectories.get(case_id, []))

            # --- missing steps ---
            for step_id, _freq in high_freq["missing_steps"]:
                missing_steps = sr.process_details.get("missing_steps", [])
                found = any(
                    step_id in (s if isinstance(s, str) else str(s))
                    for s in missing_steps
                )
                if found:
                    if step_id not in issue_examples["missing_steps"]:
                        issue_examples["missing_steps"][step_id] = []
                    if len(issue_examples["missing_steps"][step_id]) < max_examples:
                        issue_examples["missing_steps"][step_id].append(
                            _make_example(case_id, sb, sr, traj_text)
                        )

            # --- repeated / unnecessary steps ---
            cd = sr.conciseness_details
            if not cd:
                continue
            for key, indices_attr in [
                ("repeated_steps", "repeated_step_indices"),
                ("unnecessary_steps", "unnecessary_step_indices"),
            ]:
                for step_id, _freq in high_freq[key]:
                    indices = cd.get(indices_attr, [])
                    if not isinstance(indices, list):
                        indices = [indices] if indices else []
                    found = any(str(idx) in step_id for idx in indices)
                    if found:
                        if step_id not in issue_examples[key]:
                            issue_examples[key][step_id] = []
                        if len(issue_examples[key][step_id]) < max_examples:
                            issue_examples[key][step_id].append(
                                _make_example(case_id, sb, sr, traj_text)
                            )

        return issue_examples

    # ------------------------------------------------------------------
    # Text builders
    # ------------------------------------------------------------------

    def _build_stats_text(
        self,
        stats: Dict[str, Any],
        high_freq: Dict[str, List[Tuple[str, float]]],
    ) -> str:
        text = f"""## 性能指标（基于 {stats['total_cases']} 个案例）
- 平均过程: {stats['avg_process']:.1f}
- 平均精简: {stats['avg_conciseness']:.1f}
- 平均结果: {stats['avg_result']:.1f}
- 平均步骤数: {stats['avg_step_count']:.1f}

## 高频问题（占比 > 30%）
"""
        labels = [
            ("missing_steps", "**缺失步骤:**\n"),
            ("repeated_steps", "**重复步骤:**\n"),
            ("unnecessary_steps", "**不必要步骤:**\n"),
        ]
        for key, header in labels:
            if high_freq[key]:
                text += header
                for step_id, freq in high_freq[key]:
                    text += f"- {step_id}: {freq*100:.0f}%\n"
                text += "\n"
        return text

    def _build_examples_text(
        self,
        issue_examples: Dict[str, Dict[str, List[Dict[str, Any]]]],
    ) -> str:
        text = "## 实际案例分析\n\n"
        labels = [
            ("missing_steps", "Missing Steps"),
            ("repeated_steps", "Repeated Steps"),
            ("unnecessary_steps", "Unnecessary Steps"),
        ]
        for key, title in labels:
            issues = issue_examples[key]
            if not issues:
                continue
            text += f"### {title}\n\n"
            for step_id, examples in issues.items():
                text += f"**{step_id}:**\n"
                for i, ex in enumerate(examples, 1):
                    scores = ex.get("scores", {})
                    text += f"\n案例 {i} (案例ID: {ex['instance_id']}):\n"
                    text += f"- 分数: process={scores.get('process', 0):.1f}, "
                    text += f"conciseness={scores.get('conciseness', 0):.1f}, "
                    text += f"result={scores.get('result', 0):.1f}\n"
                    if ex.get("trajectory"):
                        text += f"- 执行轨迹:\n{ex['trajectory']}\n"
                    proc = ex.get("process_analysis", "")
                    conc = ex.get("conciseness_analysis", "")
                    if proc:
                        text += f"- 过程分析: {proc}\n"
                    if conc:
                        text += f"- 精简分析: {conc}\n"
                    if ex.get("optimization_plan"):
                        text += f"- 优化方案: {ex['optimization_plan']}\n"
                text += "\n"
        return text

    # ------------------------------------------------------------------
    # Model call
    # ------------------------------------------------------------------

    def _call_model(self, stats_text: str, examples_text: str) -> str:
        if not self.model_client:
            return ""

        prompt = f"""你是一个经验总结专家。基于以下统计数据和实际案例的执行轨迹，分析当前SKILL的主要问题，并给出具体的改进建议。

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
            return self.model_client.generate([{"role": "user", "content": prompt}])
        except Exception as exc:
            print(f"[警告] 生成经验汇总失败: {exc}")
            return ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _format_trajectory(self, trajectory: List[TrajectoryStep], max_steps: int = 5) -> str:
        if not trajectory:
            return ""
        lines = []
        for i, step in enumerate(trajectory[:max_steps], 1):
            content = step.content or ""
            if len(content) > 100:
                content = content[:100] + "..."
            step_text = f"步骤{i}: [{step.role}] {step.action_type}"
            if step.tool_name:
                step_text += f" ({step.tool_name})"
            if content:
                step_text += f" - {content}"
            lines.append(step_text)
        if len(trajectory) > max_steps:
            lines.append(f"... 还有 {len(trajectory) - max_steps} 步")
        return "\n".join(lines)


def _make_example(
    case_id: str,
    sb: ScoreBreakdown,
    sr: SessionAnalysisResult,
    traj_text: str,
) -> Dict[str, Any]:
    if sb.execution_mode == "direct":
        return {"instance_id": case_id, "scores": {"result": sb.result}, "input_messages": sr.input_messages,
                "final_output": sr.final_output, "judge": sr.output_analyses, "feedback": sb.feedback}
    # 过程分析来自 process_details
    process_analysis = sr.process_details.get("analysis", "")
    # 精简分析来自 conciseness_details
    conciseness_analysis = sr.conciseness_details.get("analysis", "")
    optimization_plan = sr.conciseness_details.get("optimization_plan", "")
    # 结果分析来自 output_analyses
    result_analysis = "; ".join(
        f"[{otype}] {d.get('analysis', '')}"
        for otype, d in sr.output_analyses.items()
        if d.get("analysis")
    )

    return {
        "instance_id": case_id,
        "trajectory": traj_text,
        "scores": {
            "process": sb.process,
            "conciseness": sb.conciseness,
            "result": sb.result,
        },
        "process_analysis": process_analysis,
        "conciseness_analysis": conciseness_analysis,
        "result_analysis": result_analysis,
        "optimization_plan": optimization_plan,
        "feedback": sb.feedback,
    }
