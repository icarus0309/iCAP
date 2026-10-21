"""End-to-end self-evolution orchestration — OPT mode only."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..dataset import load_dataset
from ..model_client import ModelClient
from .diagnostics import DiagnosticLogger
from .evaluators.outcome_grouped_evaluator import OutcomeGroupedEvaluator
from .evaluators.components.scorer import WeightedScorer
from .evaluators.components.session_analyzer import SessionAnalyzer
from .execution.hermes_runner import HermesCliConfig, HermesCliRunner, _read_skill_frontmatter_name
from .execution.direct_runner import DirectRunner
from .execution.pre_condition import create_pre_condition_hook
from .execution.session_parser import SessionParser
from .execution.trajectory_extractor import TrajectoryExtractor
from .mutators.structured_mutator import StructuredMutator
from .boundary import BoundaryChecker
from .schemas import (
    DataInstance,
    EvolutionRound,
    HermesRunResult,
    ScoreBreakdown,
    ScorerWeights,
    SessionAnalysisResult,
    TrajectoryStep,
    to_plain_dict,
)


@dataclass
class SelfEvolveConfig:
    """OPT self-evolution configuration."""

    rounds: int = 3
    threshold: float = 85.0
    cases_limit: Optional[int] = None
    output_dir: str = "output"
    run_id: str = ""
    resume_dir: Optional[str] = None
    hermes: HermesCliConfig = field(default_factory=HermesCliConfig)

    enable_pre_condition: bool = False
    mock_ha_url: str = "http://127.0.0.1:8124"
    mock_ha_token: str = "mock-token"

    structured_mutator_max_steps: int = 5
    outcome_split_threshold: float = 70.0
    enable_rejected_experience: bool = True
    success_case_limit: int = 10
    failure_case_limit: int = 30

    scorer_weights: Optional[ScorerWeights] = None
    output_weights: Optional[Dict[str, float]] = None

    # 执行模式：hermes（默认）或 direct（单次 LLM 调用）
    exec_mode: str = "hermes"


# ---------------------------------------------------------------------------
# Per-case result bundle
# ---------------------------------------------------------------------------

@dataclass
class _CaseResult:
    score: Optional[ScoreBreakdown]
    run_result: HermesRunResult
    trajectory: List[TrajectoryStep]
    session_analysis: Optional[SessionAnalysisResult]
    invalid_reason: str = ""
    invalid_detail: str = ""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class SelfEvolveOrchestrator:
    """Run SKILL self-evolution with outcome_grouped evaluator + structured mutator.

    1. Execute all cases (Hermes -> parse -> extract -> analyze -> score)
    2. Evaluate via OutcomeGroupedEvaluator (success/failure grouping)
    3. Check stop condition via BoundaryChecker
    4. Mutate via StructuredMutator (bounded ADD/DELETE/REPLACE edits)
    """

    def __init__(
        self,
        *,
        config: SelfEvolveConfig,
        eval_model_client: Optional[ModelClient],
        gen_model_client: Optional[ModelClient],
        exec_model_client: Optional[ModelClient] = None,
    ):
        self.config = config
        self.eval_model_client = eval_model_client
        self.gen_model_client = gen_model_client
        self.exec_model_client = exec_model_client
        self.parser = SessionParser()
        self.extractor = TrajectoryExtractor()
        self.analyzer = SessionAnalyzer(eval_model_client or self.gen_model_client,
                                        verbose=os.environ.get("ANALYZER_VERBOSE", "") == "1")
        self.logger = DiagnosticLogger()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, skill_path: str, dataset_path: str) -> Dict[str, Any]:
        skill_file = Path(skill_path)
        skill_resource_root = _resolve_skill_resource_root(skill_file)
        dataset = load_dataset(dataset_path)
        data = dataset.to_gepa_data()
        if self.config.cases_limit:
            data = data[: self.config.cases_limit]

        resume_mode = bool(self.config.resume_dir)
        if resume_mode:
            out_root = Path(self.config.resume_dir)
            out_root.mkdir(parents=True, exist_ok=True)
            run_id = self.config.run_id or out_root.name
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_id = self.config.run_id or f"opt-{timestamp}"
            out_root = Path(self.config.output_dir) / f"opt_{skill_file.stem}_{timestamp}"
            out_root.mkdir(parents=True, exist_ok=True)

        self.logger.bind(out_root / "self_evolve.log")
        source = run_id
        self.logger.info(
            "self_evolve_start",
            skill_path=str(skill_file),
            dataset_path=dataset_path,
            cases=len(data),
            rounds=self.config.rounds,
            threshold=self.config.threshold,
            output_dir=str(out_root),
            source=source,
            resume=resume_mode,
        )

        current_skill_path = out_root / "initial_skill.md"
        if not current_skill_path.exists():
            shutil.copy2(skill_file, current_skill_path)

        self.config.hermes.pre_condition_hook = create_pre_condition_hook(
            enabled=self.config.enable_pre_condition,
            url=self.config.mock_ha_url,
            token=self.config.mock_ha_token,
        )
        if self.config.exec_mode == "direct":
            runner = DirectRunner(self.exec_model_client, out_root, source, logger=self.logger)
        else:
            runner = HermesCliRunner(self.config.hermes, out_root, source, logger=self.logger)

        rounds: List[EvolutionRound] = []
        lineage: List[Dict[str, Any]] = []
        final_skill_path = current_skill_path
        best_score = 0.0
        best_skill_path = current_skill_path

        state: Dict[str, Any] = {
            "score_history": [],
            "round_index": 0,
            "rejected_experiences": [],
        }

        start_round = 1
        if resume_mode:
            start_round = _find_first_incomplete_round(out_root, self.config.rounds)
            _apply_resume_state(out_root, state, self.config, skill_file, dataset_path)
            print(f"\n[resume] 复用目录 {out_root}，从第 {start_round}/{self.config.rounds} 轮继续")
            for i in range(1, start_round):
                rdir = out_root / f"round_{i}"
                rsummary = _load_round_summary(rdir)
                if rsummary is None:
                    continue
                avg = rsummary.get("average_score", 0.0)
                state["score_history"].append(avg)
                rounds.append(_reconstruct_evolution_round(rdir, rsummary))
                lineage.append(_build_lineage_entry(i, rdir, rsummary))
                if avg > best_score:
                    best_score = avg
                    best_skill_path = _round_output_skill(rdir)
                final_skill_path = _round_output_skill(rdir)
            if start_round > self.config.rounds:
                print("[resume] 所有轮次均已完成，直接进入收尾")

        for round_index in range(start_round, self.config.rounds + 1):
            _write_state_file(out_root, state, self.config, skill_file, dataset_path)
            print(f"\n{'='*60}")
            print(f"  [OPT] 第 {round_index}/{self.config.rounds} 轮开始")
            print(f"{'='*60}")
            self.logger.info("round_start", round_index=round_index, skill_path=str(final_skill_path))
            round_dir = out_root / f"round_{round_index}"
            skill_round_path = round_dir / "skill" / "SKILL.md"
            skill_text = final_skill_path.read_text(encoding="utf-8")
            _prepare_skill_package(
                skill_path=skill_round_path,
                skill_content=skill_text,
                resource_root=skill_resource_root,
            )

            scorer = WeightedScorer(
                threshold=self.config.threshold,
                weights=self.config.scorer_weights,
                output_weights=self.config.output_weights,
            )

            # ── 1. Execute all cases ──────────────────────────────
            print(f"    执行用例 (共 {len(data)} 条)...")
            run_results: Dict[str, HermesRunResult] = {}
            trajectories: Dict[str, List[TrajectoryStep]] = {}
            session_results: Dict[str, SessionAnalysisResult] = {}
            score_breakdowns: Dict[str, ScoreBreakdown] = {}
            case_scores: List[ScoreBreakdown] = []
            invalid_pool: List[Dict[str, Any]] = []

            for i, instance in enumerate(data, 1):
                print(f"    [{i}/{len(data)}] 用例: {instance.id}")
                case_dir = round_dir / "cases" / str(instance.id)
                case_id = str(instance.id)

                # 续跑：已评分的用例直接从磁盘复用，跳过执行与三维度评分。
                case_result = None
                if resume_mode:
                    case_result = _load_scored_case(case_dir)
                    if case_result is not None:
                        print(f"      [复用] 已评分，跳过执行与评分 (score={case_result.score.total_score:.1f})")

                if case_result is None:
                    run_result = self._execute_case(
                        runner=runner,
                        skill_path=skill_round_path,
                        instance=instance,
                        case_dir=case_dir,
                        resume=resume_mode,
                    )
                    case_result = self._evaluate_case(
                        run_result=run_result,
                        instance=instance,
                        case_dir=case_dir,
                        scorer=scorer,
                    )

                if case_result.invalid_reason:
                    invalid_pool.append({
                        "case_id": case_id,
                        "reason": case_result.invalid_reason,
                        "detail": case_result.invalid_detail,
                        "trajectory_steps": len(case_result.trajectory),
                        "run_error": case_result.run_result.error,
                    })
                    self.logger.info(
                        "case_invalid", round_index=round_index, case_id=case_id,
                        reason=case_result.invalid_reason,
                        detail=case_result.invalid_detail,
                    )
                    print(f"      [无效] {case_result.invalid_reason}: {case_result.invalid_detail}")
                    continue

                run_results[case_id] = case_result.run_result
                trajectories[case_id] = case_result.trajectory
                session_results[case_id] = case_result.session_analysis
                score_breakdowns[case_id] = case_result.score
                case_scores.append(case_result.score)

                self.logger.info(
                    "case_scored", round_index=round_index, case_id=case_id,
                    total_score=case_result.score.total_score,
                    result=case_result.score.result,
                    process=case_result.score.process,
                    conciseness=case_result.score.conciseness,
                    needs_optimization=case_result.score.needs_optimization,
                )
                s = case_result.score
                status = " 需优化" if s.needs_optimization else " 通过"
                print(f"      得分={s.total_score:.1f} 过程={s.process:.1f} 精简={s.conciseness:.1f} 结果={s.result:.1f} {status}")

            (round_dir / "invalid_pool.json").write_text(
                json.dumps(invalid_pool, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if invalid_pool:
                print(f"    ⚠ 无效用例池 {len(invalid_pool)} 条 -> round_{round_index}/invalid_pool.json")

            # ── 2. Evaluate (outcome_grouped) ─────────────────────
            print(f"    评估器 (outcome_grouped) 分析中...")
            state["round_index"] = round_index
            evaluator = OutcomeGroupedEvaluator(
                model_client=self.eval_model_client or self.gen_model_client,
                threshold=self.config.outcome_split_threshold,
                stop_threshold=self.config.threshold,
                enable_rejected_experience=self.config.enable_rejected_experience,
                success_case_limit=self.config.success_case_limit,
                failure_case_limit=self.config.failure_case_limit,
            )
            eval_result = evaluator.evaluate(
                skill=skill_text, run_results=run_results, trajectories=trajectories,
                session_results=session_results, score_breakdowns=score_breakdowns,
                state=state,
            )

            dims = eval_result.dimension_scores
            prev_scores = state["score_history"]
            trend = ""
            if prev_scores:
                prev = prev_scores[-1]
                if eval_result.average_score > prev:
                    trend = f"   (+{eval_result.average_score - prev:.1f})"
                elif eval_result.average_score < prev:
                    trend = f"   (-{prev - eval_result.average_score:.1f})"
                else:
                    trend = "   (持平)"
            print(f"    本轮均分: {eval_result.average_score:.1f} | 过程={dims.get('process', 0):.1f} 精简={dims.get('conciseness', 0):.1f} 结果={dims.get('result', 0):.1f}{trend}")
            state["score_history"].append(eval_result.average_score)

            if eval_result.average_score > best_score:
                best_score = eval_result.average_score
                best_skill_path = skill_round_path
                print(f"    新的历史最佳: {best_score:.1f}")

            targets = [s.feedback for s in case_scores if s.needs_optimization and s.feedback]
            round_summary = EvolutionRound(
                round_index=round_index, skill_path=str(skill_round_path),
                case_scores=case_scores, average_score=round(eval_result.average_score, 4),
                needs_optimization=not eval_result.passed, optimization_targets=targets[:10],
                accepted=True, candidate_path=str(skill_round_path),
            )
            (round_dir / "round_summary.json").write_text(
                json.dumps(to_plain_dict(round_summary), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            rounds.append(round_summary)
            self.logger.info("round_complete", round_index=round_index,
                             average_score=eval_result.average_score,
                             needs_optimization=not eval_result.passed)
            lineage.append({
                "round": round_index, "skill_path": str(skill_round_path),
                "average_score": eval_result.average_score,
                "needs_optimization": not eval_result.passed,
            })

            # ── 3. Boundary check ─────────────────────────────────
            boundary = BoundaryChecker(threshold=self.config.threshold)
            if boundary.check(eval_result, state):
                print(f"    达到阈值 ({eval_result.average_score:.1f} >= {self.config.threshold})，停止优化")
                final_skill_path = skill_round_path
                break
            if round_index == self.config.rounds:
                print(f"    已达最大轮次 {self.config.rounds}，停止优化")
                final_skill_path = skill_round_path
                break

            if not self.gen_model_client:
                final_skill_path = skill_round_path
                lineage[-1]["optimization_skipped"] = "no generation model configured"
                break

            # ── 4. Mutate (structured) ────────────────────────────
            print(f"    变异器 (structured) 优化中...")
            mutator = StructuredMutator(
                model_client=self.gen_model_client,
                max_steps=self.config.structured_mutator_max_steps,
            )
            mutation_result = mutator.mutate(
                skill=skill_text, eval_result=eval_result, data=data, state=state,
            )

            if mutation_result.changed:
                next_path = round_dir / "next_skill.md"
                next_path.write_text(mutation_result.next_skill, encoding="utf-8")
                final_skill_path = next_path
                lineage[-1]["mutation_plan"] = mutation_result.mutation_plan
                if mutation_result.new_rejected:
                    state["rejected_experiences"].extend(mutation_result.new_rejected)
                print(f"    SKILL 已更新 -> {next_path}")
            else:
                final_skill_path = skill_round_path
                lineage[-1]["optimization_skipped"] = "mutator produced no change"
                if mutation_result.new_rejected:
                    state["rejected_experiences"].extend(mutation_result.new_rejected)
                print(f"    单线演进无有效变更，停止优化")
                break

        # ── Finalize ──────────────────────────────────────────────
        print(f"\n{'='*60}")
        print(f"OPT 自优化完成")
        print(f"{'='*60}")
        optimized_path = out_root / "optimized_skill.md"
        shutil.copy2(best_skill_path, optimized_path)
        final_last_path = out_root / "final_skill.md"
        shutil.copy2(final_skill_path, final_last_path)

        last_score = rounds[-1].average_score if rounds else 0.0
        if best_skill_path != final_skill_path:
            print(f"    最后一轮({last_score:.1f})非最佳，已回退到历史最佳版本({best_score:.1f})")

        summary = {
            "run_id": run_id, "source": source, "mode": "opt",
            "output_dir": str(out_root), "optimized_skill_path": str(optimized_path),
            "rounds": [to_plain_dict(r) for r in rounds],
            "final_average_score": last_score, "best_score": best_score,
            "best_skill_path": str(best_skill_path), "threshold": self.config.threshold,
        }
        (out_root / "lineage.json").write_text(
            json.dumps(lineage, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_root / "self_evolve_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        self.logger.info("self_evolve_complete",
                         optimized_skill_path=str(optimized_path),
                         final_average_score=summary["final_average_score"])
        return summary

    # ------------------------------------------------------------------
    # Per-case execution
    # ------------------------------------------------------------------

    def _execute_case(
        self, *, runner, skill_path, instance, case_dir, resume: bool = False,
    ) -> HermesRunResult:
        """执行单个用例；续跑时复用已有检查点，跳过执行。

        direct 模式用 input_messages（json 输入）作为 user prompt；
        hermes 模式沿用 input_text 文本输入。
        """
        if resume:
            checkpoint = case_dir / "run_result.json"
            if checkpoint.exists():
                return _load_run_result(checkpoint, case_dir)
            if (case_dir / "session.raw.jsonl").exists():
                return _reconstruct_run_result(case_dir)

        if self.config.exec_mode == "direct":
            input_messages = (
                instance.metadata.get("input_messages")
                if instance.metadata else None
            )
            return runner.run_case(
                case_id=str(instance.id), skill_path=skill_path,
                input_messages=input_messages, case_dir=case_dir,
            )

        return runner.run_case(
            case_id=str(instance.id), skill_path=skill_path,
            input_text=instance.input, case_dir=case_dir,
            pre_condition=instance.pre_condition,
        )

    def _evaluate_case(
        self, *, run_result, instance, case_dir, scorer,
    ) -> _CaseResult:
        """从 run_result 出发做 parse/extract/analyze/score（续跑时总是重跑）。"""
        session = self.parser.parse(
            case_id=str(instance.id), run_result=run_result,
            export_path=Path(run_result.exported_session_path) if run_result.exported_session_path else None,
            output_path=case_dir / "session.normalized.json",
        )
        self.logger.info("session_normalized", case_id=instance.id,
                         messages=len(session.messages), session_id=session.session_id)

        trajectory = self.extractor.extract(session, output_path=case_dir / "trajectory.json")
        self.logger.info("trajectory_extracted", case_id=instance.id, steps=len(trajectory))

        if not trajectory:
            return _CaseResult(
                score=None, run_result=run_result, trajectory=trajectory,
                session_analysis=None,
                invalid_reason="empty_trajectory",
                invalid_detail=f"轨迹为空（{len(session.messages)} 条会话消息）",
            )

        key_steps = []
        sub_type = "default"
        expected_outputs = None
        input_messages = None
        if instance.metadata:
            key_steps = instance.metadata.get("key_steps", []) or []
            sub_type = instance.metadata.get("sub_type", "default")
            expected_outputs = instance.metadata.get("expected_outputs")
            input_messages = instance.metadata.get("input_messages")

        judgment_weights = (
            self.config.scorer_weights.judgment_weights
            if self.config.scorer_weights else None
        )
        output_weights = self.config.output_weights

        analysis_result = self.analyzer.analyze(
            case_id=str(instance.id), input_text=instance.input,
            input_messages=input_messages,
            trajectory=trajectory, key_steps=key_steps,
            expected_outputs=expected_outputs,
            judgment_weights=judgment_weights,
            output_weights=output_weights,
            output_path=case_dir / "session_analysis.json",
        )
        self.logger.info("session_analyzed", case_id=instance.id,
                         process=analysis_result.process,
                         conciseness=analysis_result.conciseness,
                         output_scores=analysis_result.output_scores)

        eval_error = _evaluation_error_of(analysis_result)
        if eval_error:
            return _CaseResult(
                score=None, run_result=run_result, trajectory=trajectory,
                session_analysis=analysis_result,
                invalid_reason="evaluation_error",
                invalid_detail=eval_error,
            )

        score = scorer.score(
            case_id=str(instance.id), input_text=instance.input,
            sub_type=sub_type, key_steps=key_steps,
            process=analysis_result.process,
            conciseness=analysis_result.conciseness,
            process_details=analysis_result.process_details,
            conciseness_details=analysis_result.conciseness_details,
            output_scores=analysis_result.output_scores,
            output_analyses=analysis_result.output_analyses,
            overall_improvement_suggestions=analysis_result.overall_improvement_suggestions,
            output_weights=output_weights,
            output_path=case_dir / "score.json",
        )
        return _CaseResult(score=score, run_result=run_result,
                          trajectory=trajectory, session_analysis=analysis_result)


# ---------------------------------------------------------------------------
# Standalone helpers
# ---------------------------------------------------------------------------

def _evaluation_error_of(analysis_result: SessionAnalysisResult) -> str:
    """Return a non-empty reason if any dimension failed to evaluate.

    The analyze methods record failures in their ``*_details`` dicts under an
    ``"error"`` key (exception or JSON-parse failure). An empty string means
    all requested dimensions produced a usable result.
    """
    errors: List[str] = []
    for name, details in (
        ("process", analysis_result.process_details),
        ("conciseness", analysis_result.conciseness_details),
    ):
        if isinstance(details, dict) and details.get("error"):
            errors.append(f"{name}: {details['error']}")
    for otype, details in analysis_result.output_analyses.items():
        if isinstance(details, dict) and details.get("error"):
            errors.append(f"{otype}: {details['error']}")
    return "；".join(errors)


# ---------------------------------------------------------------------------
# Resume helpers
# ---------------------------------------------------------------------------

def _find_first_incomplete_round(out_root: Path, total_rounds: int) -> int:
    """Return the first round index lacking a complete marker.

    一个轮次「完整」判定为 round_summary.json 与 next_skill.md 都存在；
    全部完整时返回 total_rounds + 1。
    """
    for i in range(1, total_rounds + 1):
        rdir = out_root / f"round_{i}"
        if not (rdir / "round_summary.json").exists():
            return i
        if not (rdir / "next_skill.md").exists():
            return i
    return total_rounds + 1


def _load_round_summary(rdir: Path) -> Optional[Dict[str, Any]]:
    p = rdir / "round_summary.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _round_output_skill(rdir: Path) -> Path:
    next_skill = rdir / "next_skill.md"
    if next_skill.exists():
        return next_skill
    return rdir / "skill" / "SKILL.md"


def _reconstruct_evolution_round(rdir: Path, summary: Dict[str, Any]) -> EvolutionRound:
    case_scores: List[ScoreBreakdown] = []
    for cs in summary.get("case_scores", []) or []:
        try:
            case_scores.append(ScoreBreakdown(**cs))
        except (TypeError, KeyError):
            pass
    return EvolutionRound(
        round_index=summary.get("round_index", 0),
        skill_path=summary.get("skill_path", str(rdir / "skill" / "SKILL.md")),
        case_scores=case_scores,
        average_score=summary.get("average_score", 0.0),
        needs_optimization=summary.get("needs_optimization", False),
        optimization_targets=summary.get("optimization_targets", []),
        accepted=summary.get("accepted", True),
        candidate_path=summary.get("candidate_path", ""),
    )


def _build_lineage_entry(i: int, rdir: Path, summary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "round": i,
        "skill_path": summary.get("skill_path", str(rdir / "skill" / "SKILL.md")),
        "average_score": summary.get("average_score", 0.0),
        "needs_optimization": summary.get("needs_optimization", False),
    }


def _load_run_result(checkpoint: Path, case_dir: Path) -> HermesRunResult:
    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    # 续跑时运行目录可能被移动/跨机器，落盘时的绝对路径会失效；
    # 始终用当前 case_dir 重新定位会话文件，而不是信任历史绝对路径。
    raw_path = case_dir / "session.raw.jsonl"
    export_path = case_dir / "session.export.jsonl"
    data["raw_session_path"] = str(raw_path)
    data["exported_session_path"] = str(export_path) if export_path.exists() else data.get("exported_session_path")
    return HermesRunResult(**data)


def _load_scored_case(case_dir: Path) -> Optional["_CaseResult"]:
    """续跑时加载已评分用例的检查点，跳过执行与三维度评分。

    需要 score.json / session_analysis.json / trajectory.json / run_result.json
    四个文件齐全且可反序列化才视为可复用；任一缺失或损坏则返回 None，
    走正常执行 + 评分流程。
    """
    score_path = case_dir / "score.json"
    analysis_path = case_dir / "session_analysis.json"
    trajectory_path = case_dir / "trajectory.json"
    run_result_path = case_dir / "run_result.json"
    if not (score_path.exists() and analysis_path.exists()
            and trajectory_path.exists() and run_result_path.exists()):
        return None
    try:
        score = ScoreBreakdown(**json.loads(score_path.read_text(encoding="utf-8")))
        session_analysis = SessionAnalysisResult(
            **json.loads(analysis_path.read_text(encoding="utf-8"))
        )
        trajectory = [
            TrajectoryStep(**step)
            for step in json.loads(trajectory_path.read_text(encoding="utf-8"))
        ]
        run_result = _load_run_result(run_result_path, case_dir)
    except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError):
        return None
    return _CaseResult(
        score=score,
        run_result=run_result,
        trajectory=trajectory,
        session_analysis=session_analysis,
    )


def _reconstruct_run_result(case_dir: Path) -> HermesRunResult:
    """从旧工程的 raw session 文件重建 HermesRunResult（无 run_result.json 时回退）。"""
    raw_path = case_dir / "session.raw.jsonl"
    export_path = case_dir / "session.export.jsonl"
    data: Dict[str, Any] = {}
    if raw_path.exists():
        for line in raw_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                pass
    returncode = data.get("returncode", 0)
    return HermesRunResult(
        case_id=data.get("case_id", case_dir.name),
        command=data.get("command", []),
        returncode=returncode,
        stdout=data.get("stdout", ""),
        stderr=data.get("stderr", ""),
        source=data.get("source", ""),
        hermes_home=data.get("hermes_home", ""),
        raw_session_path=str(raw_path),
        exported_session_path=str(export_path) if export_path.exists() else None,
        session_id="",
        final_output=(data.get("stdout") or "").strip(),
        error="" if returncode == 0 else f"hermes exited with code {returncode}",
    )


def _write_state_file(out_root: Path, state: Dict[str, Any], config: SelfEvolveConfig,
                      skill_file: Path, dataset_path: str) -> None:
    payload = {
        "rejected_experiences": state.get("rejected_experiences", []),
        "config": {
            "rounds": config.rounds,
            "threshold": config.threshold,
            "cases_limit": config.cases_limit,
            "skill_path": str(skill_file),
            "dataset_path": dataset_path,
        },
    }
    try:
        (out_root / "state.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _apply_resume_state(out_root: Path, state: Dict[str, Any], config: SelfEvolveConfig,
                        skill_file: Path, dataset_path: str) -> None:
    p = out_root / "state.json"
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    state["rejected_experiences"] = data.get("rejected_experiences", []) or []
    stored = data.get("config", {}) or {}
    mismatches = []
    if stored.get("skill_path") != str(skill_file):
        mismatches.append(f"skill_path({stored.get('skill_path')} -> {skill_file})")
    if stored.get("dataset_path") != dataset_path:
        mismatches.append(f"dataset_path({stored.get('dataset_path')} -> {dataset_path})")
    if stored.get("rounds") is not None and stored.get("rounds") != config.rounds:
        mismatches.append(f"rounds({stored.get('rounds')} -> {config.rounds})")
    if stored.get("cases_limit") != config.cases_limit:
        mismatches.append(f"cases_limit({stored.get('cases_limit')} -> {config.cases_limit})")
    if mismatches:
        print(f"[resume] ⚠ 参数与上次不一致: {'; '.join(mismatches)}")


def _resolve_skill_resource_root(skill_file: Path) -> Optional[Path]:
    skill_file = skill_file.resolve()
    candidates: List[Path] = []
    if skill_file.name == "SKILL.md":
        candidates.append(skill_file.parent)
    skill_name = _read_skill_frontmatter_name(skill_file)
    if skill_name:
        candidates.append(skill_file.parent / skill_name)
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        if (candidate / "SKILL.md").exists() and (
            (candidate / "scripts").exists()
            or (candidate / "reference").exists()
            or (candidate / "assets").exists()
        ):
            return candidate
    return None


def _prepare_skill_package(*, skill_path, skill_content, resource_root) -> None:
    skill_dir = skill_path.parent
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    skill_dir.mkdir(parents=True, exist_ok=True)
    if resource_root and resource_root.exists():
        shutil.copytree(resource_root, skill_dir, dirs_exist_ok=True,
                        ignore=HermesCliRunner._ignore_skill_entries)
    skill_path.write_text(skill_content, encoding="utf-8")
