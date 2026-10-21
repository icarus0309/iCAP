"""Outcome-grouped evaluator – separates success/failure cases and generates
targeted experience for each group, via random-grouped hierarchical merging.

Failure experiences are produced as a fine-grained list (one issue per entry),
each carrying ``trajectory_count`` (how many failure trajectories hit it) and the
``trajectory_ids`` it covers. Success experiences are reduced to a single summary
string. Both use random grouping (batch of 10) + hierarchical merge so the full
case set is seen without overflowing the prompt context.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ...model_client import ModelClient, QuotaExceededError
from .base import BaseEvaluator
from .result import EvaluationResult
from ..schemas import HermesRunResult, ScoreBreakdown, SessionAnalysisResult, TrajectoryStep
from ..experience_merge import (
    experience_to_text,
    hierarchical_merge_experiences,
    hierarchical_summarize_text,
    merge_experience_batch,
    parse_json_array,
    sanitize_experiences,
)
from ..mechanism_rules import CASE_MECHANISM_RULES


class OutcomeGroupedEvaluator(BaseEvaluator):
    """Evaluator that groups cases by outcome (success/failure).

    Generates separate experience summaries:
    - success_experience: patterns to preserve and promote (single text)
    - failure_experiences: fine-grained issues to fix (list, each with trajectory count)

    Also loads rejected_experiences from state for feedback loop.
    """

    def __init__(
        self,
        model_client: ModelClient,
        threshold: float = 85.0,
        stop_threshold: float = 85.0,
        merge_batch_size: int = 10,
        result_threshold: float = 80.0,
        execution_mode: str = "hermes",
    ):
        self.model_client = model_client
        self.execution_mode = execution_mode
        self._skill_excerpt = ""
        self.threshold = threshold          # 成功/失败用例拆分阈值（总分）
        self.stop_threshold = stop_threshold  # 停止优化阈值
        self.merge_batch_size = merge_batch_size  # 层次合并的随机分组大小
        self.result_threshold = result_threshold  # 结果维度硬门槛（result-first）

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
        from ..evaluation_policy import case_mean
        self._skill_excerpt = skill[:4000]
        if score_breakdowns and all(s.execution_mode == "direct" for s in score_breakdowns.values()):
            self.execution_mode = "direct"
        score_breakdowns = {k: s for k, s in score_breakdowns.items() if not s.evaluation_error}
        scores = list(score_breakdowns.values())
        n = len(scores) if scores else 1

        average_score = sum(s.total_score for s in scores) / n if scores else 0.0
        dimension_scores = {
            "process": sum(s.process for s in scores) / n if scores else 0.0,
            "conciseness": sum(s.conciseness for s in scores) / n if scores else 0.0,
            "result": sum(s.result for s in scores) / n if scores else 0.0,
        }

        if self.execution_mode == "direct":
            average_score = case_mean(scores) or 0.0
            dimension_scores = {"result": average_score}

        # ── Split by outcome ──────────────────────────────────────────
        # result-first：结果维度是硬门槛。result < result_threshold 一律归失败，
        # 只有 result 过关后，再用总分判定成功/失败，避免「结果错了却进成功经验」。
        success_cases: Dict[str, ScoreBreakdown] = {}
        failure_cases: Dict[str, ScoreBreakdown] = {}
        for case_id, sb in score_breakdowns.items():
            if sb.result >= self.result_threshold and (self.execution_mode == "direct" or sb.total_score >= self.threshold):
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

        # ── Success experience (hierarchical text summary) ────────────
        if success_cases:
            print(f"    ✅ 汇总成功经验 ({len(success_cases)} 条)...")
        success_experience = self._summarize_success(success_cases, session_results)

        # ── Failure experience (hierarchical, fine-grained, counted) ──
        if failure_cases:
            print(f"    ❌ 汇总失败经验 ({len(failure_cases)} 条)...")
        failure_experiences = self._summarize_failure(failure_cases, session_results)

        # ── Load rejected experiences from state ──────────────────────
        rejected_experiences: List[str] = state.get("rejected_experiences", [])
        if rejected_experiences:
            print(f"    📜 加载历史拒绝经验: {len(rejected_experiences)} 条")

        # ── Round-to-round score comparison ─────────────────────────
        score_history: List[float] = state.get("score_history", [])
        if scores and score_history and average_score < score_history[-1]:
            prev_score = score_history[-1]
            print(f"    🔻 分数下降: {prev_score:.2f} → {average_score:.2f}")
            if failure_experiences:
                failure_text = "；".join(experience_to_text(e) for e in failure_experiences)
                rejection = (
                    f"上一轮修改被拒 (score={average_score:.1f} vs {prev_score:.1f}). "
                    f"失败分析: {failure_text}"
                )
            else:
                rejection = (
                    f"上一轮修改被拒 (score={average_score:.1f} vs {prev_score:.1f})"
                )
            rejected_experiences = list(rejected_experiences) + [rejection]
            state["rejected_experiences"].append(rejection)
            print(f"    📝 拒绝经验已记录")

        # ── Global experience (combined) ──────────────────────────────
        global_experience = self._build_global(success_experience, failure_experiences)

        passed = bool(scores) and average_score >= self.stop_threshold
        score_report = self._build_score_report(average_score, dimension_scores, passed) if scores else "无有效评估结果"

        return EvaluationResult(
            average_score=average_score,
            dimension_scores=dimension_scores,
            passed=passed,
            case_experiences=case_experiences,
            global_experience=global_experience,
            success_experience=success_experience,
            failure_experiences=failure_experiences,
            rejected_experiences=rejected_experiences,
            score_report=score_report,
            raw={
                "case_count": len(scores),
                "execution_mode": self.execution_mode,
                "threshold": self.threshold,
                "success_count": len(success_cases),
                "failure_count": len(failure_cases),
            },
        )

    # ------------------------------------------------------------------
    # Success summarization (hierarchical text)
    # ------------------------------------------------------------------

    def _summarize_success(
        self,
        cases: Dict[str, ScoreBreakdown],
        session_results: Dict[str, SessionAnalysisResult],
    ) -> str:
        if not cases:
            return ""
        leaves = [self._case_detail(case_id, sb, session_results) for case_id, sb in cases.items()]
        leaves = [leaf for leaf in leaves if leaf]
        if not leaves:
            return ""
        if not self.model_client:
            return self._heuristic_summary("success", cases, session_results)
        return hierarchical_summarize_text(
            leaves,
            summarize_batch=self._summarize_success_batch,
            batch_size=self.merge_batch_size,
        )

    def _summarize_success_batch(self, batch: List[str]) -> str:
        case_details = "\n".join(f"- {t}" for t in batch)
        if self.execution_mode == "direct":
            case_details = self._direct_experience_rules() + "\n" + case_details
        prompt = f"""你是一个SKILL分析专家。以下是 {len(batch)} 个成功案例（或中间汇总）的详细分析。

