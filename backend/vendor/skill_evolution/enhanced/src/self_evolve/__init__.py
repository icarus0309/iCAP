"""
Self-evolution workflow for SKILL.md files.

This package is intentionally additive: existing eval/opt flows do not import
it unless the self-evolve command is used.
"""

from .schemas import (
    EvolutionRound,
    HermesRunResult,
    ScoreBreakdown,
    SessionRecord,
    TrajectoryStep,
)
from .execution.hermes_runner import HermesCliRunner
from .orchestrator import SelfEvolveOrchestrator, SelfEvolveConfig
from .execution.executor import ExecutorConfig, Executor, create_executor

__all__ = [
    "EvolutionRound",
    "HermesRunResult",
    "ScoreBreakdown",
    "SessionRecord",
    "TrajectoryStep",
    "HermesCliRunner",
    "SelfEvolveConfig",
    "SelfEvolveOrchestrator",
    "ExecutorConfig",
    "Executor",
    "create_executor",
]
