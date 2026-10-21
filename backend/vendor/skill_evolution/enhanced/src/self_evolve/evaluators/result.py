"""Evaluation result dataclass."""

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class EvaluationResult:
    """评估器输出。

    所有评估器（distill / direct_analysis / outcome_grouped）都输出此结构。
    不同评估器填充不同字段：
    - 所有: average_score, dimension_scores, passed, case_experiences
    - direct_analysis: + global_experience
    - outcome_grouped: + success_experience, failure_experiences, rejected_experiences
    """

    # ---- 基础评分 ----
    average_score: float                              # 所有 case 的加权平均分
    dimension_scores: Dict[str, float] = field(       # {process, conciseness, result}
        default_factory=dict
    )
    passed: bool = False                              # 是否达到阈值

    # ---- 逐条经验 ----
    case_experiences: List[Dict[str, Any]] = field(default_factory=list)

    # ---- 全局经验 ----
    global_experience: str = ""

    # ---- 成功/失败经验（outcome_grouped 使用） ----
    success_experience: str = ""
    # 失败经验为细粒度列表，每条 {"experience", "trajectory_ids", "trajectory_count"}
    failure_experiences: List[Dict[str, Any]] = field(default_factory=list)

    # ---- 拒绝经验（outcome_grouped 从 state 加载后传递） ----
    rejected_experiences: List[str] = field(default_factory=list)

    # ---- 综合报告 ----
    score_report: str = ""

    # ---- 原始数据 ----
    raw: Dict[str, Any] = field(default_factory=dict)