这些案例表现良好。分析它们成功的原因（什么是有效的模式、策略、指导），以便在SKILL中保留和强化。

## 案例详情
{case_details}

请输出简洁的分析（不超过 300 字），重点放在具体的模式和改进方向上。只输出分析内容，不要有任何前缀或解释。"""
        try:
            return self.model_client.generate([{"role": "user", "content": prompt}])
        except QuotaExceededError:
            raise
        except Exception as exc:
            print(f"[警告] success 汇总模型调用失败: {exc}")
            return "" if self.execution_mode == "direct" else "；".join(batch)[:500]

    # ------------------------------------------------------------------
    # Failure summarization (hierarchical, counted)
    # ------------------------------------------------------------------

    def _summarize_failure(
        self,
        cases: Dict[str, ScoreBreakdown],
        session_results: Dict[str, SessionAnalysisResult],
    ) -> List[Dict[str, Any]]:
        if not cases:
            return []
        leaves = [
            {"case_id": case_id, "detail": self._case_detail(case_id, sb, session_results)}
            for case_id, sb in cases.items()
        ]
        leaves = [leaf for leaf in leaves if leaf["detail"]]
        if not leaves:
            return []
        if not self.model_client:
            return self._heuristic_failure_experiences(cases, session_results)
        return hierarchical_merge_experiences(
            leaves,
            to_experiences=self._cases_to_experiences,
            merge_experiences=self._merge_experiences,
            batch_size=self.merge_batch_size,
        )

    def _cases_to_experiences(self, batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        prompt = f"""你是SKILL失败经验分析专家。以下是 {len(batch)} 个失败案例的详细分析，请归纳出通用、可执行的失败经验。

