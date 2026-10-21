"""Freeform mutator – wraps GEPA optimizer as a BaseMutator.

This is the current default mutator. It passes the evaluation feedback
into GEPA's population/Pareto optimization loop, where LLM-based freeform
rewrites of the entire skill document are generated and selected.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import BaseMutator
from .result import MutationResult
from .gepa import DataInstance, GEPAOptimizer, PromptCandidate
from ..evaluators.result import EvaluationResult


class FreeformMutator(BaseMutator):
    """Mutator that delegates to GEPA for full-text LLM mutation.

    The `gepa_evaluator` callable must have the signature:
        (output, expected, input_text="", sub_type="") -> (score, side_info)
    It is used internally by GEPA to score each candidate against the dataset.
    """

    def __init__(
        self,
        model_client: Any,
        gen_model_client: Optional[Any] = None,
        gepa_evaluator: Optional[Any] = None,
        skill_executor: Optional[Any] = None,
        output_dir: Optional[str] = None,
        iterations: int = 1,
        pareto_size: Optional[int] = None,
        minibatch_size: int = 10,
        max_metric_calls: Optional[int] = None,
        enable_merge: bool = False,
        progress_callback: Optional[Any] = None,
    ):
        self.model_client = model_client
        self.gen_model_client = gen_model_client or model_client
        self.gepa_evaluator = gepa_evaluator
        self.skill_executor = skill_executor
        self.output_dir = output_dir
        self.iterations = iterations
        self.pareto_size = pareto_size
        self.minibatch_size = minibatch_size
        self.max_metric_calls = max_metric_calls
        self.enable_merge = enable_merge
        self.progress_callback = progress_callback

    def mutate(
        self,
        *,
        skill: str,
        eval_result: EvaluationResult,
        data: List[Any],
        state: Dict[str, Any],
        root_scores: Optional[Dict[str, float]] = None,
        root_side_infos: Optional[Dict[str, Any]] = None,
        target_score: Optional[float] = None,
    ) -> MutationResult:
        gepa_data = _to_gepa_instances(data)

        from ...config import GEPAConfig

        gepa_config = GEPAConfig(
            iterations=self.iterations,
            pareto_size=self.pareto_size or len(gepa_data),
            minibatch_size=self.minibatch_size,
            max_metric_calls=self.max_metric_calls,
            enable_merge=self.enable_merge,
        )

        evaluator_fn = self.gepa_evaluator if self.gepa_evaluator else _make_fallback_evaluator()

        optimizer = GEPAOptimizer(
            model_client=self.model_client,
            config=gepa_config,
            evaluator=evaluator_fn,
            is_skill_mode=True,
            gen_model_client=self.gen_model_client,
            output_dir=self.output_dir,
            skill_executor=self.skill_executor,
            progress_callback=self.progress_callback,
        )

        candidate: Optional[PromptCandidate] = optimizer.optimize(
            skill, gepa_data, root_scores=root_scores,
            root_side_infos=root_side_infos, target_score=target_score,
        )

        if candidate and candidate.prompt != skill:
            return MutationResult(
                next_skill=candidate.prompt,
                changed=True,
                new_rejected=_extract_rejected(optimizer.iteration_history),
                raw={
                    "candidate_score": candidate.aggregate_score,
                    "candidate_id": candidate.id,
                    "generation": candidate.generation,
                    "total_metric_calls": optimizer.total_metric_calls,
                },
            )

        # Even if the best candidate == root (no improvement), still return
        # the best one found — it may differ from the input skill.
        if candidate:
            return MutationResult(
                next_skill=candidate.prompt,
                changed=False,
                new_rejected=_extract_rejected(optimizer.iteration_history),
                raw={
                    "candidate_score": candidate.aggregate_score,
                    "candidate_id": candidate.id,
                    "generation": candidate.generation,
                    "total_metric_calls": optimizer.total_metric_calls,
                },
            )

        return MutationResult(next_skill=skill, changed=False)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _to_gepa_instances(data: List[Any]) -> List[DataInstance]:
    """Convert a list of data items to GEPA DataInstance objects."""
    gepa_data: List[DataInstance] = []
    for item in data:
        if isinstance(item, DataInstance):
            gepa_data.append(item)
        elif isinstance(item, dict):
            gepa_data.append(DataInstance(
                id=str(item.get("id", "")),
                input=str(item.get("input", "")),
                expected_output=str(item.get("expected_output", "")),
                metadata=item.get("metadata"),
            ))
        else:
            gepa_data.append(DataInstance(
                id=str(getattr(item, "id", "")),
                input=str(getattr(item, "input", "")),
                expected_output=str(getattr(item, "expected_output", "")),
                metadata=getattr(item, "metadata", None),
            ))
    return gepa_data


def _make_fallback_evaluator():
    """Create a simple fallback evaluator when none is provided."""
    def fallback(output, expected, input_text="", sub_type=""):
        from ...scoring.base import SideInfo
        return 0.0, SideInfo(0.0, "no evaluator configured")
    return fallback


def _extract_rejected(iteration_history: List[Dict[str, Any]]) -> List[str]:
    """Extract descriptions of rejected mutations from iteration history."""
    rejected: List[str] = []
    for entry in iteration_history:
        if not entry.get("accepted", True) and entry.get("type") == "mutate":
            parent_score = entry.get("parent_score", 0)
            child_score = entry.get("score", 0)
            rejected.append(
                f"[迭代{entry['iteration']}] 候选{entry.get('candidate_id', '?')} "
                f"被拒 (score={child_score:.1f} vs parent={parent_score:.1f})"
            )
    return rejected
