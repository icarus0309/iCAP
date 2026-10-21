"""Base evaluator interface."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List
from .result import EvaluationResult
from ..schemas import HermesRunResult, SessionAnalysisResult, ScoreBreakdown, TrajectoryStep


class BaseEvaluator(ABC):
    """评估器接口。

    每种评估器实现一种经验生成策略，分析所有 case 的执行结果，
    输出结构化的 EvaluationResult（经验 + 评分 + 是否达标）。
    """

    @abstractmethod
    def evaluate(
        self,
        *,
        skill: str,
        run_results: Dict[str, HermesRunResult],
        trajectories: Dict[str, List[TrajectoryStep]],
        session_results: Dict[str, SessionAnalysisResult],
        score_breakdowns: Dict[str, ScoreBreakdown],
        state: Dict[str, Any],
    ) -> EvaluationResult:
        """分析所有 case 的执行结果，输出结构化经验。

        Args:
            skill: 当前 SKILL.md 全文。
            run_results: case_id → HermesRunResult。
            trajectories: case_id → 提取后的轨迹步骤列表。
            session_results: case_id → 三维度分析结果。
            score_breakdowns: case_id → 综合评分。
            state: 当前运行状态（轮次、历史分数、上一轮 rejected_experiences 等）。

        Returns:
            EvaluationResult，包含平均分、维度分、是否达标、各类经验文本。
        """
        ...