## 要求
- 一条经验只描述一个根因（root_cause），不要把多个不同问题耦合到同一条。
- 每个根因带一个建议列表 suggestions（可多条），每条建议是可执行的修改方向。
- 自主归并：同一根因跨多个 case 出现时归并为一条，suggestions 取并集（去重），并在 trajectory_ids 里列出命中的 case_id；不要强制拆分，也不要强制合并。
- trajectory_count 必须等于 trajectory_ids 的长度。
- 建议保持为列表，不要把多条建议改写合并成一句；最终拼接由代码完成。

{self._direct_experience_rules() if self.execution_mode == "direct" else CASE_MECHANISM_RULES}

## 失败案例
{json.dumps(batch, ensure_ascii=False, indent=2)}

输出 JSON 数组（只输出 JSON，不要其他内容）：
[{{"root_cause": "...", "suggestions": ["...", ...], "trajectory_ids": ["case_id", ...], "trajectory_count": N}}]"""
        for attempt in range(2):
            try:
                text = self.model_client.generate([{"role": "user", "content": prompt}])
            except QuotaExceededError:
                raise
            except Exception as exc:
                print(f"[警告] 失败经验汇总模型调用失败: {exc}")
                return []
            if self.execution_mode == "direct" and text.strip() == "[]":
                return []
            exps = sanitize_experiences(parse_json_array(text))
            if exps:
                return exps
            prompt += "\n\n【重要】只输出 JSON 数组，以 [ 开头、以 ] 结尾，不要输出任何解释。"
        return []

    def _merge_experiences(self, batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return merge_experience_batch(self.model_client.generate, batch, kind_label="失败")

    # ------------------------------------------------------------------
    # Case detail building
    # ------------------------------------------------------------------

    def _direct_experience_rules(self):
        return ("仅依据 input messages、本次最终输出、judge 原文及当前 SKILL 摘要归纳可迁移判断规则。"
                "指出触发条件、可观察问题、证据和适用边界；分数不能单独证明成因。"
                "不得推测内部推理、工具操作、关键步骤或过程精简，不复制个案答案、私有事实或回复模板。"
                "若证据不足、无法归因或已有同等规则，无需强行提炼：成功返回空文本，失败返回空数组。"
                "\n当前 SKILL 摘要：\n" + self._skill_excerpt)

    @staticmethod
    def _case_detail(
        case_id: str,
        sb: ScoreBreakdown,
        session_results: Dict[str, SessionAnalysisResult],
    ) -> str:
        sr = session_results.get(case_id)
        if sr is None:
            return ""
        if sb.execution_mode == "direct":
            return json.dumps({"case_id": case_id, "result": sb.result, "input_messages": sr.input_messages,
                               "final_output": sr.final_output, "judge": sr.output_analyses}, ensure_ascii=False)
        parts = [f"## {case_id} (score={sb.total_score:.1f})"]
        if sr.process_details.get("analysis"):
            parts.append(f"过程: {sr.process_details['analysis']}")
        if sr.conciseness_details.get("analysis"):
            parts.append(f"精简: {sr.conciseness_details['analysis']}")
        for otype, odetails in sr.output_analyses.items():
            if odetails.get("analysis"):
                parts.append(f"结果[{otype}]: {odetails['analysis']}")
        if sb.feedback:
            parts.append(f"反馈: {sb.feedback}")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Heuristic fallbacks (no model)
    # ------------------------------------------------------------------

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

    @staticmethod
    def _heuristic_failure_experiences(
        cases: Dict[str, ScoreBreakdown],
        session_results: Dict[str, SessionAnalysisResult],
    ) -> List[Dict[str, Any]]:
        """No-model fallback: one experience per failure case (feedback as text)."""
        result: List[Dict[str, Any]] = []
        for case_id, sb in cases.items():
            text = (sb.feedback or "").strip()
            if not text:
                continue
            result.append({
                "root_cause": text,
                "suggestions": [],
                "trajectory_ids": [case_id],
                "trajectory_count": 1,
            })
        return result

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    def _build_global(self, success: str, failure_experiences: List[Dict[str, Any]]) -> str:
        parts = []
        if success:
            parts.append(f"### 成功经验\n{success}")
        if failure_experiences:
            items = "\n".join(f"- {experience_to_text(e)}" for e in failure_experiences)
            parts.append(f"### 失败经验\n{items}")
        return "\n\n".join(parts)

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
