"""Outcome-grouped evaluator – separates success/failure cases and generates
targeted experience for each group.

Designed to work with the structured mutator, which uses success_experience
and failure_experience separately to plan bounded edits.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ...model_client import ModelClient
from .base import BaseEvaluator
from .result import EvaluationResult
from ..schemas import HermesRunResult, ScoreBreakdown, SessionAnalysisResult, TrajectoryStep


class OutcomeGroupedEvaluator(BaseEvaluator):
    """Evaluator that groups cases by outcome (success/failure).

    Generates separate experience summaries:
    - success_experience: patterns to preserve and promote
    - failure_experience: patterns to fix

    Also loads rejected_experiences from state for feedback loop.
    """

    def __init__(
        self,
        model_client: ModelClient,
        threshold: float = 85.0,
        stop_threshold: float = 85.0,
        enable_rejected_experience: bool = True,
        success_case_limit: int = 10,
        failure_case_limit: int = 30,
    ):
        self.model_client = model_client
        self.threshold = threshold          # 成功/失败用例拆分阈值
        self.stop_threshold = stop_threshold  # 停止优化阈值
        self.enable_rejected_experience = enable_rejected_experience  # 拒绝经验开关
        self.success_case_limit = success_case_limit  # 成功组总结采样的用例数上限
        self.failure_case_limit = failure_case_limit  # 失败组总结采样的用例数上限

    def evaluate(
        self,
        *,
        skill: str,
        run_results: Dict[str, HermesRunResult],
        trajectories: Dict[str, List[TrajectoryStep]],
        session_results: Dict[str, SessionAnalysisResult],
        score_breakdowns: Dict[str, ScoreBreakdown],
        state: Dict[str, Any],
    ) -> EvaluationResult:
        scores = list(score_breakdowns.values())
        n = len(scores) if scores else 1

        average_score = sum(s.total_score for s in scores) / n if scores else 0.0
        dimension_scores = {
            "process": sum(s.process for s in scores) / n if scores else 0.0,
            "conciseness": sum(s.conciseness for s in scores) / n if scores else 0.0,
            "result": sum(s.result for s in scores) / n if scores else 0.0,
        }

        # ── Split by outcome ──────────────────────────────────────────
        success_cases: Dict[str, ScoreBreakdown] = {}
        failure_cases: Dict[str, ScoreBreakdown] = {}
        for case_id, sb in score_breakdowns.items():
            if sb.total_score >= self.threshold:
                success_cases[case_id] = sb
            else:
                failure_cases[case_id] = sb

        print(f"    📊 成功 {len(success_cases)} / 失败 {len(failure_cases)} (阈值: {self.threshold})")

        # ── Case experiences ──────────────────────────────────────────
        case_experiences: List[Dict[str, Any]] = []
        for case_id, sb in score_breakdowns.items():
            case_experiences.append({
                "case_id": case_id,
                "total_score": sb.total_score,
                "needs_optimization": sb.needs_optimization,
                "feedback": sb.feedback,
                "outcome": "success" if case_id in success_cases else "failure",
            })

        # ── Success experience ────────────────────────────────────────
        if success_cases:
            print(f"    ✅ 汇总成功经验 ({len(success_cases)} 条)...")
        success_experience = self._summarize_group(
            "success", success_cases, session_results, trajectories, self.success_case_limit
        )

        # ── Failure experience ────────────────────────────────────────
        if failure_cases:
            print(f"    ❌ 汇总失败经验 ({len(failure_cases)} 条)...")
        failure_experience = self._summarize_group(
            "failure", failure_cases, session_results, trajectories, self.failure_case_limit
        )

        # ── Rejected experiences (optional, controlled by switch) ───
        rejected_experiences: List[str] = []
        if self.enable_rejected_experience:
            # ── Load rejected experiences from state ──────────────────
            rejected_experiences = state.get("rejected_experiences", [])
            if rejected_experiences:
                print(f"    📜 加载历史拒绝经验: {len(rejected_experiences)} 条")

            # ── Round-to-round score comparison ─────────────────────
            score_history: List[float] = state.get("score_history", [])
            if score_history and average_score < score_history[-1]:
                prev_score = score_history[-1]
                print(f"    🔻 分数下降: {prev_score:.2f} → {average_score:.2f}")
                if failure_experience:
                    rejection = (
                        f"上一轮修改被拒 (score={average_score:.1f} vs {prev_score:.1f}). "
                        f"失败分析: {failure_experience}"
                    )
                else:
                    rejection = (
                        f"上一轮修改被拒 (score={average_score:.1f} vs {prev_score:.1f})"
                    )
                rejected_experiences = list(rejected_experiences) + [rejection]
                state["rejected_experiences"].append(rejection)
                print(f"    📝 拒绝经验已记录")

        # ── Global experience (combined) ──────────────────────────────
        global_experience = self._build_global(success_experience, failure_experience)

        passed = average_score >= self.stop_threshold
        score_report = self._build_score_report(average_score, dimension_scores, passed)

        return EvaluationResult(
            average_score=average_score,
            dimension_scores=dimension_scores,
            passed=passed,
            case_experiences=case_experiences,
            global_experience=global_experience,
            success_experience=success_experience,
            failure_experience=failure_experience,
            rejected_experiences=rejected_experiences,
            score_report=score_report,
            raw={
                "case_count": n,
                "threshold": self.threshold,
                "success_count": len(success_cases),
                "failure_count": len(failure_cases),
            },
        )

    # ------------------------------------------------------------------
    # Group summarization
    # ------------------------------------------------------------------

    def _summarize_group(
        self,
        label: str,
        cases: Dict[str, ScoreBreakdown],
        session_results: Dict[str, SessionAnalysisResult],
        trajectories: Dict[str, List[TrajectoryStep]],
        case_limit: int = 10,
    ) -> str:
        """Summarize a group of cases (success or failure)."""
        if not cases:
            return ""

        # Build case detail text
        details: List[str] = []
        for case_id, sb in cases.items():
            sr = session_results.get(case_id)
            if sr is None:
                continue
            parts = [f"## {case_id} (score={sb.total_score:.1f})"]
            user_input, model_output = self._extract_input_output(trajectories.get(case_id, []))
            if user_input:
                parts.append(f"输入: {user_input}")
            if model_output:
                parts.append(f"输出: {model_output}")
            if sr.process_details.get("analysis"):
                parts.append(f"过程: {sr.process_details['analysis']}")
            if sr.conciseness_details.get("analysis"):
                parts.append(f"精简: {sr.conciseness_details['analysis']}")
            for otype, odetails in sr.output_analyses.items():
                if odetails.get("analysis"):
                    parts.append(f"结果[{otype}]: {odetails['analysis']}")
            details.append("\n".join(parts))

        case_text = "\n\n".join(details[:case_limit])

        if self.model_client:
            return self._call_model_summary(label, case_text, len(cases))
        return self._heuristic_summary(label, cases, session_results)

    @staticmethod
    def _extract_input_output(trajectory: List[TrajectoryStep], limit: int = 800) -> Tuple[str, str]:
        """从轨迹提取用户输入（拼接所有 user 轮）与模型最终输出（最后一条 assistant）。"""
        user_parts: List[str] = []
        assistant_output = ""
        for step in trajectory:
            content = (step.content or "").strip()
            if not content:
                continue
            if step.role == "user":
                user_parts.append(content)
            elif step.role == "assistant":
                assistant_output = content  # 取最后一条 assistant
        user_input = "\n".join(user_parts)
        if len(user_input) > limit:
            user_input = user_input[:limit] + f"...[截断，原{len(user_input)}字符]"
        if len(assistant_output) > limit:
            assistant_output = assistant_output[:limit] + f"...[截断，原{len(assistant_output)}字符]"
        return user_input, assistant_output

    def _call_model_summary(self, label: str, case_text: str, count: int) -> str:
        direction = {
            "success": "这些案例表现良好。分析它们成功的原因（什么是有效的模式、策略、指导），以便在SKILL中保留和强化。",
            "failure": "这些案例表现不佳。分析失败的根本原因（缺少什么指导、什么步骤经常出错），以便在SKILL中修复。",
        }
        prompt = f"""你是一个SKILL分析专家。以下是{count}个{label}案例的详细分析。

