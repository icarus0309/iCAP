"""Direct-analysis evaluator – the current default evaluator.

Uses per-case results already computed by the orchestrator (session analysis
+ scoring), then runs a batch analyzer to produce a global experience summary.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ...model_client import ModelClient
from .base import BaseEvaluator
from .result import EvaluationResult
from .components.batch_analyzer import BatchAnalyzer
from ..schemas import HermesRunResult, ScoreBreakdown, SessionAnalysisResult, TrajectoryStep


class DirectAnalysisEvaluator(BaseEvaluator):
    """Evaluator that works with per-case results already computed by the orchestrator.

    The orchestrator has already:
    1. Run all cases through Hermes
    2. Parsed sessions → SessionRecord
    3. Extracted trajectories → List[TrajectoryStep]
    4. Analyzed each session → SessionAnalysisResult
    5. Scored each case → ScoreBreakdown

    This evaluator:
    - Computes aggregate scores (average, per-dimension)
    - Builds case_experiences from ScoreBreakdown
    - Calls BatchAnalyzer for a global experience summary
    """

    def __init__(self, model_client: ModelClient, threshold: float = 85.0):
        self.batch_analyzer = BatchAnalyzer(model_client)
        self.threshold = threshold

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
        if any(s.execution_mode == "direct" for s in score_breakdowns.values()):
            from .outcome_grouped_evaluator import OutcomeGroupedEvaluator
            return OutcomeGroupedEvaluator(model_client=self.batch_analyzer.model_client,
                stop_threshold=self.threshold, result_threshold=state.get("result_threshold", 80.0),
                execution_mode="direct").evaluate(skill=skill, run_results=run_results, trajectories=trajectories,
                    session_results=session_results, score_breakdowns=score_breakdowns, state=state)
        # ── Aggregate scores ──────────────────────────────────────────
        scores = list(score_breakdowns.values())
        n = len(scores) if scores else 1

        average_score = sum(s.total_score for s in scores) / n if scores else 0.0
        dimension_scores = {
            "process": sum(s.process for s in scores) / n if scores else 0.0,
            "conciseness": sum(s.conciseness for s in scores) / n if scores else 0.0,
            "result": sum(s.result for s in scores) / n if scores else 0.0,
        }

        # ── Case experiences ───────────────────────────────────────────
        case_experiences: List[Dict[str, Any]] = []
        for case_id, sb in score_breakdowns.items():
            case_experiences.append({
                "case_id": case_id,
                "total_score": sb.total_score,
                "needs_optimization": sb.needs_optimization,
                "feedback": sb.feedback,
                "dimension_suggestions": sb.dimension_suggestions,
                "sub_type_scores": sb.sub_type_scores,
            })

        # ── Global experience ──────────────────────────────────────────
        global_experience = self.batch_analyzer.analyze(
            session_results=session_results,
            score_breakdowns=score_breakdowns,
            trajectories=trajectories,
        )

        # ── Pass / build report ────────────────────────────────────────
        passed = average_score >= self.threshold
        score_report = self._build_score_report(average_score, dimension_scores, passed)

        return EvaluationResult(
            average_score=average_score,
            dimension_scores=dimension_scores,
            passed=passed,
            case_experiences=case_experiences,
            global_experience=global_experience,
            score_report=score_report,
            raw={
                "case_count": len(scores),
                "threshold": self.threshold,
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_score_report(
        average_score: float,
        dimension_scores: Dict[str, float],
        passed: bool,
    ) -> str:
        if "process" not in dimension_scores and "conciseness" not in dimension_scores:
            return f"结果平均分: {average_score:.2f}\n达标: {'是' if passed else '否'}"
        lines = [
            f"综合平均分: {average_score:.2f}",
            f"过程(process): {dimension_scores.get('process', 0):.2f}",
            f"精简(conciseness): {dimension_scores.get('conciseness', 0):.2f}",
            f"结果(result): {dimension_scores.get('result', 0):.2f}",
            f"达标: {'是' if passed else '否'}",
        ]
        return "\n".join(lines)
