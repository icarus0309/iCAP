"""Unified orchestrator — 小步迭代 + 探索性重写 自动切换.

Core loop:
    1. Execute all cases → trajectory → session analysis (4-dim) → score
    2. Evaluate → generate success/failure experiences → merge into pool
    3. Convergence check
    4. Stall detection → choose strategy:
       - structured: select failures, bounded ADD/DELETE/REPLACE
       - exploratory: freeform GEPA rewrite, then back to structured
    5. Version update (accept / rollback + reject)
    6. Pool persistence
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import load_scorer_weights
from .execution.direct_runner import DirectRunner
from .execution.direct_case import evaluate_direct_case
from .evaluation_policy import DIRECT_POLICY, prepare_direct_data, fingerprint, model_identity, read_json, write_json
from .mutators.direct_exploratory_mutator import DirectExploratoryMutator
from .knowledge.direct_knowledge_extractor import extract_direct_knowledge
from ..dataset import load_dataset
from ..model_client import ModelClient
from .diagnostics import DiagnosticLogger
from .experience_pool import ExperiencePool
from .experience_merge import hierarchical_merge_experiences, merge_experience_batch
from .evaluators.outcome_grouped_evaluator import OutcomeGroupedEvaluator
from .evaluators.components.scorer import WeightedScorer
from .evaluators.components.session_analyzer import SessionAnalyzer
from .evaluators.result_first_router import (
    ROUTE_FAILURE,
    ROUTE_INVALID,
    ROUTE_KNOWLEDGE,
    ROUTE_SUCCESS,
    route_result_first,
)
from .execution.hermes_runner import HermesCliConfig, HermesCliRunner, _read_skill_frontmatter_name
from .execution.dual_model_runner import DualModelRunner
from .execution.pre_condition import create_pre_condition_hook
from .execution.session_parser import SessionParser
from .execution.trajectory_extractor import TrajectoryExtractor
from .mutators.structured_mutator import StructuredMutator
from .mutators.freeform_mutator import FreeformMutator
from .boundary import BoundaryChecker
from .knowledge import extract_case_knowledge
from ..scoring.base import SideInfo
from .mutators.gepa import DataInstance
from .schemas import (
    EvolutionRound,
    HermesRunResult,
    ScoreBreakdown,
    SessionAnalysisResult,
    TrajectoryStep,
    to_plain_dict,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class SelfEvolveConfig:
    """Unified self-evolution configuration."""

    rounds: int = 6
    threshold: float = 85.0
    cases_limit: Optional[int] = None
    output_dir: str = "output"
    run_id: str = ""

    exec_mode: str = "hermes"
    direct_judge_prompt: str = ""
    exec_model_client: Any = None
    large_exec_model_client: Any = None

    def __post_init__(self):
        if self.exec_mode not in {"hermes", "direct"}:
            raise ValueError("exec_mode must be hermes or direct")
        if self.exec_mode == "direct":
            if isinstance(self.gepa_iterations, bool) or not isinstance(self.gepa_iterations, int) or self.gepa_iterations <= 0:
                raise ValueError("direct GEPA_ITERATIONS must be a positive integer")
            self.gepa_iterations = min(self.gepa_iterations, 3)

    # Hermes
    hermes: HermesCliConfig = field(default_factory=HermesCliConfig)

    # Dual-model mode: when set, the small model (`hermes`) runs first and the
    # large model (`large_hermes`) only runs when the small model's result fails.
    large_hermes: Optional[HermesCliConfig] = None

    # Result-first routing threshold: a result score at/above this value counts
    # as "correct" (aligns with distill's result_failure_threshold = 80).
    result_threshold: float = 80.0

    # Pre-condition
    enable_pre_condition: bool = False
    mock_ha_url: str = "http://127.0.0.1:8124"
    mock_ha_token: str = "mock-token"

    # Structured mutator
    max_steps: int = 5
    outcome_split_threshold: float = 70.0

    # GEPA (exploratory rewrite)
    gepa_iterations: int = 3
    gepa_minibatch_size: int = 5
    gepa_max_metric_calls: Optional[int] = None

    # Strategy switch
    stall_threshold: float = 2.0   # 连续提升 < 此值视为停滞
    stall_rounds: int = 2           # 连续停滞轮数触发探索性重写


# ---------------------------------------------------------------------------
# Per-case result bundle
# ---------------------------------------------------------------------------

@dataclass
class _CaseResult:
    score: ScoreBreakdown
    run_result: HermesRunResult
    trajectory: List[TrajectoryStep]
    session_analysis: SessionAnalysisResult


@dataclass
class _RoutedCaseResult:
    """One case's result-first routing outcome (dual-model mode).

    ``score`` / ``session_analysis`` / ``trajectory`` / ``run_result`` are the
    primary small-model artifacts (full three-dimension eval) consumed by the
    evaluator and the resume logic, keeping the same shape as the legacy
    single-model path. ``large_*`` fields are populated only when the large
    model ran (KNOWLEDGE or both-failed FAILURE), and are needed by the
    knowledge extractor (P2).
    """

    case_id: str
    route: str
    score: ScoreBreakdown
    run_result: HermesRunResult
    trajectory: List[TrajectoryStep]
    session_analysis: SessionAnalysisResult
    routing: Dict[str, Any] = field(default_factory=dict)
    small_result_score: Optional[ScoreBreakdown] = None
    large_score: Optional[ScoreBreakdown] = None
    large_run_result: Optional[HermesRunResult] = None
    large_trajectory: Optional[List[TrajectoryStep]] = None
    large_session_analysis: Optional[SessionAnalysisResult] = None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class SelfEvolveOrchestrator:
    """Unified orchestrator with strategy switching.

    Default: structured (small-step) mutator.
    On stall: exploratory (freeform/GEPA) mutator, then back to structured.
    """

    def __init__(
        self,
        *,
        config: SelfEvolveConfig,
        eval_model_client: Optional[ModelClient],
        gen_model_client: Optional[ModelClient],
        progress_callback: Any = None,
        stop_checker: Any = None,
        model_info: Optional[Dict[str, str]] = None,
    ):
        self.config = config
        self.eval_model = eval_model_client
        self.gen_model = gen_model_client
        self._on_progress = progress_callback  # callable(event_type, data)
        self._stop_checker = stop_checker      # callable() -> bool
        self._model_info = model_info or {}
        self.parser = SessionParser()
        self.extractor = TrajectoryExtractor()
        self.analyzer = SessionAnalyzer(
            eval_model_client or gen_model_client,
            verbose=os.environ.get("ANALYZER_VERBOSE", "") == "1",
        )
        self.logger = DiagnosticLogger()

    def _emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """Emit a progress event to the callback if set."""
        data["event"] = event_type
        data["timestamp"] = time.time()
        if self._on_progress:
            try:
                self._on_progress(event_type, data)
            except Exception:
                pass  # never let callback errors crash the orchestrator

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, skill_path: str, dataset_path: str, resume_from: Optional[str] = None) -> Dict[str, Any]:
        skill_file = Path(skill_path)
        if not skill_file.is_file():
            raise ValueError(f"Skill 路径不是有效文件: {skill_path!r}")
        skill_resource_root = _resolve_skill_resource_root(skill_file)
        dataset = load_dataset(dataset_path)
        data = dataset.to_gepa_data()
        if self.config.cases_limit:
            data = data[: self.config.cases_limit]
        if self.config.exec_mode == "direct":
            if not self.config.exec_model_client or not self.analyzer.model_client:
                raise ValueError("direct requires execution and judge model clients")
            evaluation_set_id = prepare_direct_data(data, self.config.direct_judge_prompt)
        else:
            evaluation_set_id = fingerprint([str(i.id) for i in data])
        run_policy = {"execution_mode": self.config.exec_mode, "evaluation_set_id": evaluation_set_id}
        if self.config.exec_mode == "direct":
            run_policy.update(DIRECT_POLICY)
            run_policy.update(small_model=model_identity(self.config.exec_model_client),
                              large_model=model_identity(self.config.large_exec_model_client),
                              judge_model=model_identity(self.analyzer.model_client),
                              result_threshold=self.config.result_threshold, stop_threshold=self.config.threshold,
                              cases=[{"id": str(i.id), "metadata": i.metadata} for i in data])
        run_policy_id = fingerprint(run_policy)
        instances_by_id = {str(instance.id): instance for instance in data}

        # ── Output / resume detection ──────────────────────────────
        resume = resume_from is not None
        if resume:
            out_root = Path(resume_from)
            if not out_root.is_dir():
                raise ValueError(f"续跑目录不存在: {resume_from!r}")
            ckpt_path = out_root / "checkpoint.json"
            if ckpt_path.exists():
                ckpt = json.loads(ckpt_path.read_text(encoding="utf-8"))
            else:
                # 检查文件检测：无 checkpoint.json 时按输出目录内容推断断点
                print("  [续跑] 未找到 checkpoint.json，按输出目录内容检测断点...")
                ckpt = _reconstruct_checkpoint_from_files(
                    out_root, skill_file, self.config.rounds)
                previous_meta = read_json(out_root / "run_meta.json")
                ckpt["execution_mode"] = previous_meta.get("execution_mode", "hermes")
                ckpt["run_policy_id"] = previous_meta.get("run_policy_id")
            previous_mode = ckpt.get("execution_mode", "hermes")
            if previous_mode != self.config.exec_mode:
                raise ValueError("续跑执行模式不同，请使用新的输出目录")
            if self.config.exec_mode == "direct" and ckpt.get("run_policy_id") != run_policy_id:
                initial = out_root / "initial_skill.md"
                initial_text = initial.read_text(encoding="utf-8") if initial.exists() else skill_file.read_text(encoding="utf-8")
                ckpt.update(done=False, completed_round=0, final_skill=initial_text, best_skill=initial_text,
                            best_score=0.0, score_history=[], lineage=[], pool={}, comparison_case_ids=None)
            if ckpt.get("done"):
                raise ValueError(f"该运行已完整结束，无需续跑: {out_root}")
            run_id = ckpt.get("run_id") or self.config.run_id or out_root.name
            start_round = int(ckpt.get("completed_round", 0)) + 1
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_id = self.config.run_id or f"evo-{timestamp}"
            out_root = Path(self.config.output_dir) / f"evo_{skill_file.stem}_{timestamp}"
            out_root.mkdir(parents=True, exist_ok=True)
            ckpt = None
            start_round = 1
            # 运行元信息：无 checkpoint.json 时也可按文件恢复（web 续跑读取）
            (out_root / "run_meta.json").write_text(
                json.dumps({
                    "version": 1,
                    "run_id": run_id,
                    "skill_path": str(skill_file),
                    "dataset_path": dataset_path,
                    "total_rounds": self.config.rounds,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        meta = read_json(out_root / "run_meta.json")
        meta.update(execution_mode=self.config.exec_mode, run_policy_id=run_policy_id,
                    evaluation_policy=run_policy, gepa_iterations=self.config.gepa_iterations)
        write_json(out_root / "run_meta.json", meta)
        self.logger.bind(out_root / "self_evolve.log")

        self.logger.info("start", skill_path=str(skill_file), cases=len(data),
                         rounds=self.config.rounds, threshold=self.config.threshold,
                         resume=resume, start_round=start_round)

        # Init skill (single-file)
        if resume:
            final_skill_path = out_root / "resume_final.md"
            final_skill_path.write_text(ckpt["final_skill"], encoding="utf-8")
            best_skill_path = out_root / "resume_best.md"
            best_skill_path.write_text(ckpt["best_skill"], encoding="utf-8")
        else:
            current_skill_path = out_root / "initial_skill.md"
            shutil.copy2(skill_file, current_skill_path)
            best_skill_path = current_skill_path
            final_skill_path = current_skill_path

        # Runner(s)
        dual_runner = None
        large_runner = None
        if self.config.exec_mode == "direct":
            runner = DirectRunner(self.config.exec_model_client, out_root, run_id, model_role="small")
            if self.config.large_exec_model_client:
                large_runner = DirectRunner(self.config.large_exec_model_client, out_root, run_id, model_role="large")
        else:
            self.config.hermes.pre_condition_hook = create_pre_condition_hook(
                enabled=self.config.enable_pre_condition,
                url=self.config.mock_ha_url,
                token=self.config.mock_ha_token,
            )
            dual_runner: Optional[DualModelRunner] = None
            if self.config.large_hermes is not None:
                self.config.large_hermes.pre_condition_hook = self.config.hermes.pre_condition_hook
                dual_runner = DualModelRunner.create(
                    small_config=self.config.hermes,
                    large_config=self.config.large_hermes,
                    run_dir=out_root,
                    run_id=run_id,
                    logger=self.logger,
                )
                # Reuse the isolated small runner for the primary path + GEPA eval.
                runner = dual_runner.small
            else:
                runner = HermesCliRunner(self.config.hermes, out_root, run_id, logger=self.logger)

            if dual_runner:
                large_runner = dual_runner.large

        # Experience pool
        pool_path = out_root / "experience_pool.json"
        if resume:
            pool = ExperiencePool.from_dict(ckpt.get("pool", {}), model_client=self.eval_model)
        else:
            pool = ExperiencePool(model_client=self.eval_model)

        # State
        if resume:
            score_history: List[float] = list(ckpt.get("score_history", []))
            best_score = float(ckpt.get("best_score", 0.0))
            lineage: List[Dict[str, Any]] = list(ckpt.get("lineage", []))
        else:
            score_history: List[float] = []
            best_score = 0.0
            lineage: List[Dict[str, Any]] = []
        comparison_case_ids = ckpt.get("comparison_case_ids") if resume else None
        prev_skill_path = final_skill_path
        state: Dict[str, Any] = {"score_history": score_history,
                                 "rejected_experiences": pool.get_rejected() if resume else []}

        def save_ckpt(*, done: bool, completed_round: int) -> None:
            """Write a resume checkpoint capturing all in-memory state."""
            payload = {
                "version": 1,
                "execution_mode": self.config.exec_mode,
                "run_policy_id": run_policy_id,
                "run_id": run_id,
                "skill_path": skill_path,
                "dataset_path": dataset_path,
                "done": done,
                "completed_round": completed_round,
                "total_rounds": self.config.rounds,
                "final_skill": final_skill_path.read_text(encoding="utf-8"),
                "best_skill": best_skill_path.read_text(encoding="utf-8"),
                "best_score": best_score,
                "comparison_case_ids": comparison_case_ids,
                "score_history": list(score_history),
                "lineage": list(lineage),
                "pool": pool.to_dict(),
            }
            (out_root / "checkpoint.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        self._emit("start", {
            "skill_path": str(skill_file), "dataset_path": dataset_path,
            "cases": len(data), "rounds": self.config.rounds,
            "threshold": self.config.threshold, "output_dir": str(out_root),
            "resume": resume, "start_round": start_round,
            "execution_mode": self.config.exec_mode,
            "evaluated_dimensions": ["result"] if self.config.exec_mode == "direct" else ["process", "conciseness", "result"],
            "gen_model": self._model_info.get("gen_model", ""),
            "eval_model": self._model_info.get("eval_model", ""),
            "hermes_model": self._model_info.get("hermes_model", ""),
            "max_steps": self.config.max_steps,
            "stall_threshold": self.config.stall_threshold,
            "stall_rounds": self.config.stall_rounds,
            "enable_pre_condition": self.config.enable_pre_condition,
            "mock_ha_url": self.config.mock_ha_url,
        })

        # 初始断点：即使首轮执行中崩溃也能续跑
        if not resume:
            save_ckpt(done=False, completed_round=0)

        for round_idx in range(start_round, self.config.rounds + 1):
            if self._stop_checker and self._stop_checker():
                print(f"  收到停止信号，结束于第 {round_idx - 1} 轮")
                save_ckpt(done=False, completed_round=round_idx - 1)
                self._emit("error", {"message": "用户手动停止"})
                break
            round_start_time = time.time()
            self._emit("round_start", {"round": round_idx, "total_rounds": self.config.rounds})
            print(f"\n{'='*60}")
            print(f"  Round {round_idx}/{self.config.rounds}")
            print(f"{'='*60}")

            round_dir = out_root / f"round_{round_idx}"
            skill_round_path = round_dir / "skill" / "SKILL.md"
            skill_text = final_skill_path.read_text(encoding="utf-8")
            _prepare_skill_package(
                skill_path=skill_round_path,
                skill_content=skill_text,
                resource_root=skill_resource_root,
            )

            scorer = WeightedScorer(threshold=self.config.threshold, weights=load_scorer_weights())

            # 每轮失败/知识池重置：本轮经验只统计本轮轨迹，不跨轮合并
            pool.clear_failure_pool()

            # ── 1. Execute all cases ──────────────────────────────
            print(f"  执行用例 ({len(data)} 条)...")
            self._emit("stage", {"round": round_idx, "phase": "executing",
                                  "message": f"执行用例 ({len(data)} 条)..."})
            case_scores: List[ScoreBreakdown] = []
            score_breakdowns: Dict[str, ScoreBreakdown] = {}
            session_results: Dict[str, SessionAnalysisResult] = {}
            run_results: Dict[str, HermesRunResult] = {}
            trajectories: Dict[str, List[TrajectoryStep]] = {}
            routing_results: Dict[str, Dict[str, Any]] = {}
            routed_results: Dict[str, _RoutedCaseResult] = {}

            for i, instance in enumerate(data, 1):
                cid = str(instance.id)
                case_dir = round_dir / "cases" / cid
                if self.config.exec_mode == "direct" or large_runner is not None:
                    routed = self._run_one_case_result_first(
                        small_runner=runner,
                        large_runner=large_runner,
                        skill_path=skill_round_path,
                        instance=instance,
                        case_dir=case_dir,
                        scorer=scorer,
                        result_threshold=self.config.result_threshold,
                        allow_reuse=(self.config.exec_mode == "direct" or (resume and round_idx == start_round)),
                    )
                    route = routed.route
                    routing_results[cid] = routed.routing
                    routed_results[cid] = routed
                else:
                    # 断点续跑：仅首个续跑轮次允许复用已完成用例；评估/变异总重跑
                    case_result = self._run_one_case_or_reuse(
                        runner=runner, skill_path=skill_round_path,
                        instance=instance, case_dir=case_dir,
                        scorer=scorer,
                        allow_reuse=(self.config.exec_mode == "direct" or (resume and round_idx == start_round)),
                    )
                    routed = self._to_routed(case_result)
                    route = routed.route
                    routing_results[cid] = routed.routing
                    routed_results[cid] = routed
                run_results[cid] = routed.run_result
                trajectories[cid] = routed.trajectory
                session_results[cid] = routed.session_analysis
                score_breakdowns[cid] = routed.score
                case_scores.append(routed.score)

                s = routed.score
                status = "✗" if s.needs_optimization else "✓"
                dims_text = " ".join(f"{dim}={getattr(s, dim):.1f}" for dim in s.evaluated_dimensions)
                print(f"    [{i}/{len(data)}] {instance.id} | route={route} | {s.total_score:.1f} {dims_text} {status}")

                rr = routed.run_result
                tr = routed.trajectory
                self._emit("case_done", {
                    "round": round_idx, "case_id": cid, "index": i, "total": len(data),
                    "route": route,
                    "total_score": s.total_score,
                    "execution_mode": s.execution_mode,
                    "evaluated_dimensions": s.evaluated_dimensions,
                    "process": s.process if "process" in s.evaluated_dimensions else None,
                    "conciseness": s.conciseness if "conciseness" in s.evaluated_dimensions else None,
                    "judge": routed.session_analysis.output_analyses,
                    "result": s.result,
                    "trajectory_steps": len(tr),
                    "final_output": (rr.final_output or "") if self.config.exec_mode == "direct" else (rr.final_output or "")[:300],
                    "returncode": rr.returncode,
                    "error": (s.evaluation_error or rr.error)[:200],
                    "hermes_output": (rr.stdout or "")[-500:] if rr.stdout else "",
                })

            # ── 1.5 Collect invalid cases into the invalid pool ────
            # 无效用例（轨迹为空 / 评估解析失败重试仍失败）仅记录，不参与变异。
            round_invalid: List[Dict[str, str]] = []
            for cid, routed in routed_results.items():
                if routed.route != ROUTE_INVALID:
                    continue
                reason = routed.routing.get("reason", "unknown")
                errs = routed.routing.get("evaluation_errors") or {}
                detail = errs.get("small") or errs.get("large") or ""
                entry = f"[{cid}] {reason}"
                if detail:
                    entry += f" ({detail})"
                pool.merge_invalid(entry, round_idx)
                round_invalid.append({"case_id": cid, "reason": reason, "detail": detail})
            if round_invalid:
                (round_dir / "invalid_cases.json").write_text(
                    json.dumps(round_invalid, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  无效用例 {len(round_invalid)} 条已记录到无效池")

            # ── 2. Evaluate & merge experiences ───────────────────
            print(f"  评估器分析中...")
            self._emit("stage", {"round": round_idx, "phase": "evaluating",
                                  "message": "评估器分析中..."})
            state["round_index"] = round_idx
            state["score_history"] = score_history
            state["rejected_experiences"] = pool.get_rejected()

            evaluator = OutcomeGroupedEvaluator(
                model_client=self.eval_model or self.gen_model,
                threshold=self.config.outcome_split_threshold,
                stop_threshold=self.config.threshold,
                result_threshold=self.config.result_threshold,
                execution_mode=self.config.exec_mode,
            )

            # INVALID（评估/执行出错）用例不参与成功/失败经验，也不拉低均分。
            valid_cids = {cid for cid, r in routed_results.items() if r.route != ROUTE_INVALID}
            if comparison_case_ids is None and valid_cids:
                comparison_case_ids = sorted(valid_cids)
            comparable = self.config.exec_mode != "direct" or sorted(valid_cids) == comparison_case_ids
            if not comparable:
                state["score_history"] = []
            eval_score_breakdowns = {cid: s for cid, s in score_breakdowns.items() if cid in valid_cids}
            eval_run_results = {cid: rr for cid, rr in run_results.items() if cid in valid_cids}
            eval_trajectories = {cid: t for cid, t in trajectories.items() if cid in valid_cids}
            eval_session_results = {cid: s for cid, s in session_results.items() if cid in valid_cids}

            eval_result = evaluator.evaluate(
                skill=skill_text, run_results=eval_run_results, trajectories=eval_trajectories,
                session_results=eval_session_results, score_breakdowns=eval_score_breakdowns,
                state=state,
            )

            avg_score = eval_result.average_score
            if comparable and valid_cids:
                score_history.append(avg_score)
            if not comparable:
                eval_result.passed = False

            # Merge into pool
            if eval_result.success_experience:
                pool.merge_success(eval_result.success_experience, round_idx)
            for exp in eval_result.failure_experiences:
                pool.merge_failure(exp, round_idx, origin="failure")

            # Extract contrastive-trace knowledge for KNOWLEDGE cases (structured root_cause + suggestions)
            knowledge_leaves: List[Dict[str, Any]] = []
            if large_runner is not None:
                for cid, routed in routed_results.items():
                    if routed.route != ROUTE_KNOWLEDGE:
                        continue
                    lesson = self._extract_knowledge_lesson(
                        routed=routed,
                        instance=instances_by_id.get(cid),
                        round_dir=round_dir,
                        skill_text=skill_text,
                    )
                    if lesson:
                        knowledge_leaves.append({
                            "root_cause": lesson["root_cause"],
                            "suggestions": lesson["suggestions"],
                            "trajectory_ids": [cid],
                            "trajectory_count": 1,
                        })
            if knowledge_leaves:
                self._merge_knowledge_leaves(knowledge_leaves, round_idx, pool)

            dims = eval_result.dimension_scores
            trend = _trend_str(score_history)
            print(f"  均分: {avg_score:.1f} | " + " ".join(f"{k}={v:.1f}" for k, v in dims.items()) + trend)
            print(f"  {pool.summary()}")

            self._emit("eval_done", {
                "round": round_idx, "average_score": avg_score,
                "dimension_scores": dims, "passed": eval_result.passed,
                "pool": pool.to_dict(),
            })

            # Track best
            if comparable and valid_cids and avg_score > best_score:
                best_score = avg_score
                best_skill_path = skill_round_path
                print(f"  新最佳: {best_score:.1f}")

            # Save round summary
            round_summary = _save_round_summary(round_dir, round_idx, skill_round_path,
                                                case_scores, avg_score, eval_result.passed)
            pool.save(pool_path)

            lineage.append({
                "round": round_idx, "skill_path": str(skill_round_path),
                "average_score": avg_score, "passed": eval_result.passed,
                "pool_summary": pool.summary(),
            })

            # ── 3. Convergence check ──────────────────────────────
            boundary = BoundaryChecker(threshold=self.config.threshold)
            stop_reason = None
            if comparable and valid_cids and boundary.check(eval_result, state):
                stop_reason = f"达标 ({avg_score:.1f} >= {self.config.threshold})"
            elif not valid_cids:
                stop_reason = "无有效评估结果"
            elif not comparable:
                stop_reason = "有效评估案例集合变化，无法与历史分数比较"
            elif round_idx == self.config.rounds:
                stop_reason = "达到最大轮次"
            elif not self.gen_model:
                stop_reason = "无生成模型"

            if stop_reason:
                print(f"  {stop_reason}，停止")
                final_skill_path = skill_round_path
                save_ckpt(done=True, completed_round=round_idx)
                break

            # ── 4. Strategy selection ─────────────────────────────
            strategy = _select_strategy(score_history, self.config)
            prev_skill_path = skill_round_path
            self._emit("strategy", {"round": round_idx, "strategy": strategy,
                                     "score_history": score_history})

            if strategy == "exploratory":
                # ── Exploratory rewrite ─────────────────────────
                self._emit("stage", {"round": round_idx, "phase": "mutating",
                                      "message": "探索性重写中..."})
                print(f"\n  🔀 检测到停滞，触发探索性重写...")
                # Best known score from history (0-100 scale), used as early-stop target
                current_best = max(score_history) if score_history else 0.0
                print(f"     当前最优={current_best:.1f}, 成功约束={len(pool.get_all_success())}条, "
                      f"失败经验={len([e for e in pool.failure_pool if e.status == 'active'])}条")

                if self.config.exec_mode == "direct":
                    def evaluate_rewrite(prompt, attempt):
                        attempt_dir = round_dir / "direct_exploration" / f"attempt_{attempt}"
                        attempt_skill = attempt_dir / "SKILL.md"
                        attempt_skill.write_text(prompt, encoding="utf-8")
                        results = {}
                        for instance in data:
                            rr, tr, sa, score = evaluate_direct_case(runner=runner, analyzer=self.analyzer,
                                scorer=scorer, skill_path=attempt_skill, instance=instance,
                                case_dir=attempt_dir / "cases" / str(instance.id),
                                result_threshold=self.config.result_threshold)
                            results[str(instance.id)] = score
                        return results
                    mutator = DirectExploratoryMutator(model_client=self.gen_model, evaluate=evaluate_rewrite,
                        output_dir=round_dir / "direct_exploration", iterations=self.config.gepa_iterations,
                        max_metric_calls=self.config.gepa_max_metric_calls, progress_callback=self._emit,
                        stop_checker=self._stop_checker)
                    state["success_constraints"] = pool.get_success_texts()
                    mutation_result = mutator.mutate(skill=skill_text, eval_result=eval_result, data=data,
                        state=state, baseline_scores=eval_score_breakdowns, cache_context=run_policy_id)
                else:
                    # Build skill executor for GEPA internal evaluation
                    skill_executor = HermesSkillExecutor(
                        runner=runner, round_dir=round_dir,
                        skill_resource_root=skill_resource_root,
                        parser=self.parser, extractor=self.extractor,
                        analyzer=self.analyzer, scorer=scorer,
                    )

                    mutator = FreeformMutator(
                        model_client=self.gen_model,
                        gen_model_client=self.gen_model,
                        gepa_evaluator=self._score_from_executor_feedback,
                        skill_executor=skill_executor,
                        output_dir=str(round_dir / "gepa"),
                        iterations=self.config.gepa_iterations,
                        minibatch_size=self.config.gepa_minibatch_size,
                        max_metric_calls=self.config.gepa_max_metric_calls,
                        enable_merge=False,
                        progress_callback=self._on_progress,
                    )

                    # Build root_scores from the round's already-executed results
                    # to avoid re-running all cases in GEPA for the current skill.
                    # Also carry the full ScoreBreakdown side-info so the root
                    # candidate retains diagnosis feedback (otherwise the first GEPA
                    # mutation — whose parent is always the root — runs blind).
                    root_scores: Dict[str, float] = {}
                    root_side_infos: Dict[str, Any] = {}
                    for instance, sc in zip(data, case_scores):
                        cid = str(instance.id)
                        root_scores[cid] = sc.total_score
                        root_side_infos[cid] = SideInfo(
                            sc.total_score / 100.0, sc.feedback,
                            {"self_evolve_score": to_plain_dict(sc)},
                        )

                    mutation_result = mutator.mutate(
                        skill=skill_text, eval_result=eval_result,
                        data=data, state=state, root_scores=root_scores,
                        root_side_infos=root_side_infos,
                        target_score=current_best,
                    )

                # After exploratory rewrite, reset rejected experiences: the old
                # root's rejections no longer apply, but this round's rejected
                # mutations are new-node content and should be recorded.
                if self.config.exec_mode != "direct" or mutation_result.changed:
                    pool.clear_rejected()
                for r in mutation_result.new_rejected:
                    if r not in pool.get_rejected():
                        pool.add_rejected(r)
                state["rejected_experiences"] = pool.get_rejected()
                if self.config.exec_mode != "direct" or mutation_result.changed:
                    print(f"  已更新版本及拒绝经验（当前 {len(pool.get_rejected())} 条）")

                lineage[-1]["strategy"] = "exploratory"

                if mutation_result.changed:
                    next_path = round_dir / "next_skill.md"
                    next_path.write_text(mutation_result.next_skill, encoding="utf-8")
                    lineage[-1]["candidate_score"] = mutation_result.raw.get("candidate_score")
                    if self.config.exec_mode == "direct" and mutation_result.raw["candidate_score"] > best_score:
                        best_score = mutation_result.raw["candidate_score"]
                        best_skill_path = next_path
                    print(f"  探索性重写完成 → {next_path}")
                else:
                    print(f"  探索性重写无变更，保持当前版本")
                    next_path = skill_round_path

                final_skill_path = next_path

                self._emit("mutate_done", {
                    "round": round_idx, "strategy": "exploratory",
                    "changed": mutation_result.changed,
                    "candidate_score": mutation_result.raw.get("candidate_score"),
                    "action_count": mutation_result.raw.get("total_metric_calls", 1) if mutation_result.raw else 1,
                })

            else:
                # ── Structured (small-step) ──────────────────────
                self._emit("stage", {"round": round_idx, "phase": "mutating",
                                      "message": "小步迭代修改中..."})
                selected = pool.select_failure(self.config.max_steps)
                all_success = pool.get_success_texts()

                if not selected:
                    print(f"  无待处理的失败经验，跳过本轮修改")
                    final_skill_path = skill_round_path
                    continue

                print(f"\n  📝 小步迭代: 失败 {len(selected)} 条")
                for item in selected:
                    print(f"     [失败] [{item.id}] {item.content[:100]}")

                mutator = StructuredMutator(
                    model_client=self.gen_model,
                    max_steps=self.config.max_steps,
                )

                mutation_result = mutator.mutate(
                    skill=skill_text, eval_result=eval_result,
                    data=data, state=state,
                    selected_failures=[
                        {"id": e.id, "content": e.content} for e in selected
                    ],
                    all_success=all_success,
                )

                lineage[-1]["strategy"] = "structured"

                if mutation_result.changed:
                    next_path = round_dir / "next_skill.md"
                    next_path.write_text(mutation_result.next_skill, encoding="utf-8")
                    lineage[-1]["mutation_plan"] = mutation_result.mutation_plan
                    print(f"  小步修改完成 → {next_path}")
                else:
                    print(f"  小步迭代无变更")
                    next_path = skill_round_path

                final_skill_path = next_path

                # Handle rejected from evaluator
                for r in eval_result.rejected_experiences:
                    if r not in pool.get_rejected():
                        pool.add_rejected(r)

                self._emit("mutate_done", {
                    "round": round_idx, "strategy": "structured",
                    "changed": mutation_result.changed,
                    "action_count": mutation_result.raw.get("action_count", 0) if mutation_result.raw else 0,
                    "mutation_plan": mutation_result.mutation_plan,
                    "selected_failure_ids": [e.id for e in selected],
                })

            # ── 5. Round done ─────────────────────────────────────
            round_elapsed = time.time() - round_start_time
            self._emit("round_done", {
                "round": round_idx, "average_score": avg_score,
                "best_score": best_score, "elapsed": round_elapsed,
                "pool": pool.to_dict(),
            })

            # 每轮完整结束后落盘断点（评估+变异都已完成），供中断后续跑
            save_ckpt(done=False, completed_round=round_idx)

            # 每轮经验池快照：供断点续跑重建 + web 历史展示（成功/失败/拒绝按轮保留）
            pool.save(round_dir / "experience_pool.json")

        # ── Finalize ──────────────────────────────────────────────
        print(f"\n{'='*60}")
        print(f"自优化完成")
        print(f"{'='*60}")

        optimized_path = out_root / "optimized_skill.md"
        shutil.copy2(best_skill_path, optimized_path)
        final_last_path = out_root / "final_skill.md"
        shutil.copy2(final_skill_path, final_last_path)
        pool.save(pool_path)

        last_score = score_history[-1] if score_history else 0.0
        if best_skill_path != final_skill_path:
            print(f"  最后一轮({last_score:.1f})非最佳，已退回到历史最佳({best_score:.1f})")

        summary = {
            "run_id": run_id,
            "output_dir": str(out_root),
            "optimized_skill_path": str(optimized_path),
            "final_average_score": last_score,
            "best_score": best_score,
            "best_skill_path": str(best_skill_path),
            "threshold": self.config.threshold,
            "score_history": score_history,
            "execution_mode": self.config.exec_mode,
            "evaluation_set_id": evaluation_set_id,
            "status": "evaluated" if score_history else "no_valid_results",
            "lineage": lineage,
        }
        (out_root / "lineage.json").write_text(
            json.dumps(lineage, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_root / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"  最终得分: {last_score:.1f} | 最佳: {best_score:.1f} (阈值: {summary['threshold']})")
        print(f"  {pool.summary()}")
        print(f"  输出: {out_root}")

        self._emit("complete", summary)
        return summary

    # ------------------------------------------------------------------
    # Per-case execution
    # ------------------------------------------------------------------

    def _run_one_case_or_reuse(
        self, *, runner, skill_path, instance, case_dir, scorer,
        allow_reuse=False,
    ) -> _CaseResult:
        """Run a case, or reuse a completed one during the first resumed round."""
        cid = str(instance.id)
        if allow_reuse and _case_is_complete(case_dir):
            try:
                return self._load_case_result(cid, case_dir)
            except Exception as exc:
                print(f"    [续跑] 复用用例 {cid} 失败({type(exc).__name__}: {exc})，改为重新执行")
        return self._run_one_case(
            runner=runner, skill_path=skill_path, instance=instance,
            case_dir=case_dir, scorer=scorer,
        )

    def _load_case_result(self, case_id: str, case_dir: Path) -> _CaseResult:
        """Reconstruct a completed case's result from on-disk artifacts."""
        score = ScoreBreakdown(**json.loads((case_dir / "score.json").read_text(encoding="utf-8")))
        sa = SessionAnalysisResult(**json.loads((case_dir / "session_analysis.json").read_text(encoding="utf-8")))
        traj = [TrajectoryStep(**t) for t in json.loads((case_dir / "trajectory.json").read_text(encoding="utf-8"))]
        rr_path = case_dir / "run_result.json"
        if rr_path.exists():
            rr = HermesRunResult(**json.loads(rr_path.read_text(encoding="utf-8")))
        else:
            rr = HermesRunResult(case_id=case_id, command=[], returncode=0, stdout="", stderr="",
                                 source="", hermes_home="", raw_session_path="", final_output="", error="[reused]")
        return _CaseResult(score=score, run_result=rr, trajectory=traj, session_analysis=sa)

    @staticmethod
    def _load_optional_json(path: Path, cls: Any) -> Optional[Any]:
        """Load a dataclass from a JSON file, or None if absent/corrupt."""
        if not path.exists():
            return None
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    @staticmethod
    def _load_optional_trajectory(path: Path) -> Optional[List[TrajectoryStep]]:
        if not path.exists():
            return None
        try:
            return [TrajectoryStep(**t) for t in json.loads(path.read_text(encoding="utf-8"))]
        except Exception:
            return None

    def _load_routed_case_result(self, case_id: str, case_dir: Path) -> _RoutedCaseResult:
        """Reconstruct a completed dual-model case's result from on-disk artifacts."""
        score = ScoreBreakdown(**json.loads((case_dir / "score.json").read_text(encoding="utf-8")))
        sa = SessionAnalysisResult(**json.loads((case_dir / "session_analysis.json").read_text(encoding="utf-8")))
        traj = [TrajectoryStep(**t) for t in json.loads((case_dir / "trajectory.json").read_text(encoding="utf-8"))]
        routing = json.loads((case_dir / "route.json").read_text(encoding="utf-8"))

        rr_path = case_dir / "run_result.json"
        if rr_path.exists():
            rr = HermesRunResult(**json.loads(rr_path.read_text(encoding="utf-8")))
        else:
            rr = HermesRunResult(case_id=case_id, command=[], returncode=0, stdout="", stderr="",
                                 source="", hermes_home="", raw_session_path="", final_output="", error="[reused]")

        large_dir = case_dir / "large"
        large_score = self._load_optional_json(large_dir / "score.json", ScoreBreakdown)
        large_trajectory = self._load_optional_trajectory(large_dir / "trajectory.json")

        return _RoutedCaseResult(
            case_id=case_id,
            route=routing.get("route", ROUTE_FAILURE),
            score=score,
            run_result=rr,
            trajectory=traj,
            session_analysis=sa,
            routing=routing,
            small_result_score=score,
            large_score=large_score,
            large_trajectory=large_trajectory,
        )

    @staticmethod
    def _instance_eval_fields(instance: Any) -> tuple:
        """Return (key_steps, sub_type, expected_outputs) for one data instance."""
        key_steps: List[Dict[str, Any]] = []
        sub_type = "default"
        expected_outputs = None
        if instance.metadata:
            key_steps = instance.metadata.get("key_steps", []) or []
            sub_type = instance.metadata.get("sub_type", "default")
            expected_outputs = instance.metadata.get("expected_outputs")
        return key_steps, sub_type, expected_outputs

    @staticmethod
    def _instance_to_knowledge_case(instance: Any) -> Dict[str, Any]:
        """Build the ``case`` dict consumed by ``extract_case_knowledge``."""
        metadata = instance.metadata or {}
        return {
            "case_id": str(instance.id),
            "utterance": instance.input,
            "key_steps": metadata.get("key_steps", []),
            "output": metadata.get("expected_outputs") or instance.expected_output,
            "pre_condition": instance.pre_condition,
        }

    def _extract_knowledge_lesson(
        self,
        *,
        routed: "_RoutedCaseResult",
        instance: Any,
        round_dir: Path,
        skill_text: str,
    ) -> Optional[Dict[str, Any]]:
        """Extract one KNOWLEDGE case's structured lesson (root_cause + suggestion).

        Reuses an on-disk ``knowledge.json`` when present so a resumed run does
        not re-invoke the extractor (double LLM cost + drift).
        """
        if instance is None:
            return None
        cid = routed.case_id
        knowledge_dir = round_dir / "cases" / cid / "knowledge"
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = knowledge_dir / "knowledge.json"

        if self.config.exec_mode == "direct":
            if not (routed.large_run_result and routed.large_session_analysis and routed.large_score):
                return None
            artifact = extract_direct_knowledge(case_id=cid,
                input_messages=instance.metadata["input_messages"], skill_text=skill_text,
                small_output=routed.run_result.final_output, large_output=routed.large_run_result.final_output,
                small_score=routed.score.result, large_score=routed.large_score.result,
                small_judge={k: d.get("judge_result", {}) for k, d in routed.session_analysis.output_analyses.items()},
                large_judge={k: d.get("judge_result", {}) for k, d in routed.large_session_analysis.output_analyses.items()},
                cache_context={"small": read_json(knowledge_dir.parent / "cache.json"),
                               "large": read_json(knowledge_dir.parent / "large" / "cache.json")},
                result_threshold=self.config.result_threshold, client=self.eval_model or self.gen_model,
                knowledge_dir=knowledge_dir)
            if not artifact.get("valid"):
                return None
            ca = artifact["case_analysis"]
            return {"root_cause": ca["root_cause"], "suggestions": [
                f"模式：{ca['pattern']}；改进方向：{ca['improvement_direction']}；适用边界：{ca['applicability_boundary']}"]}

        if artifact_path.exists():
            try:
                artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            except Exception:
                artifact = {}
        else:
            client = self.eval_model or self.gen_model
            if client is None:
                return None
            small_result = float(routed.small_result_score.result) if routed.small_result_score else 0.0
            large_result = float(routed.large_score.result) if routed.large_score else 0.0
            artifact = extract_case_knowledge(
                case=self._instance_to_knowledge_case(instance),
                small_trajectory=routed.trajectory,
                large_trajectory=routed.large_trajectory or [],
                small_result_score=small_result,
                large_result_score=large_result,
                result_threshold=self.config.result_threshold,
                client=client,
                current_skill_excerpt=skill_text[:4000],
                knowledge_dir=knowledge_dir,
            )

        if not artifact.get("valid"):
            return None
        case_analysis = artifact.get("case_analysis") or {}
        root_cause = str(case_analysis.get("root_cause") or "").strip()
        suggestion = str(case_analysis.get("suggestion") or "").strip()
        if not (root_cause and suggestion):
            return None
        return {"root_cause": root_cause, "suggestions": [suggestion]}

    def _merge_knowledge_leaves(
        self,
        leaves: List[Dict[str, Any]],
        round_idx: int,
        pool: ExperiencePool,
    ) -> None:
        """Hierarchically merge per-case knowledge lessons, then enter the unified pool.

        Knowledge is treated exactly like failure (``origin="knowledge"`` carries no
        priority). Lessons from different cases are consolidated via the same
        hierarchical map-reduce used for failure experiences, then merged into the
        failure pool with ``trajectory_count`` = number of knowledge cases hit.
        """
        client = self.eval_model or self.gen_model
        if client is not None and len(leaves) > 1:
            leaves = hierarchical_merge_experiences(
                leaves,
                to_experiences=lambda batch: batch,
                merge_experiences=lambda batch: merge_experience_batch(
                    client.generate, batch, kind_label="知识"
                ),
                batch_size=10,
            )
        for exp in leaves:
            pool.merge_failure(exp, round_idx, origin="knowledge")

    @staticmethod
    def _run_error(
        run_result: Optional[HermesRunResult],
        trajectory: Optional[List[TrajectoryStep]],
    ) -> str:
        """Classify an execution/collection failure (empty string = OK)."""
        if run_result is None:
            return "execution_failed"
        if run_result.returncode != 0:
            return run_result.error or f"hermes exited with code {run_result.returncode}"
        if not trajectory:
            return "trajectory_missing"
        return ""

    def _execute_case(
        self, *, runner, skill_path, instance, case_dir,
    ) -> tuple[HermesRunResult, List[TrajectoryStep]]:
        """Run Hermes once, parse + extract the trajectory (no analysis/scoring)."""
        run_result = runner.run_case(
            case_id=str(instance.id), skill_path=skill_path,
            input_text=instance.input, case_dir=case_dir,
            pre_condition=instance.pre_condition,
        )
        # 持久化 run_result，供断点续跑时复用（评估器实际不消费它，仅 web 展示用）
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "run_result.json").write_text(
            json.dumps(to_plain_dict(run_result), ensure_ascii=False, indent=2), encoding="utf-8")
        session = self.parser.parse(
            case_id=str(instance.id), run_result=run_result,
            export_path=Path(run_result.exported_session_path) if run_result.exported_session_path else None,
            output_path=case_dir / "session.normalized.json",
        )
        trajectory = self.extractor.extract(session, output_path=case_dir / "trajectory.json")
        return run_result, trajectory

    def _analyze_and_score(
        self, *, instance, trajectory, case_dir, scorer,
        judgment_weights, output_weights,
        analysis_path: Optional[Path] = None,
        score_path: Optional[Path] = None,
    ) -> tuple[SessionAnalysisResult, ScoreBreakdown]:
        """Analyze + score one trajectory with the given dimension gating."""
        key_steps, sub_type, expected_outputs = self._instance_eval_fields(instance)
        analysis_result = self.analyzer.analyze(
            case_id=str(instance.id), input_text=instance.input,
            trajectory=trajectory, key_steps=key_steps,
            expected_outputs=expected_outputs,
            judgment_weights=judgment_weights,
            output_weights=output_weights,
            output_path=analysis_path or (case_dir / "session_analysis.json"),
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
            output_path=score_path or (case_dir / "score.json"),
        )
        return analysis_result, score

    def _run_one_case(
        self, *, runner, skill_path, instance, case_dir, scorer,
    ) -> _CaseResult:
        run_result, trajectory = self._execute_case(
            runner=runner, skill_path=skill_path, instance=instance, case_dir=case_dir)
        analysis_result, score = self._analyze_and_score(
            instance=instance, trajectory=trajectory, case_dir=case_dir, scorer=scorer,
            judgment_weights=scorer.weights.judgment_weights,
            output_weights=scorer.output_weights,
        )
        return _CaseResult(score=score, run_result=run_result,
                          trajectory=trajectory, session_analysis=analysis_result)

    @staticmethod
    def _to_routed(case_result: _CaseResult) -> _RoutedCaseResult:
        """Wrap a legacy single-model result into the routed shape."""
        route = ROUTE_SUCCESS if not case_result.score.needs_optimization else ROUTE_FAILURE
        return _RoutedCaseResult(
            case_id=case_result.score.case_id,
            route=route,
            score=case_result.score,
            run_result=case_result.run_result,
            trajectory=case_result.trajectory,
            session_analysis=case_result.session_analysis,
            routing={
                "case_id": case_result.score.case_id,
                "route": route,
                "reason": "legacy_single_model",
            },
        )

    def _build_fallback_small_result(
        self, *, instance, case_dir, scorer, output_weights,
    ) -> tuple[SessionAnalysisResult, ScoreBreakdown]:
        """Produce a zeroed small-model analysis/score when execution/eval failed.

        Keeps ``session_analysis.json`` / ``score.json`` present so the on-disk
        shape of an INVALID case stays consistent with the previous flow.
        """
        key_steps, sub_type, _ = self._instance_eval_fields(instance)
        empty_analysis = SessionAnalysisResult(
            case_id=str(instance.id),
            key_steps=key_steps,
            process=0.0,
            conciseness=0.0,
            output_scores={},
            output_analyses={},
            trajectory_quality=0.0,
            overall_improvement_suggestions="",
        )
        (case_dir / "session_analysis.json").write_text(
            json.dumps(to_plain_dict(empty_analysis), ensure_ascii=False, indent=2), encoding="utf-8")
        score = scorer.score(
            case_id=str(instance.id), input_text=instance.input,
            sub_type=sub_type, key_steps=key_steps,
            process=0.0,
            conciseness=0.0,
            process_details={},
            conciseness_details={},
            output_scores={},
            output_analyses={},
            overall_improvement_suggestions="",
            output_weights=output_weights,
            output_path=case_dir / "score.json",
        )
        return empty_analysis, score

    def _run_direct_routed(self, *, small_runner, large_runner, skill_path, instance, case_dir,
                           scorer, result_threshold, allow_reuse=False):
        rr, tr, sa, score = evaluate_direct_case(runner=small_runner, analyzer=self.analyzer,
            scorer=scorer, skill_path=skill_path, instance=instance, case_dir=case_dir,
            result_threshold=result_threshold, allow_reuse=allow_reuse)
        large_rr = large_tr = large_sa = large_score = None
        attempted = False
        if not score.evaluation_error and score.result < result_threshold and large_runner:
            attempted = True
            large_rr, large_tr, large_sa, large_score = evaluate_direct_case(runner=large_runner,
                analyzer=self.analyzer, scorer=scorer, skill_path=skill_path, instance=instance,
                case_dir=case_dir / "large", result_threshold=result_threshold, allow_reuse=allow_reuse)
        routing = route_result_first(case_id=str(instance.id), small_result_score=score.result,
            small_process_score=None, small_conciseness_score=None,
            large_result_score=large_score.result if large_score else None,
            small_error=score.evaluation_error, large_error=large_score.evaluation_error if large_score else "",
            result_threshold=result_threshold, execution_mode="direct",
            large_model_enabled=large_runner is not None, large_model_run=attempted)
        if routing["route"] == ROUTE_INVALID:
            score.evaluation_error = score.evaluation_error or (large_score.evaluation_error if large_score else "invalid")
        write_json(case_dir / "route.json", routing)
        return _RoutedCaseResult(case_id=str(instance.id), route=routing["route"], score=score,
            run_result=rr, trajectory=tr, session_analysis=sa, routing=routing, small_result_score=score,
            large_score=large_score, large_run_result=large_rr, large_trajectory=large_tr,
            large_session_analysis=large_sa)

    def _run_one_case_result_first(
        self, *, small_runner, large_runner, skill_path, instance, case_dir,
        scorer, result_threshold, allow_reuse=False,
    ) -> _RoutedCaseResult:
        """Run the result-first decision tree for one case (dual-model mode).

        ① small model execute + full 3-dim eval (process/conciseness/result)
        ② large model is gated only by small result vs result_threshold
        ③ if small result failed: large model execute + result-only eval
        Produces route.json + small full score/session_analysis/trajectory.
        """
        if self.config.exec_mode == "direct":
            return self._run_direct_routed(small_runner=small_runner, large_runner=large_runner,
                skill_path=skill_path, instance=instance, case_dir=case_dir, scorer=scorer,
                result_threshold=result_threshold, allow_reuse=allow_reuse)
        cid = str(instance.id)
        case_dir.mkdir(parents=True, exist_ok=True)
        if allow_reuse and _routed_case_is_complete(case_dir):
            try:
                return self._load_routed_case_result(cid, case_dir)
            except Exception as exc:
                print(f"    [续跑] 复用用例 {cid} 失败({type(exc).__name__}: {exc})，改为重新执行")
        output_weights = scorer.output_weights
        full_jw = dict(scorer.weights.judgment_weights)
        result_only_jw = {"process": 0.0, "conciseness": 0.0, "result": 1.0}

        small_error = ""
        large_error = ""
        small_run_result: Optional[HermesRunResult] = None
        small_traj: List[TrajectoryStep] = []
        small_analysis: Optional[SessionAnalysisResult] = None
        small_score: Optional[ScoreBreakdown] = None
        large_run_result: Optional[HermesRunResult] = None
        large_traj: Optional[List[TrajectoryStep]] = None
        large_analysis: Optional[SessionAnalysisResult] = None
        large_score: Optional[ScoreBreakdown] = None

        # ① small model execute + full 3-dim eval
        try:
            small_run_result, small_traj = self._execute_case(
                runner=small_runner, skill_path=skill_path, instance=instance, case_dir=case_dir)
        except Exception as exc:
            small_error = str(exc)
        else:
            small_error = self._run_error(small_run_result, small_traj)
            if not small_error:
                small_analysis, small_score = self._analyze_and_score(
                    instance=instance, trajectory=small_traj, case_dir=case_dir, scorer=scorer,
                    judgment_weights=full_jw, output_weights=output_weights,
                    analysis_path=case_dir / "session_analysis.json",
                    score_path=case_dir / "score.json",
                )
                if small_analysis and small_analysis.evaluation_error:
                    small_error = f"evaluation_parse_failed: {small_analysis.evaluation_error}"

        small_result_value = float(small_score.result) if small_score is not None else None

        if not small_error and small_result_value is not None and small_result_value < result_threshold:
            # ③ small result fail -> large model execute + result-only eval
            large_dir = case_dir / "large"
            large_dir.mkdir(parents=True, exist_ok=True)
            try:
                large_run_result, large_traj = self._execute_case(
                    runner=large_runner, skill_path=skill_path, instance=instance, case_dir=large_dir)
            except Exception as exc:
                large_error = str(exc)
            else:
                large_error = self._run_error(large_run_result, large_traj)
                if not large_error:
                    large_analysis, large_score = self._analyze_and_score(
                        instance=instance, trajectory=large_traj, case_dir=large_dir, scorer=scorer,
                        judgment_weights=result_only_jw, output_weights=output_weights)
                    if large_analysis and large_analysis.evaluation_error:
                        large_error = f"evaluation_parse_failed: {large_analysis.evaluation_error}"

        routing = route_result_first(
            case_id=cid,
            small_result_score=small_result_value,
            small_process_score=(float(small_score.process) if small_score is not None else None),
            small_conciseness_score=(float(small_score.conciseness) if small_score is not None else None),
            large_result_score=(float(large_score.result) if large_score is not None else None),
            small_error=small_error,
            large_error=large_error,
            result_threshold=result_threshold,
        )
        (case_dir / "route.json").write_text(
            json.dumps(routing, ensure_ascii=False, indent=2), encoding="utf-8")

        if small_run_result is None:
            small_run_result = HermesRunResult(
                case_id=cid, command=[], returncode=1, stdout="", stderr="",
                source="", hermes_home="", raw_session_path="", final_output="",
                error=small_error,
            )

        if small_analysis is None or small_score is None:
            small_analysis, small_score = self._build_fallback_small_result(
                instance=instance, case_dir=case_dir, scorer=scorer,
                output_weights=output_weights,
            )

        return _RoutedCaseResult(
            case_id=cid,
            route=routing["route"],
            score=small_score,
            run_result=small_run_result,
            trajectory=small_traj,
            session_analysis=small_analysis,
            routing=routing,
            small_result_score=small_score,
            large_score=large_score,
            large_run_result=large_run_result,
            large_trajectory=large_traj,
            large_session_analysis=large_analysis,
        )

    # ------------------------------------------------------------------
    # GEPA internal evaluator
    # ------------------------------------------------------------------

    def _score_from_executor_feedback(
        self, output: Any, expected: str, input_text: str = "", sub_type: str = ""
    ):
        from ..scoring.base import SideInfo
        if isinstance(output, dict) and isinstance(output.get("score_breakdown"), ScoreBreakdown):
            score = output["score_breakdown"]
            return score.total_score / 100.0, SideInfo(
                score.total_score / 100.0, score.feedback,
                {"self_evolve_score": to_plain_dict(score)},
            )
        return 0.0, SideInfo(0.0, "no score")


