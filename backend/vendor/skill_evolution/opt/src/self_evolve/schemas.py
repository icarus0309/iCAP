"""Dataclasses shared by the self-evolution pipeline."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# GEPA DataInstance (moved here from deleted src/gepa/)
# ---------------------------------------------------------------------------

@dataclass
class DataInstance:
    """A single test case for the optimization pipeline."""

    id: str
    input: str
    expected_output: str = ""
    metadata: Optional[Dict[str, Any]] = None
    pre_condition: Optional[List[Dict[str, Any]]] = None


def to_plain_dict(obj: Any) -> Dict[str, Any]:
    """Convert a self-evolve dataclass into a JSON-serializable dict."""
    return asdict(obj)


@dataclass
class HermesRunResult:
    """Raw result produced by a Hermes invocation."""

    case_id: str
    command: List[str]
    returncode: int
    stdout: str
    stderr: str
    source: str
    hermes_home: str
    raw_session_path: str
    exported_session_path: Optional[str] = None
    session_id: str = ""
    final_output: str = ""
    error: str = ""


@dataclass
class SessionRecord:
    """Normalized session data. The messages field is the primary artifact."""

    case_id: str
    session_id: str = ""
    source: str = ""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    raw_records: List[Dict[str, Any]] = field(default_factory=list)
    final_output: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TrajectoryStep:
    """One normalized execution step derived from session messages."""

    index: int
    role: str
    action_type: str
    content: str = ""
    tool_name: str = ""
    tool_input: Any = None
    tool_output: Any = None
    success: Optional[bool] = None
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DistilledTrajectory:
    """Structured experience distilled from a raw trajectory."""

    case_id: str
    summary: str = ""
    successful_patterns: List[str] = field(default_factory=list)
    failure_modes: List[str] = field(default_factory=list)
    optimization_suggestions: List[str] = field(default_factory=list)
    key_step_coverage: Dict[str, bool] = field(default_factory=dict)
    trajectory_quality: float = 0.0
    raw_model_output: str = ""


@dataclass
class SessionAnalysisResult:
    """Multi-dimensional session analysis result (三维度：过程 / 精简 / 结果)."""

    case_id: str
    key_steps: Optional[List[Dict[str, Any]]] = None

    # Dimension 1: process (过程精准度)
    process: float = 0.0
    process_details: Dict[str, Any] = field(default_factory=dict)

    # Dimension 2: conciseness (过程简洁度)
    conciseness: float = 0.0
    conciseness_details: Dict[str, Any] = field(default_factory=dict)

    # 结果子类型评分与分析（key: text / tool / file）
    # {"text": 78.0, "tool": 85.0, ...}
    output_scores: Dict[str, float] = field(default_factory=dict)
    output_analyses: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # 兼容字段（轨迹质量，旧字段保留，不再参与计算）
    trajectory_quality: float = 0.0

    # Overall improvement suggestions (综合优化建议)
    overall_improvement_suggestions: str = ""

    # Suggestions
    optimization_suggestions: List[str] = field(default_factory=list)
    failure_modes: List[str] = field(default_factory=list)
    successful_patterns: Optional[List[str]] = None


@dataclass
class ScorerWeights:
    """Scorer weight configuration (overridable via env, see src/config.py)."""

    # 整体维度权重（过程 / 精简 / 结果），三项之和 = 1.0
    judgment_weights: Dict[str, float] = field(default_factory=lambda: {
        "process": float(os.getenv("JUDGMENT_WEIGHT_PROCESS", "0.40")),
        "conciseness": float(os.getenv("JUDGMENT_WEIGHT_CONCISENESS", "0.20")),
        "result": float(os.getenv("JUDGMENT_WEIGHT_RESULT", "0.40")),
    })

    # 方向权重（优化方向分）。当前为死代码（无下游消费者），保留待后续改造，不进 env。
    optimization_weights: Dict[str, float] = field(default_factory=lambda: {
        "process": 0.333,
        "conciseness": 0.333,
        "result": 0.333,
    })


@dataclass
class ScoreBreakdown:
    """Weighted score used to decide whether another optimization round is needed."""

    case_id: str
    total_score: float
    result: float
    process: float
    conciseness: float
    needs_optimization: bool
    feedback: str = ""
    weights: Dict[str, float] = field(default_factory=dict)
    details: Dict[str, Any] = field(default_factory=dict)

    # Multi-dimensional scores (优化方向分)
    sub_type_scores: Dict[str, float] = field(default_factory=dict)

    # 结果子类型评分（各 text/tool/file 的评分）
    output_scores: Dict[str, float] = field(default_factory=dict)

    # Multi-dimensional suggestions
    dimension_suggestions: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class EvolutionRound:
    """Summary for one self-evolution round."""

    round_index: int
    skill_path: str
    case_scores: List[ScoreBreakdown] = field(default_factory=list)
    average_score: float = 0.0
    needs_optimization: bool = False
    optimization_targets: List[str] = field(default_factory=list)
    accepted: bool = True
    candidate_path: str = ""
