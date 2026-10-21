"""Result-first routing decision tree for one small/large model case pair.

Mirrors distill's ``dual_model_router.route_case`` intent, but the *result*
dimension is lifted out as the first gate: the large model only runs when the
small model's result is wrong. Hermes evaluates three dimensions; direct evaluates only result.
Only the result score controls the large-model gate.

Decision tree (routing keys only off the result score):

    small result >= result_threshold         -> SUCCESS   (large model NOT run)
    small result < result_threshold
        ├─ large result >= result_threshold  -> KNOWLEDGE (small failed, large succeeded)
        └─ otherwise                         -> FAILURE   (small trace kept)
    any evaluation error                      -> INVALID
"""

from __future__ import annotations

from typing import Any

ROUTE_SUCCESS = "SUCCESS"
ROUTE_FAILURE = "FAILURE"
ROUTE_KNOWLEDGE = "KNOWLEDGE"
ROUTE_INVALID = "INVALID"

# Backward-compatible aliases matching distill's route names, for any consumer
# that still keys off the older constants.
ROUTE_SUCCESS_DISTILL = "success_experience"
ROUTE_FAILURE_DISTILL = "failure_experience"
ROUTE_KNOWLEDGE_DISTILL = "knowledge_extraction"
ROUTE_INVALID_DISTILL = "invalid"


def route_result_first(
    *,
    case_id: str | None,
    small_result_score: float | None,
    small_process_score: float | None,
    small_conciseness_score: float | None,
    large_result_score: float | None,
    small_error: str = "",
    large_error: str = "",
    result_threshold: float = 80.0,
    execution_mode: str = "hermes",
    large_model_enabled: bool = True,
    large_model_run: bool | None = None,
) -> dict[str, Any]:
    """Return a routing dict describing which downstream path a case takes.

    Hermes scores three dimensions; direct passes None for unassessed dimensions, so
    ``small_process_score`` / ``small_conciseness_score`` are present whenever
    the small evaluation succeeded; ``large_result_score`` is ``None`` when the
    large model was not run.
    """
    small_error = str(small_error or "").strip()
    large_error = str(large_error or "").strip()
    base: dict[str, Any] = {
        "case_id": case_id,
        "small_result_score": small_result_score,
        "small_process_score": small_process_score,
        "small_conciseness_score": small_conciseness_score,
        "large_result_score": large_result_score,
        "result_threshold": result_threshold,
        "large_model_run": large_result_score is not None if large_model_run is None else large_model_run,
        "execution_mode": execution_mode,
        "evaluated_dimensions": ["result"] if execution_mode == "direct" else ["process", "conciseness", "result"],
    }

    if small_error or large_error:
        return {
            **base,
            "route": ROUTE_INVALID,
            "reason": "evaluation_error",
            "evaluation_errors": {"small": small_error, "large": large_error},
        }

    if small_result_score is None:
        return {
            **base,
            "route": ROUTE_INVALID,
            "reason": "small_result_missing",
        }

    small_result_ok = small_result_score >= result_threshold
    if small_result_ok:
        return {**base, "route": ROUTE_SUCCESS, "reason": "small_result_ok"}

    if not large_model_enabled:
        return {**base, "route": ROUTE_FAILURE, "reason": "small_failed_large_disabled"}

    # Small result failed -> large model runs (result-only).
    if large_result_score is None:
        return {
            **base,
            "route": ROUTE_INVALID,
            "reason": "large_model_not_run_after_small_result_failure",
        }
    if large_result_score >= result_threshold:
        return {**base, "route": ROUTE_KNOWLEDGE, "reason": "small_failed_large_succeeded"}
    return {**base, "route": ROUTE_FAILURE, "reason": "both_models_result_failed"}


__all__ = [
    "ROUTE_FAILURE",
    "ROUTE_INVALID",
    "ROUTE_KNOWLEDGE",
    "ROUTE_SUCCESS",
    "ROUTE_FAILURE_DISTILL",
    "ROUTE_INVALID_DISTILL",
    "ROUTE_KNOWLEDGE_DISTILL",
    "ROUTE_SUCCESS_DISTILL",
    "route_result_first",
]
