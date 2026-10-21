"""GEPA core dataclasses: PromptCandidate, EvaluationResult, DataInstance."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EvaluationResult:
    """GEPA internal evaluation result for a single instance."""

    instance_id: str
    score: float
    side_info: Any
    output: str = ""


@dataclass
class PromptCandidate:
    """A prompt variant in the GEPA population."""

    prompt: str
    eval_results: Dict[str, EvaluationResult] = field(default_factory=dict)
    sub_type_scores: Dict[str, float] = field(default_factory=dict)
    aggregate_score: float = 0.0
    wins: int = 0
    generation: int = 0
    parent_id: Optional[int] = None
    id: int = 0

    def update_aggregate_score(self) -> None:
        """Update aggregate score from sub_type_scores or eval_results."""
        if self.sub_type_scores:
            self.aggregate_score = sum(self.sub_type_scores.values()) / len(self.sub_type_scores)
        elif self.eval_results:
            self.aggregate_score = (
                sum(r.score for r in self.eval_results.values()) / len(self.eval_results) * 100
            )
        else:
            self.aggregate_score = 0.0


@dataclass
class DataInstance:
    """A single dataset instance."""

    id: str
    input: str
    expected_output: str
    metadata: Optional[Dict[str, Any]] = None
    pre_condition: Optional[Any] = None  # pre-condition entity states