# ---------------------------------------------------------------------------
# HermesSkillExecutor (adapter for GEPA internal evaluation)
# ---------------------------------------------------------------------------

class HermesSkillExecutor:
    """Callable adapter so GEPAOptimizer can re-execute skill candidates."""

    def __init__(self, *, runner, round_dir, skill_resource_root,
                 parser, extractor, analyzer, scorer):
        self.runner = runner
        self.round_dir = round_dir
        self.skill_resource_root = skill_resource_root
        self.parser = parser
        self.extractor = extractor
        self.analyzer = analyzer
        self.scorer = scorer
        self.counter = 0

    def __call__(self, prompt: str, instance: DataInstance) -> Dict[str, Any]:
        self.counter += 1
        eval_dir = self.round_dir / "candidate_eval" / f"case_{instance.id}_{self.counter}"
        skill_path = eval_dir / "skill" / "SKILL.md"
        _prepare_skill_package(
            skill_path=skill_path, skill_content=prompt,
            resource_root=self.skill_resource_root,
        )
        if isinstance(self.runner, DirectRunner):
            rr, tr, sa, score = evaluate_direct_case(runner=self.runner, analyzer=self.analyzer,
                scorer=self.scorer, skill_path=skill_path, instance=instance, case_dir=eval_dir)
            return {"output": rr.final_output, "score_breakdown": score, "trajectory": tr,
                    "distilled": sa, "run_result": rr}
        run_result = self.runner.run_case(
            case_id=str(instance.id), skill_path=skill_path,
            input_text=instance.input, case_dir=eval_dir,
            pre_condition=instance.pre_condition,
        )
        session = self.parser.parse(
            case_id=str(instance.id), run_result=run_result,
            export_path=Path(run_result.exported_session_path) if run_result.exported_session_path else None,
            output_path=eval_dir / "session.normalized.json",
        )
        trajectory = self.extractor.extract(session, output_path=eval_dir / "trajectory.json")

        key_steps = []
        sub_type = "default"
        expected_outputs = None
        if instance.metadata:
            key_steps = instance.metadata.get("key_steps", []) or []
            sub_type = instance.metadata.get("sub_type", "default")
            expected_outputs = instance.metadata.get("expected_outputs")

        judgment_weights = self.scorer.weights.judgment_weights
        output_weights = self.scorer.output_weights

        analysis = self.analyzer.analyze(
            case_id=str(instance.id), input_text=instance.input,
            trajectory=trajectory, key_steps=key_steps,
            expected_outputs=expected_outputs,
            judgment_weights=judgment_weights,
            output_weights=output_weights,
            output_path=eval_dir / "session_analysis.json",
        )
        score = self.scorer.score(
            case_id=str(instance.id), input_text=instance.input,
            sub_type=sub_type, key_steps=key_steps,
            process=analysis.process,
            conciseness=analysis.conciseness,
            process_details=analysis.process_details,
            conciseness_details=analysis.conciseness_details,
            output_scores=analysis.output_scores,
            output_analyses=analysis.output_analyses,
            overall_improvement_suggestions=analysis.overall_improvement_suggestions,
            output_weights=output_weights,
            output_path=eval_dir / "score.json",
        )
        return {
            "output": session.final_output,
            "score_breakdown": score,
            "session": session,
            "trajectory": trajectory,
            "distilled": analysis,
            "run_result": run_result,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _select_strategy(score_history: List[float], config: SelfEvolveConfig) -> str:
    """Determine whether to use structured or exploratory mutation."""
    if len(score_history) < config.stall_rounds + 1:
        return "structured"

    recent = score_history[-config.stall_rounds:]
    baseline = score_history[-config.stall_rounds - 1]
    improvements = [r - baseline for r in recent]
    stalled = all(imp < config.stall_threshold for imp in improvements)

    if stalled:
        print(f"  停滞检测: 连续{config.stall_rounds}轮提升 < {config.stall_threshold}")
        return "exploratory"
    return "structured"


def _round_index(name: str) -> Optional[int]:
    """Parse ``round_N`` directory name to its integer index, or None."""
    if not name.startswith("round_"):
        return None
    try:
        return int(name[len("round_"):])
    except ValueError:
        return None


def _case_is_complete(case_dir: Path) -> bool:
    """A case is reusable iff all downstream artifacts are present."""
    return all(
        (case_dir / name).exists()
        for name in ("score.json", "session_analysis.json", "trajectory.json")
    )


def _routed_case_is_complete(case_dir: Path) -> bool:
    """A dual-model case is reusable iff route + downstream artifacts are present."""
    return _case_is_complete(case_dir) and (case_dir / "route.json").exists()


def _reconstruct_checkpoint_from_files(
    out_root: Path, skill_file: Path, total_rounds: int,
) -> Dict[str, Any]:
    """Reconstruct a checkpoint-shaped dict by scanning round directories.

    Fallback for ``--resume`` when ``checkpoint.json`` is absent (e.g. the user
    manually deleted files to roll back to an earlier round). A round directory
    is treated as *complete* when its ``round_summary.json`` exists; the first
    round directory lacking that marker is the interrupted round to resume from.

    To mark round ``N`` as the resume point, delete ``round_N/round_summary.json``
    (and any later rounds you don't want kept). This reconstructs the in-memory
    state (final skill, score history, best, pool, lineage) from rounds ``1..N-1``
    so the loop can re-run round ``N`` with case reuse.
    """
    rounds: List[tuple] = []
    for d in out_root.glob("round_*"):
        idx = _round_index(d.name)
        if idx is not None and d.is_dir():
            rounds.append((idx, d))
    rounds.sort(key=lambda t: t[0])
    round_by_idx = {idx: d for idx, d in rounds}

    complete = [idx for idx, d in rounds if (d / "round_summary.json").exists()]
    incomplete = [idx for idx, d in rounds if idx not in complete]

    if incomplete:
        start_round = min(incomplete)
    elif complete:
        start_round = max(complete) + 1
    else:
        start_round = 1

    if start_round > total_rounds:
        raise ValueError(f"该运行已完整结束，无需续跑: {out_root}")

    print(f"  [续跑] 检测到完整轮次={complete}，中断轮次={incomplete}，将从第 {start_round} 轮续跑")

    # 当前 skill = 进入 start_round 时的 skill（已包含 1..start_round-1 全部变异）
    skill_dir = out_root / f"round_{start_round}" / "skill"
    if (skill_dir / "SKILL.md").exists():
        current_skill = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    elif (out_root / "initial_skill.md").exists():
        current_skill = (out_root / "initial_skill.md").read_text(encoding="utf-8")
    else:
        current_skill = skill_file.read_text(encoding="utf-8")

    score_history: List[float] = []
    best_score = 0.0
    best_skill = current_skill
    lineage: List[Dict[str, Any]] = []

    for idx in sorted(complete):
        if idx >= start_round:
            continue
        d = round_by_idx[idx]
        summary: Dict[str, Any] = {}
        summary_path = d / "round_summary.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception:
                summary = {}
        avg = float(summary.get("average_score", 0.0))
        passed = bool(summary.get("passed", False))
        score_history.append(avg)

        skill_path = d / "skill" / "SKILL.md"
        if avg > best_score and skill_path.exists():
            best_score = avg
            best_skill = skill_path.read_text(encoding="utf-8")

        lineage.append({
            "round": idx,
            "skill_path": str(d / "skill" / "SKILL.md"),
            "average_score": avg,
            "passed": passed,
            "pool_summary": "",
        })

    # 经验池：取 start_round 之前最后一个已完成轮次的快照（含 1..start_round-1
    # 的累积经验），避免带入中断轮次已合并的经验造成重复。
    pool_data: Dict[str, Any] = {}
    prior_complete = [idx for idx in complete if idx < start_round]
    if prior_complete:
        last_complete = max(prior_complete)
        pool_file = out_root / f"round_{last_complete}" / "experience_pool.json"
        if not pool_file.exists():
            pool_file = out_root / "experience_pool.json"
        if pool_file.exists():
            try:
                pool_data = json.loads(pool_file.read_text(encoding="utf-8"))
            except Exception:
                pool_data = {}

    run_id = out_root.name
    meta_path = out_root / "run_meta.json"
    if meta_path.exists():
        try:
            run_id = json.loads(meta_path.read_text(encoding="utf-8")).get("run_id") or run_id
        except Exception:
            pass

    return {
        "version": 1,
        "run_id": run_id,
        "done": False,
        "completed_round": start_round - 1,
        "total_rounds": total_rounds,
        "final_skill": current_skill,
        "best_skill": best_skill,
        "best_score": best_score,
        "score_history": score_history,
        "lineage": lineage,
        "pool": pool_data,
    }


def _trend_str(score_history: List[float]) -> str:
    if len(score_history) < 2:
        return ""
    delta = score_history[-1] - score_history[-2]
    if delta > 0.5:
        return f" ↑{delta:.1f}"
    elif delta < -0.5:
        return f" ↓{abs(delta):.1f}"
    return " →"


def _save_round_summary(
    round_dir: Path, round_index: int, skill_path: Path,
    case_scores: List[ScoreBreakdown], avg_score: float, passed: bool,
) -> None:
    targets = [s.feedback for s in case_scores if not s.evaluation_error and s.needs_optimization and s.feedback]
    summary = {
        "round_index": round_index,
        "skill_path": str(skill_path),
        "average_score": round(avg_score, 4),
        "passed": passed,
        "execution_mode": case_scores[0].execution_mode if case_scores else "hermes",
        "evaluated_dimensions": case_scores[0].evaluated_dimensions if case_scores else [],
        "evaluation_set_id": case_scores[0].details.get("evaluation_set_id") if case_scores else None,
        "valid_case_ids": [s.case_id for s in case_scores if not s.evaluation_error],
        "optimization_targets": targets[:10],
    }
    (round_dir / "round_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


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


SkillExecutor = HermesSkillExecutor
