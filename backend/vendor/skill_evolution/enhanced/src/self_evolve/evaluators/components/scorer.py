"""Weighted scoring for self-evolution cases (三维度：过程 / 精简 / 结果)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ....config import load_output_weights
from ....scoring.base import SideInfo
from ...schemas import ScoreBreakdown, ScorerWeights, SessionAnalysisResult, to_plain_dict


class WeightedScorer:
    """三维度加权评分器。"""

    def __init__(
        self,
        evaluator: Optional[Any] = None,
        threshold: float = 85.0,
        weights: Optional[ScorerWeights] = None,
        output_weights: Optional[Dict[str, float]] = None,
    ):
        self.evaluator = evaluator
        self.threshold = threshold
        self.weights = weights or ScorerWeights()
        # 结果子类型权重（text/tool/file），env 驱动。
        self.output_weights = output_weights or load_output_weights()

    def score(
        self,
        *,
        case_id: str,
        input_text: str,
        sub_type: str,
        key_steps: Optional[List[Dict[str, Any]]],
        process: float,
        conciseness: float,
        process_details: Dict[str, Any],
        conciseness_details: Dict[str, Any],
        output_scores: Optional[Dict[str, float]] = None,
        output_analyses: Optional[Dict[str, Dict[str, Any]]] = None,
        overall_improvement_suggestions: str = "",
        output_weights: Optional[Dict[str, float]] = None,
        output_path: Optional[Path] = None,
        execution_mode: str = "hermes", evaluation_error: str = "",
    ) -> ScoreBreakdown:
        """
        三维度加权评分。

          result = Σ(w_type * score_type) / Σ(w_type)   # 对存在的子类型归一化
          total  = w过程 * process + w精简 * conciseness + w结果 * result
        """
        output_scores = output_scores or {}
        output_analyses = output_analyses or {}

        if execution_mode == "direct":
            from ...evaluation_policy import DIRECT_POLICY, RESULT_WEIGHTS, write_json
            import math
            effective_weights = dict(output_weights if output_weights is not None else self.output_weights)
            if set(output_scores) == {"onlyllm"}:
                effective_weights["onlyllm"] = 1.0
            error = evaluation_error
            active = {k: v for k, v in output_scores.items() if effective_weights.get(k, 0) > 0}
            if not active or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 100 for v in active.values()):
                error = error or "invalid or missing result scores"
            result = self._combine_result(active, effective_weights) if not error else 0.0
            analyses = [d.get("analysis", "") for d in output_analyses.values() if d.get("analysis")]
            score = ScoreBreakdown(case_id=case_id, total_score=result, result=result,
                process=0.0, conciseness=0.0, needs_optimization=bool(error) or result < self.threshold,
                execution_mode="direct", evaluation_mode="result_only", evaluated_dimensions=["result"],
                evaluation_error=error, feedback=error or "；".join(analyses) or "结果评估完成",
                weights=dict(RESULT_WEIGHTS), sub_type_scores={"result": result},
                output_scores=output_scores, dimension_suggestions={"result": analyses},
                details={**DIRECT_POLICY, "output_weights": effective_weights,
                         "analysis_details": {"output_analyses": output_analyses}})
            if output_path:
                write_json(output_path, to_plain_dict(score))
            return score

        # 结果维度：按子类型权重加权（对存在的 type 归一化）
        result = self._combine_result(output_scores, output_weights)

        # 整体总分
        jw = self.weights.judgment_weights
        total = (
            process * jw.get("process", 0.0)
            + conciseness * jw.get("conciseness", 0.0)
            + result * jw.get("result", 0.0)
        )

        # 方向/门控分（进 GEPA 变异反馈，见 gepa/reflection.py）
        ow = self.weights.optimization_weights
        sub_type_scores = {
            "process": process * ow.get("process", 0.0),
            "conciseness": conciseness * ow.get("conciseness", 0.0),
            "result": result * ow.get("result", 0.0),
        }

        dimension_suggestions = self._generate_dimension_suggestions(
            process=process,
            conciseness=conciseness,
            result=result,
            process_details=process_details,
            conciseness_details=conciseness_details,
            output_analyses=output_analyses,
            overall_improvement_suggestions=overall_improvement_suggestions,
        )

        feedback_parts = []
        if process < 80:
            feedback_parts.append(f"过程精准度低({process:.1f})")
        if conciseness < 80:
            feedback_parts.append(f"精简度低({conciseness:.1f})")
        if result and result < 80:
            feedback_parts.append(f"结果质量低({result:.1f})")

        suggestions_text = []
        for dim, suggestions in dimension_suggestions.items():
            if suggestions:
                concrete = suggestions[-1] if len(suggestions) > 1 else suggestions[0]
                suggestions_text.append(f"{dim}:{concrete}")

        feedback = "；".join(feedback_parts) if feedback_parts else "评估完成"
        if suggestions_text:
            feedback += "；" + "；".join(suggestions_text)

        output_breakdown: Dict[str, Any] = {"result": result}
        for otype, oscore in output_scores.items():
            output_breakdown[otype] = oscore

        score = ScoreBreakdown(
            case_id=case_id,
            total_score=total,
            result=result,
            process=process,
            conciseness=conciseness,
            needs_optimization=total < self.threshold,
            feedback=feedback,
            weights=jw,
            sub_type_scores=sub_type_scores,
            output_scores=dict(output_scores),
            dimension_suggestions=dimension_suggestions,
            details={
                "judgment_weights": jw,
                "optimization_weights": ow,
                "output_weights": self.output_weights,
                "output_breakdown": output_breakdown,
                "analysis_details": {
                    "process_details": process_details,
                    "conciseness_details": conciseness_details,
                    "output_analyses": output_analyses,
                },
            },
        )

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(to_plain_dict(score), ensure_ascii=False, indent=2), encoding="utf-8")

        return score

    def _combine_result(
        self,
        output_scores: Dict[str, float],
        output_weights: Optional[Dict[str, float]],
    ) -> float:
        """结果维度：按子类型权重加权，并对存在的 type 归一化。"""
        weights = output_weights or self.output_weights
        weighted_sum = 0.0
        weight_sum = 0.0
        for otype, oscore in output_scores.items():
            w = weights.get(otype, 0.0)
            if w <= 0:
                continue
            weighted_sum += w * oscore
            weight_sum += w
        if weight_sum <= 0:
            return 0.0
        return weighted_sum / weight_sum

    def _generate_dimension_suggestions(
        self,
        *,
        process: float,
        conciseness: float,
        result: float,
        process_details: Dict[str, Any],
        conciseness_details: Dict[str, Any],
        output_analyses: Dict[str, Dict[str, Any]],
        overall_improvement_suggestions: str = "",
    ) -> Dict[str, List[str]]:
        """基于各维度分析与阈值生成维度改进建议。"""
        suggestions: Dict[str, List[str]] = {}

        suggestions["process"] = []
        if process_details and "analysis" in process_details:
            suggestions["process"].append(process_details["analysis"])
        elif process < 80:
            suggestions["process"] = ["改进关键步骤的准确性", "检查是否遗漏了重要步骤"]

        suggestions["conciseness"] = []
        if conciseness_details and "analysis" in conciseness_details:
            suggestions["conciseness"].append(conciseness_details["analysis"])
        elif conciseness < 80:
            suggestions["conciseness"] = ["减少不必要的步骤", "避免重复执行"]

        suggestions["result"] = []
        result_analyses = [d.get("analysis", "") for d in output_analyses.values() if d.get("analysis")]
        if result_analyses:
            suggestions["result"].extend(result_analyses)
        elif result and result < 80:
            suggestions["result"] = ["提高输出结果的准确性"]

        if overall_improvement_suggestions:
            suggestions["overall"] = [overall_improvement_suggestions]

        return suggestions

    # ------------------------------------------------------------------
    # 以下为旧版遗留方法，当前未被调用，保留待后续清理。
    # ------------------------------------------------------------------

    def _score_output(
        self,
        *,
        actual_output: str,
        expected_output: str,
        input_text: str,
        sub_type: str,
    ) -> tuple[float, str, Dict[str, Any]]:
        if self.evaluator:
            score, side_info = self.evaluator(
                actual_output,
                expected_output,
                input_text=input_text,
                sub_type=sub_type,
            )
            score_100 = score * 100 if score <= 1.0 else score
            feedback = side_info.feedback if isinstance(side_info, SideInfo) else getattr(side_info, "feedback", "")
            diagnostics = getattr(side_info, "diagnostics", {}) if side_info else {}
            return max(0.0, min(100.0, float(score_100))), feedback, diagnostics

        return self._heuristic_output_score(actual_output, expected_output), "heuristic output score", {}

    def _heuristic_output_score(self, actual: str, expected: str) -> float:
        actual_tokens = set((actual or "").lower().split())
        expected_tokens = set((expected or "").lower().split())
        if not expected_tokens:
            return 100.0 if actual.strip() else 0.0
        if not actual_tokens:
            return 0.0
        overlap = len(actual_tokens & expected_tokens) / len(expected_tokens)
        return max(0.0, min(100.0, overlap * 100.0))

    def _score_key_steps(self, key_steps: List[Dict[str, Any]], distilled: SessionAnalysisResult) -> float:
        if not key_steps:
            return 0.0
        coverage = distilled.key_step_coverage if hasattr(distilled, "key_step_coverage") else {}
        if coverage:
            return sum(1 for covered in coverage.values() if covered) / max(1, len(coverage)) * 100.0
        return 0.0
