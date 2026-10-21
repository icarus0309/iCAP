"""Base mutator interface."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List

from .result import MutationResult
from ..evaluators.result import EvaluationResult


class BaseMutator(ABC):
    """Mutation interface.

    Each mutator implementation takes the current skill, the evaluation
    result, and the full dataset, and produces a mutated skill document.
    """

    @abstractmethod
    def mutate(
        self,
        *,
        skill: str,
        eval_result: EvaluationResult,
        data: List[Any],
        state: Dict[str, Any],
    ) -> MutationResult:
        """Mutate the current skill based on evaluation feedback.

        Args:
            skill: Current SKILL.md full text.
            eval_result: Structured evaluation output.
            data: Full dataset instances (for internal re-evaluation).
            state: Current run state (round, history, rejected_experiences).

        Returns:
            MutationResult with the next skill and metadata.
        """
        ...
