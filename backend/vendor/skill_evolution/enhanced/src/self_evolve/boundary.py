"""Stop-condition checker for the self-evolution loop."""

from __future__ import annotations

from typing import Any, Dict

from .evaluators.result import EvaluationResult


class BoundaryChecker:
    """Check whether the self-evolution loop should stop.

    Uses its own threshold to compare against eval_result.average_score,
    rather than relying on eval_result.passed (which may have been
    computed with a different threshold by the evaluator).
    """

    def __init__(self, threshold: float = 85.0):
        self.threshold = threshold

    def check(
        self,
        eval_result: EvaluationResult,
        state: Dict[str, Any],
    ) -> bool:
        """Return True if optimization should stop.

        Args:
            eval_result: The evaluator's output for the current round.
            state: Current run state (round_index, history, etc.).

        Returns:
            True when the skill quality is sufficient.
        """
        if eval_result.average_score >= self.threshold:
            return True

        # Additional check: if score has been declining
        history = state.get("score_history", [])
        if len(history) >= 2 and history[-1] < history[-2]:
            # Score declined; could also stop, but for now just pass
            pass

        return False
