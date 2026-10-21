"""GEPA trace store – save iteration artifacts to disk."""

from pathlib import Path
from typing import Optional

from .candidate import PromptCandidate


def save_skill_immediately(
    candidate: PromptCandidate,
    iteration: int,
    accepted: bool,
    output_dir: Optional[str] = None,
    parent_id: Optional[int] = None,
) -> None:
    """Save a candidate skill file to the output directory immediately."""
    if not output_dir:
        return

    output_path = Path(output_dir)
    iter_dir = output_path / "iterations"
    iter_dir.mkdir(parents=True, exist_ok=True)

    status = "accepted" if accepted else "rejected"
    filename = f"iter_{iteration}_candidate_{candidate.id}_{status}.md"
    filepath = iter_dir / filename
    filepath.write_text(candidate.prompt, encoding="utf-8")