{direction.get(label, "")}

## 案例详情
{case_text}

请输出简洁的分析（不超过 200 字），重点放在具体的模式和改进方向上。只输出分析内容，不要有任何前缀或解释。"""

        try:
            return self.model_client.generate([{"role": "user", "content": prompt}])
        except Exception as exc:
            print(f"[警告] {label}总结模型调用失败: {exc}")
            return f"[模型调用失败] {case_text[:500]}"

    @staticmethod
    def _heuristic_summary(
        label: str,
        cases: Dict[str, ScoreBreakdown],
        session_results: Dict[str, SessionAnalysisResult],
    ) -> str:
        if not cases:
            return ""
        avg = sum(sb.total_score for sb in cases.values()) / len(cases)
        lines = [f"## {label}案例 ({len(cases)} 个, 均分 {avg:.1f})"]
        # Collect common failure modes
        modes: List[str] = []
        for case_id, sb in cases.items():
            sr = session_results.get(case_id)
            if sr and sr.failure_modes:
                modes.extend(sr.failure_modes)
        if modes:
            from collections import Counter
            for mode, cnt in Counter(modes).most_common(5):
                lines.append(f"- [{cnt}/{len(cases)}] {mode}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def _build_global(self, success: str, failure: str) -> str:
        parts = []
        if success:
            parts.append(f"### 成功经验\n{success}")
        if failure:
            parts.append(f"### 失败经验\n{failure}")
        return "\n\n".join(parts)

    @staticmethod
    def _build_score_report(
        average_score: float,
        dimension_scores: Dict[str, float],
        passed: bool,
    ) -> str:
        lines = [
            f"综合平均分: {average_score:.2f}",
            f"过程(process): {dimension_scores.get('process', 0):.2f}",
            f"精简(conciseness): {dimension_scores.get('conciseness', 0):.2f}",
            f"结果(result): {dimension_scores.get('result', 0):.2f}",
            f"达标: {'是' if passed else '否'}",
        ]
        return "\n".join(lines)
