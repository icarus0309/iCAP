"""Bounded sequential exploration. No population, parents, branches or Pareto selection."""
import json
from pathlib import Path
from .result import MutationResult
from ..evaluation_policy import case_mean, fingerprint, read_json, write_json, model_identity
from ...model_client import QuotaExceededError

REWRITE_SYSTEM = """你是单次 API 调用场景的 SKILL 优化专家。根据输入、最终输出的 judge 反馈和经验约束，
探索性重写完整 SKILL。提炼可迁移的输出判断规则、触发条件和适用边界。
不得推测内部推理，不补写工具步骤或过程精简要求；不得复制个案答案或私有事实。
保留原有 YAML 元数据及成功约束。只输出完整 SKILL 文本，不解释，不输出 Markdown 代码围栏。"""


class DirectExploratoryMutator:
    def __init__(self, *, model_client, evaluate, output_dir, iterations=3,
                 max_metric_calls=None, progress_callback=None, stop_checker=None):
        if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations <= 0:
            raise ValueError("direct GEPA_ITERATIONS must be a positive integer")
        self.model = model_client
        self.evaluate = evaluate
        self.output_dir = Path(output_dir)
        self.iterations = min(iterations, 3)
        self.max_metric_calls = max_metric_calls
        self.emit = progress_callback or (lambda *args: None)
        self.stop_checker = stop_checker

    def mutate(self, *, skill, eval_result, data, state, baseline_scores, cache_context=""):
        valid_ids = sorted(k for k, score in baseline_scores.items() if not score.evaluation_error)
        baseline = case_mean([baseline_scores[k] for k in valid_ids])
        if baseline is None:
            return MutationResult(next_skill=skill, changed=False, raw={"stop_reason": "no_valid_baseline"})
        identity = fingerprint({"version": 1, "skill": skill, "scores": {k: baseline_scores[k].result for k in valid_ids},
            "set": [str(i.id) for i in data], "context": cache_context, "model": model_identity(self.model),
            "iterations": self.iterations, "max_metric_calls": self.max_metric_calls})
        path = self.output_dir / "checkpoint.json"
        ckpt = read_json(path)
        if ckpt.get("identity") != identity:
            ckpt = {"identity": identity, "baseline_skill": skill, "baseline_score": baseline,
                    "evaluation_set_id": fingerprint([str(i.id) for i in data]), "valid_case_ids": valid_ids,
                    "attempts": [], "last_skill": skill, "accepted": False, "done": False,
                    "metric_calls": 0, "feedback": {k: baseline_scores[k].feedback for k in valid_ids}}
        while not ckpt["done"] and len(ckpt["attempts"]) < self.iterations:
            if self.stop_checker and self.stop_checker():
                ckpt["stop_reason"] = "interrupted"
                write_json(path, ckpt)
                break
            if self.max_metric_calls is not None and ckpt["metric_calls"] + len(data) > self.max_metric_calls:
                ckpt.update(done=True, stop_reason="metric_budget_exhausted")
                break
            attempt = len(ckpt["attempts"]) + 1
            entry = {"iteration": attempt, "accepted": False, "status": "started"}
            ckpt["attempts"].append(entry)
            # Reserve the attempt before any model call; resume cannot reset the budget.
            write_json(path, ckpt)
            self.emit("gepa_iter_start", {"execution_mode": "direct", "iteration": attempt,
                "total_iterations": self.iterations, "baseline_score": baseline})
            try:
                generated = self.model.generate([{"role": "system", "content": REWRITE_SYSTEM},
                    {"role": "user", "content": json.dumps({"skill": ckpt["last_skill"],
                        "judge_feedback": ckpt["feedback"], "experience": eval_result.global_experience,
                        "success_constraints": state.get("success_constraints", []),
                        "rejected_experiences": state.get("rejected_experiences", [])}, ensure_ascii=False)}])
                text = generated.strip() if isinstance(generated, str) else ""
                if text.startswith("```") and text.endswith("```"):
                    text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                if not text or text == ckpt["last_skill"]:
                    entry["status"] = "empty_or_unchanged"
                else:
                    ckpt["last_skill"] = text
                    entry["skill"] = text
                    attempt_dir = self.output_dir / f"attempt_{attempt}"
                    attempt_dir.mkdir(parents=True, exist_ok=True)
                    (attempt_dir / "SKILL.md").write_text(text, encoding="utf-8")
                    ckpt["metric_calls"] += len(data)
                    write_json(path, ckpt)
                    scores = self.evaluate(text, attempt)
                    ids = sorted(k for k, score in scores.items() if not score.evaluation_error)
                    comparable = ids == valid_ids and set(scores) == {str(i.id) for i in data}
                    score = case_mean([scores[k] for k in ids]) if comparable else None
                    entry.update(score=score, status="evaluated" if comparable else "invalid_evaluation_set",
                        case_scores={k: {"result": s.result, "error": s.evaluation_error, "feedback": s.feedback}
                                     for k, s in scores.items()})
                    ckpt["feedback"] = {k: s.feedback for k, s in scores.items() if not s.evaluation_error}
                    if score is not None and score > baseline + 1e-6:
                        entry["accepted"] = True
                        ckpt.update(accepted=True, done=True, accepted_skill=text, candidate_score=score,
                                    stop_reason="improved")
            except QuotaExceededError:
                raise
            except Exception as exc:
                entry.update(status="error", error=str(exc))
            if len(ckpt["attempts"]) >= self.iterations and not ckpt["done"]:
                ckpt.update(done=True, stop_reason="attempt_limit")
            write_json(self.output_dir / f"attempt_{attempt}" / "result.json", entry)
            write_json(path, ckpt)
            self.emit("gepa_iter_done", {"execution_mode": "direct", "iteration": attempt,
                "total_iterations": self.iterations, "baseline_score": baseline,
                "candidate_score": entry.get("score"), "accepted": entry["accepted"],
                "stop_reason": ckpt.get("stop_reason", "continue")})
        if not ckpt["done"] and len(ckpt["attempts"]) >= self.iterations:
            ckpt.update(done=True, stop_reason="attempt_limit")
        write_json(path, ckpt)
        return MutationResult(next_skill=ckpt.get("accepted_skill", skill), changed=ckpt["accepted"],
            raw={"candidate_score": ckpt.get("candidate_score"), "total_metric_calls": ckpt["metric_calls"],
                 "attempt_count": len(ckpt["attempts"]), "stop_reason": ckpt.get("stop_reason")})
