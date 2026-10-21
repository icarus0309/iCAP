"""GEPA optimizer engine – the main optimizer class.

Keeps the core GEPA algorithm intact (population, Pareto, accept/reject)
while delegating to sibling modules for selection, reflection, and mutation.
"""

import random
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .candidate import DataInstance, EvaluationResult, PromptCandidate
from .selection import find_pareto_frontier, select_parent, select_final_candidate
from .reflection import collect_feedback_for_reflection, collect_experience_summary
from .prompt_builder import mutate_prompt_with_reflection
from .trace_store import save_skill_immediately


class GEPAOptimizer:
    """Genetic-Pareto prompt optimizer – skill optimization variant."""

    def __init__(
        self,
        model_client: Any,
        config: Any,
        evaluator: Callable[..., Any],
        is_skill_mode: bool = False,
        gen_model_client: Optional[Any] = None,
        output_dir: Optional[str] = None,
        skill_executor: Optional[Callable[[str, DataInstance], Any]] = None,
        progress_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.model_client = model_client
        self.gen_model_client = gen_model_client or model_client
        self.config = config
        self.evaluator = evaluator
        self.is_skill_mode = is_skill_mode
        self.output_dir = output_dir
        self.skill_executor = skill_executor
        self.progress_callback = progress_callback
        self.candidates: List[PromptCandidate] = []
        self.next_id = 0
        self.total_metric_calls = 0
        self.start_time = time.time()
        self.iteration_history: List[Dict[str, Any]] = []

        if config.random_seed is not None:
            random.seed(config.random_seed)

    # ------------------------------------------------------------------
    # Data splitting
    # ------------------------------------------------------------------

    def split_data(
        self, data: List[DataInstance]
    ) -> Tuple[List[DataInstance], List[DataInstance]]:
        """Stratified split into Pareto (validation) and feedback sets."""
        if len(data) <= self.config.pareto_size:
            return data, []

        sub_type_groups: Dict[str, List[DataInstance]] = {}
        for instance in data:
            sub_type = "default"
            if instance.metadata and "sub_type" in instance.metadata:
                sub_type = instance.metadata["sub_type"]
            sub_type_groups.setdefault(sub_type, []).append(instance)

        num_types = len(sub_type_groups)
        pareto_per_type = max(1, self.config.pareto_size // num_types)

        pareto_set: List[DataInstance] = []
        feedback_set: List[DataInstance] = []

        for sub_type, group in sub_type_groups.items():
            shuffled = group.copy()
            random.shuffle(shuffled)
            n_pareto = min(pareto_per_type, len(shuffled))
            pareto_set.extend(shuffled[:n_pareto])
            feedback_set.extend(shuffled[n_pareto:])

        if len(pareto_set) < self.config.pareto_size and feedback_set:
            random.shuffle(feedback_set)
            need = self.config.pareto_size - len(pareto_set)
            pareto_set.extend(feedback_set[:need])
            feedback_set = feedback_set[need:]

        print(f"分层抽样: {num_types} 个 sub_type, pareto={len(pareto_set)}, feedback={len(feedback_set)}")
        return pareto_set, feedback_set

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------

    def sample_minibatch(self, feedback_set: List[DataInstance]) -> List[DataInstance]:
        """Sample a minibatch from the feedback set."""
        if len(feedback_set) <= self.config.minibatch_size:
            return feedback_set
        return random.sample(feedback_set, self.config.minibatch_size)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute_prompt(self, prompt: str, data_instance: DataInstance) -> Any:
        """Execute a prompt against a data instance."""
        if self.skill_executor is not None:
            return self.skill_executor(prompt, data_instance)

        if self.is_skill_mode:
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": data_instance.input},
            ]
        elif "{input}" in prompt:
            formatted = prompt.format(input=data_instance.input)
            messages = [{"role": "user", "content": formatted}]
        else:
            formatted = f"{prompt}\n\n用户问题：{data_instance.input}"
            messages = [{"role": "user", "content": formatted}]

        return self.model_client.generate(messages)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate_candidate(
        self, candidate: PromptCandidate, instances: List[DataInstance]
    ) -> None:
        """Evaluate a candidate on all instances, computing sub_type scores."""
        for i, instance in enumerate(instances):
            output = self.execute_prompt(candidate.prompt, instance)
            sub_type = "default"
            if instance.metadata and "sub_type" in instance.metadata:
                sub_type = instance.metadata["sub_type"]
            score, side_info = self.evaluator(
                output,
                instance.expected_output,
                input_text=instance.input,
                sub_type=sub_type,
            )

            # Extract dimension scores from diagnostics
            dims = {}
            if hasattr(side_info, "diagnostics") and side_info.diagnostics:
                se_score = side_info.diagnostics.get("self_evolve_score", {})
                dims["process"] = se_score.get("process", 0)
                dims["conciseness"] = se_score.get("conciseness", 0)
                dims["result"] = se_score.get("result", 0)
            process = dims.get("process", 0)
            conciseness = dims.get("conciseness", 0)
            result = dims.get("result", 0)
            print(f"  [{i+1}/{len(instances)}] 执行ID:{instance.id} sub_type:{sub_type} | 得分={score*100:.1f} 过程={process:.1f} 精简={conciseness:.1f} 结果={result:.1f}")
            self.total_metric_calls += 1

            candidate.eval_results[instance.id] = EvaluationResult(
                instance_id=instance.id,
                score=score,
                side_info=side_info,
                output=output,
            )

            # Emit progress for each case evaluation
            if self.progress_callback:
                try:
                    self.progress_callback("gepa_case_done", {
                        "case_id": instance.id,
                        "index": i + 1,
                        "total": len(instances),
                        "score": round(score * 100, 1),
                        "process": round(process, 1),
                        "conciseness": round(conciseness, 1),
                        "result": round(result, 1),
                    })
                except Exception:
                    pass

        # Compute sub_type averaged scores
        sub_type_groups: Dict[str, List[float]] = {}
        for instance in instances:
            sub_type = "default"
            if instance.metadata and "sub_type" in instance.metadata:
                sub_type = instance.metadata["sub_type"]
            if instance.id in candidate.eval_results:
                sub_type_groups.setdefault(sub_type, []).append(
                    candidate.eval_results[instance.id].score
                )

        candidate.sub_type_scores = {
            st: (sum(scores) / len(scores)) * 100
            for st, scores in sub_type_groups.items()
        }
        candidate.update_aggregate_score()

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def try_merge_candidates(
        self,
        candidate_a: PromptCandidate,
        candidate_b: PromptCandidate,
        pareto_set: List[DataInstance],
    ) -> Optional[PromptCandidate]:
        """Attempt to merge two candidates into a better one."""
        if candidate_a.id == candidate_b.id:
            return None

        system_prompt = """你是一个SKILL合并专家。你的任务是合并两个SKILL文档的优点，优先修改和整合现有内容。

合并原则：
1. 必须完整保留文档开头的YAML frontmatter（---之间的元数据），使用得分更高版本的元数据
2. 优化思路：优先修改和整合现有内容，而不是添加新示例
3. 合并策略：
   - 分析两个文档各自的优点和特点
   - 整合相似内容，去除重复
   - 调整结构让逻辑更顺畅
   - 合并相似的规则或指令
   - 补充缺失的说明（优先补充说明而非示例）
4. 合并后应保持结构清晰、逻辑连贯
5. 直接输出合并后的完整SKILL文档（必须以---开头）"""

        user_prompt = f"""## SKILL文档A（得分: {candidate_a.aggregate_score:.3f}）
{candidate_a.prompt}

## SKILL文档B（得分: {candidate_b.aggregate_score:.3f}）
{candidate_b.prompt}

## 分析任务
1. 先分析A和B各自的优点和不足
2. 思考如何通过修改和整合现有内容来提升效果
3. 优先合并相似内容、调整结构、补充说明
4. 尽量避免直接添加新示例
5. 输出合并后的完整SKILL文档："""

        merged_prompt = self.gen_model_client.generate_with_system(system_prompt, user_prompt)

        from .prompt_builder import _extract_skill_content
        merged_prompt = _extract_skill_content(merged_prompt)

        if len(merged_prompt) < 100:
            return None

        merged_candidate = PromptCandidate(
            prompt=merged_prompt,
            generation=max(candidate_a.generation, candidate_b.generation) + 1,
            parent_id=candidate_a.id,
            id=self.next_id,
        )
        self.next_id += 1
        self.evaluate_candidate(merged_candidate, pareto_set)

        if merged_candidate.aggregate_score > max(
            candidate_a.aggregate_score, candidate_b.aggregate_score
        ):
            return merged_candidate
        return None

    # ------------------------------------------------------------------
    # Stopping
    # ------------------------------------------------------------------

    def should_stop(self) -> bool:
        """Check if optimization should stop (metric calls or timeout)."""
        if hasattr(self.config, "max_metric_calls") and self.config.max_metric_calls:
            if self.total_metric_calls >= self.config.max_metric_calls:
                return True
        if hasattr(self.config, "timeout_seconds") and self.config.timeout_seconds:
            if time.time() - self.start_time >= self.config.timeout_seconds:
                return True
        return False

    # ------------------------------------------------------------------
    # Main optimization loop
    # ------------------------------------------------------------------

    def optimize(
        self, initial_prompt: str, data: List[DataInstance],
        root_scores: Optional[Dict[str, float]] = None,
        root_side_infos: Optional[Dict[str, Any]] = None,
        target_score: Optional[float] = None,
    ) -> Optional[PromptCandidate]:
        """Run the GEPA optimization loop.

        Args:
            initial_prompt: The skill text to start from.
            data: All dataset instances.
            root_scores: Pre-computed scores for the root candidate, keyed by
                instance_id. Score is on 0-100 scale. If provided, the root
                candidate is NOT re-executed — scores are injected directly.
            target_score: If any candidate exceeds this score (0-100), stop
                early. Used for early exit when a better version is found.
        """
        print(f"开始GEPA优化，迭代次数: {self.config.iterations}")
        print(f"数据大小: {len(data)}, 小批量大小: {self.config.minibatch_size}")
        print(f"Skill模式: {'是' if self.is_skill_mode else '否'}")
        if root_scores:
            print(f"根候选已有预计算分数，跳过重复评估 ({len(root_scores)} 条)")

        evaluation_set = data

        # Root candidate
        root = PromptCandidate(prompt=initial_prompt, generation=0, id=self.next_id)
        self.next_id += 1

        if root_scores:
            # Inject pre-computed scores — skip re-execution
            for instance in evaluation_set:
                score_100 = root_scores.get(instance.id, 0.0)
                side_info = (root_side_infos or {}).get(instance.id)
                root.eval_results[instance.id] = EvaluationResult(
                    instance_id=instance.id,
                    score=score_100 / 100.0,
                    side_info=side_info,
                    output="(reused from round evaluation)",
                )
            root.update_aggregate_score()
            print(f"根候选聚合得分(复用): {root.aggregate_score:.3f}")
        else:
            self.evaluate_candidate(root, evaluation_set)
            print(f"根候选聚合得分: {root.aggregate_score:.3f}")

        self.iteration_history.append({
            "iteration": 0, "type": "root", "candidate_id": root.id,
            "score": root.aggregate_score, "sub_type_scores": dict(root.sub_type_scores),
            "prompt_length": len(root.prompt), "accepted": True,
        })

        best_score = root.aggregate_score
        no_improvement_count = 0
        self.candidates: List[PromptCandidate] = [root]  # 接受种群以根为种子（修复：此前为空，导致首轮无 parent 被跳过）
        self.all_candidates: List[PromptCandidate] = [root]  # all candidates incl. rejected

        for iteration in range(self.config.iterations):
            if self.should_stop():
                print(f"达到停止条件，提前结束于迭代 {iteration}")
                break

            print(f"\n--- 迭代 {iteration + 1}/{self.config.iterations} (总调用: {self.total_metric_calls}) ---")

            # Emit iteration start
            if self.progress_callback:
                try:
                    self.progress_callback("stage", {
                        "phase": "mutating",
                        "message": f"探索性重写 · GEPA 迭代 {iteration + 1}/{self.config.iterations}",
                    })
                    self.progress_callback("gepa_iter_start", {
                        "iteration": iteration + 1,
                        "total_iterations": self.config.iterations,
                        "candidates": len(self.candidates),
                        "total_metric_calls": self.total_metric_calls,
                    })
                except Exception:
                    pass

            frontier = find_pareto_frontier(self.candidates)
            print(f"帕累托前沿大小: {len(frontier)}")

            parent = select_parent(frontier)
            if not parent:
                print("没有可用的父候选，跳过")
                continue

            minibatch = self.sample_minibatch(evaluation_set)
            if not minibatch:
                print("没有可用的反馈数据，跳过")
                continue

            # Generate feedback
            minibatch_feedback = collect_feedback_for_reflection(parent, minibatch)
            experience_summary = collect_experience_summary(
                self.gen_model_client, parent, evaluation_set
            )
            feedback_text = minibatch_feedback
            if experience_summary:
                feedback_text += "\n\n" + experience_summary

            # Mutate
            child_prompt = mutate_prompt_with_reflection(
                self.gen_model_client, parent.prompt, feedback_text
            )

            if child_prompt == parent.prompt:
                print(f"[SKIP] 变异返回原文档，跳过此次迭代")
                continue

            child = PromptCandidate(
                prompt=child_prompt,
                generation=iteration + 1,
                parent_id=parent.id,
                id=self.next_id,
            )
            self.next_id += 1
            self.evaluate_candidate(child, evaluation_set)

            if child.aggregate_score > parent.aggregate_score + 1e-6:
                self.candidates.append(child)
                print(f"[OK] 接受子候选: {child.aggregate_score:.3f} > {parent.aggregate_score:.3f}")
                save_skill_immediately(child, iteration + 1, True, self.output_dir, parent.id)

                self.iteration_history.append({
                    "iteration": iteration + 1, "type": "mutate",
                    "candidate_id": child.id, "parent_id": parent.id,
                    "score": child.aggregate_score,
                    "sub_type_scores": dict(child.sub_type_scores),
                    "parent_score": parent.aggregate_score,
                    "prompt_length": len(child.prompt), "accepted": True,
                })

                if child.aggregate_score > best_score + 1e-6:
                    best_score = child.aggregate_score
                    no_improvement_count = 0
                else:
                    no_improvement_count += 1
            else:
                print(f"[X] 拒绝子候选: {child.aggregate_score:.3f} <= {parent.aggregate_score:.3f}")
                no_improvement_count += 1
                save_skill_immediately(child, iteration + 1, False, self.output_dir, parent.id)

                self.iteration_history.append({
                    "iteration": iteration + 1, "type": "mutate",
                    "candidate_id": child.id, "parent_id": parent.id,
                    "score": child.aggregate_score,
                    "sub_type_scores": dict(child.sub_type_scores),
                    "parent_score": parent.aggregate_score,
                    "prompt_length": len(child.prompt), "accepted": False,
                })

            # Always track this candidate (even if rejected)
            self.all_candidates.append(child)

            # Early stop if any candidate beats the target score
            if target_score is not None and child.aggregate_score > target_score:
                print(f"  ⚡ 子候选得分 {child.aggregate_score:.1f} > 目标 {target_score:.1f}，提前停止 GEPA")
                break

            # Merge on every 3rd iteration
            if getattr(self.config, "enable_merge", True) and iteration % 3 == 0 and len(self.candidates) >= 2:
                to_merge = random.sample(self.candidates, min(2, len(self.candidates)))
                merged = self.try_merge_candidates(to_merge[0], to_merge[1], evaluation_set)
                if merged and merged.aggregate_score > max(
                    to_merge[0].aggregate_score, to_merge[1].aggregate_score
                ):
                    self.candidates.append(merged)
                    self.all_candidates.append(merged)
                    print(f"[OK] 合并成功: {merged.aggregate_score:.3f}")
                    save_skill_immediately(merged, iteration + 1, True, self.output_dir)
                    self.iteration_history.append({
                        "iteration": iteration + 1, "type": "merge",
                        "candidate_id": merged.id,
                        "parent_ids": [to_merge[0].id, to_merge[1].id],
                        "score": merged.aggregate_score,
                        "sub_type_scores": dict(merged.sub_type_scores),
                        "parent_scores": [to_merge[0].aggregate_score, to_merge[1].aggregate_score],
                        "prompt_length": len(merged.prompt), "accepted": True,
                    })

            # Emit iteration done
            if self.progress_callback:
                try:
                    self.progress_callback("gepa_iter_done", {
                        "iteration": iteration + 1,
                        "total_iterations": self.config.iterations,
                        "candidates": len(self.candidates),
                        "best_score": round(best_score, 1),
                        "total_metric_calls": self.total_metric_calls,
                        "accepted": child.aggregate_score > parent.aggregate_score + 1e-6,
                    })
                except Exception:
                    pass

            if hasattr(self.config, "no_improvement_patience") and self.config.no_improvement_patience:
                if no_improvement_count >= self.config.no_improvement_patience:
                    print(f"连续{no_improvement_count}次无改进，提前结束")
                    break

        final_candidate = select_final_candidate(self.all_candidates)

        print(f"\n{'='*50}")
        print(f"优化完成!")
        print(f"总候选数（接受）: {len(self.candidates)}")
        print(f"总尝试数: {len(self.iteration_history)}")
        print(f"总Metric调用: {self.total_metric_calls}")
        print(f"耗时: {time.time() - self.start_time:.1f}秒")
        if final_candidate:
            print(f"最佳候选得分: {final_candidate.aggregate_score:.3f}")
            print(f"最佳候选代数: {final_candidate.generation}")
        print(f"{'='*50}")

        return final_candidate
