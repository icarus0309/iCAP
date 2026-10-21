"""GEPA Pareto frontier selection utilities."""

import random
from typing import Dict, List, Optional

from .candidate import PromptCandidate


def find_pareto_frontier(candidates: List[PromptCandidate]) -> List[PromptCandidate]:
    """Find the Pareto frontier based on sub_type_scores vectors."""
    if not candidates:
        return []

    # Collect all sub_types
    all_sub_types: set = set()
    for c in candidates:
        all_sub_types.update(c.sub_type_scores.keys())

    # Best score per sub_type
    best_per_type: Dict[str, float] = {}
    for sub_type in all_sub_types:
        best_per_type[sub_type] = max(
            c.sub_type_scores.get(sub_type, 0.0) for c in candidates
        )

    # Assign win counts
    for candidate in candidates:
        candidate.wins = 0
        for sub_type, best_score in best_per_type.items():
            if sub_type in candidate.sub_type_scores:
                if abs(candidate.sub_type_scores[sub_type] - best_score) < 1e-6:
                    candidate.wins += 1

    # Find non-dominated candidates
    frontier: List[PromptCandidate] = []
    for candidate in candidates:
        dominated = False
        for other in candidates:
            if other.id == candidate.id:
                continue
            if _dominates(other, candidate):
                dominated = True
                break
        if not dominated:
            frontier.append(candidate)

    return frontier


def _dominates(a: PromptCandidate, b: PromptCandidate) -> bool:
    """Check if candidate a dominates candidate b on sub_type_scores."""
    if a.sub_type_scores and b.sub_type_scores:
        common = set(a.sub_type_scores.keys()) & set(b.sub_type_scores.keys())
        if not common:
            return False
        all_better_or_equal = True
        any_strictly_better = False
        for sub_type in common:
            score_a = a.sub_type_scores[sub_type]
            score_b = b.sub_type_scores[sub_type]
            if score_a < score_b - 1e-6:
                all_better_or_equal = False
                break
            elif score_a > score_b + 1e-6:
                any_strictly_better = True
        return all_better_or_equal and any_strictly_better

    # Fallback: single aggregate_score
    return a.aggregate_score > b.aggregate_score + 1e-6


def select_parent(frontier: List[PromptCandidate]) -> Optional[PromptCandidate]:
    """Select a parent from the Pareto frontier (weighted by wins)."""
    if not frontier:
        return None

    win_counts = [max(c.wins, 1) for c in frontier]
    total_wins = sum(win_counts)
    probabilities = [w / total_wins for w in win_counts]
    return random.choices(frontier, weights=probabilities, k=1)[0]


def select_final_candidate(candidates: List[PromptCandidate]) -> Optional[PromptCandidate]:
    """Select the best final candidate (highest aggregate score)."""
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.aggregate_score)
