"""Mutation result dataclass."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MutationResult:
    """Output of a mutator.

    Attributes:
        next_skill: Full text of the mutated SKILL.md.
        changed: Whether the skill actually changed.
        mutation_plan: Structured edit plan (used by structured_mutator).
        new_rejected: Rejected experiences generated during this mutation
            (for feeding back to outcome_grouped_evaluator in the next round).
        raw: Arbitrary additional data.
    """

    next_skill: str
    changed: bool
    mutation_plan: Optional[str] = None
    new_rejected: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
