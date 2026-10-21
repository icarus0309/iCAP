"""Dual-model Hermes runner: holds small + large isolated runners.

The small model runs first; the large model only runs when the small model's
result fails. Actual dispatch / routing lives in the orchestrator (which owns
parsing, trajectory extraction, analysis and scoring); this module is a thin
holder + factory for the two ``HermesCliRunner`` instances, mirroring distill's
``collect_case_pair`` separation of small/large envs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..diagnostics import DiagnosticLogger
from .hermes_runner import HermesCliConfig, HermesCliRunner


@dataclass
class DualModelRunner:
    """Two isolated Hermes runners sharing the same skill but different models."""

    small: HermesCliRunner
    large: HermesCliRunner

    @classmethod
    def create(
        cls,
        *,
        small_config: HermesCliConfig,
        large_config: HermesCliConfig,
        run_dir: Path,
        run_id: str,
        logger: Optional[DiagnosticLogger] = None,
    ) -> "DualModelRunner":
        logger = logger or DiagnosticLogger()
        small = HermesCliRunner(
            small_config,
            run_dir / "small_env",
            f"{run_id}-small",
            logger=logger,
        )
        large = HermesCliRunner(
            large_config,
            run_dir / "large_env",
            f"{run_id}-large",
            logger=logger,
        )
        return cls(small=small, large=large)


__all__ = ["DualModelRunner"]
