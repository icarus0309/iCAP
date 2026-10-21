"""Shared main/candidate direct execution, with separate execution and judge caches."""
from pathlib import Path
from ..evaluation_policy import DIRECT_POLICY, fingerprint, model_identity, read_json, write_json
from ..schemas import HermesRunResult, ScoreBreakdown, SessionAnalysisResult, TrajectoryStep, to_plain_dict


def evaluate_direct_case(*, runner, analyzer, scorer, skill_path, instance, case_dir,
                         result_threshold=80, allow_reuse=True):
    case_dir = Path(case_dir)
    md = instance.metadata
    try:
        skill = Path(skill_path).read_text(encoding="utf-8")
    except OSError:
        skill = ""  # Runner records the read failure as an INVALID execution.
    execution_id = fingerprint({"version": 1, "mode": "direct", "model_role": runner.model_role,
        "model": model_identity(runner.model_client), "messages": md["input_messages"], "skill": skill})
    cache = read_json(case_dir / "cache.json") if allow_reuse else {}
    run = read_json(case_dir / "run_result.json")
    if cache.get("execution_id") == execution_id and run.get("execution_mode") == "direct" and not run.get("error") and run.get("returncode") == 0:
        rr = HermesRunResult(**run)
    else:
        rr = runner.run_case(case_id=str(instance.id), skill_path=skill_path,
                             input_messages=md["input_messages"], case_dir=case_dir)
    # Only the current final answer is an execution artifact; history is evidence, not new execution.
    trajectory = [TrajectoryStep(index=0, role="assistant", action_type="assistant_final", content=rr.final_output)] if rr.final_output else []
    write_json(case_dir / "trajectory.json", [to_plain_dict(t) for t in trajectory])
    write_json(case_dir / "session.normalized.json", {"case_id": str(instance.id),
        "messages": [*md["input_messages"], {"role": "assistant", "content": rr.final_output}],
        "final_output": rr.final_output, "metadata": {"execution_mode": "direct", "model_role": runner.model_role}})
    evaluation_id = fingerprint({"execution_id": execution_id, "output": rr.final_output,
        "evaluation_set_id": md.get("evaluation_set_id"),
        "policy": DIRECT_POLICY, "judge_model": model_identity(analyzer.model_client),
        "template": md.get("judge_template_hash"), "expected": md["expected_outputs"],
        "judge_fields": md.get("judge_fields"), "weights": md["output_weights"],
        "result_threshold": result_threshold, "stop_threshold": scorer.threshold})
    sa_data, score_data = read_json(case_dir / "session_analysis.json"), read_json(case_dir / "score.json")
    if cache.get("evaluation_id") == evaluation_id and sa_data and score_data and not score_data.get("evaluation_error"):
        sa = SessionAnalysisResult(**sa_data)
        score = ScoreBreakdown(**score_data)
    else:
        if rr.error or rr.returncode or not rr.final_output.strip():
            sa = SessionAnalysisResult(case_id=str(instance.id), execution_mode="direct", evaluation_mode="result_only",
                evaluated_dimensions=["result"], input_messages=md["input_messages"], final_output=rr.final_output,
                evaluation_error=rr.error or "execution failed or empty output")
            write_json(case_dir / "session_analysis.json", to_plain_dict(sa))
        else:
            sa = analyzer.analyze(case_id=str(instance.id), input_text=instance.input, trajectory=trajectory,
                execution_mode="direct", input_messages=md["input_messages"], final_output=rr.final_output,
                expected_outputs=md["expected_outputs"], output_weights=md["output_weights"],
                judge_template=md.get("judge_template", ""), judge_fields=md.get("judge_fields"), skill_text=skill,
                output_path=case_dir / "session_analysis.json")
        score = scorer.score(case_id=str(instance.id), input_text=instance.input,
            sub_type=md.get("sub_type", "default"), key_steps=[], process=0, conciseness=0,
            process_details={}, conciseness_details={}, output_scores=sa.output_scores,
            output_analyses=sa.output_analyses, output_weights=md["output_weights"],
            execution_mode="direct", evaluation_error=sa.evaluation_error, output_path=case_dir / "score.json")
    score.details.update(evaluation_set_id=md.get("evaluation_set_id"),
                         model_role=runner.model_role, model_identity=model_identity(runner.model_client))
    write_json(case_dir / "score.json", to_plain_dict(score))
    write_json(case_dir / "cache.json", {"execution_id": execution_id, "evaluation_id": evaluation_id,
                                         "execution_mode": "direct", "model_role": runner.model_role})
    return rr, trajectory, sa, score
