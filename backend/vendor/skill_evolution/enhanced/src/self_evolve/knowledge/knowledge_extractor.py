"""Extract evidence-backed, actionable knowledge from small-fail / large-success trace pairs.

Migrated from ``skill_distill_multi_dim/src/trace2skill/knowledge_extractor.py`` with the
following deliberate differences (see ``docs/skillimprove_merge_plan.md`` §改动3):

- 输出**自然文本知识经验**（``case_analysis.root_cause`` + ``case_analysis.suggestion``），不再产出 ``edits`` / patch 结构；
- 不迁移 patch/edit 机器（``sanitize_patch`` / ``guard_patch_edits`` / repair 等）；
- 结果分数改用 evo 的 ``SessionAnalyzer`` 结果维度（``result``），仅做一层
  ``evo result score -> distill result score 形状`` 的适配；
- 轨迹直接用 evo ``TrajectoryStep``（经由 ``build_model_evaluation_trajectory`` 适配）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

from ...model_client import QuotaExceededError
from ..schemas import TrajectoryStep
from .common import complete_json_with_retry, write_json
from .key_steps_judge import align_trajectory_to_key_steps, validate_key_steps
from .key_steps_output_evaluator import build_model_evaluation_trajectory
from .knowledge_prompts import (
    DIVERGENCE_VERIFICATION_SYSTEM,
    FIRST_DIVERGENCE_SYSTEM,
    KNOWLEDGE_EXTRACTION_SYSTEM,
    divergence_verification_prompt,
    first_divergence_prompt,
    knowledge_extraction_prompt,
)


def _events_for_evaluation(
    evaluation: dict[str, Any],
    trajectory: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    wanted = set(evaluation.get("actual_trajectory_steps") or [])
    return [event for event in trajectory if event.get("step") in wanted]


def _evidence_steps(value: Any, allowed: set[int]) -> list[int]:
    if not isinstance(value, list):
        return []
    return [step for step in value if isinstance(step, int) and not isinstance(step, bool) and step in allowed]


def _validated_divergence(
    raw: dict[str, Any],
    *,
    step_id: str,
    small_events: list[dict[str, Any]],
    large_events: list[dict[str, Any]],
    prior_small_events: list[dict[str, Any]] | None = None,
    prior_large_events: list[dict[str, Any]] | None = None,
    small_recovery_lookahead: list[dict[str, Any]] | None = None,
    large_recovery_lookahead: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prior_small_events = prior_small_events or []
    prior_large_events = prior_large_events or []
    small_recovery_lookahead = small_recovery_lookahead or []
    large_recovery_lookahead = large_recovery_lookahead or []
    small_current_allowed = {int(event["step"]) for event in small_events}
    large_current_allowed = {int(event["step"]) for event in large_events}
    small_prior_allowed = {int(event["step"]) for event in prior_small_events}
    large_prior_allowed = {int(event["step"]) for event in prior_large_events}
    small_allowed = small_current_allowed | small_prior_allowed
    large_allowed = large_current_allowed | large_prior_allowed
    recovery_allowed = small_current_allowed | {
        int(event["step"]) for event in small_recovery_lookahead
    }
    small_evidence = _evidence_steps(raw.get("small_evidence_steps"), small_allowed)
    large_evidence = _evidence_steps(raw.get("large_evidence_steps"), large_allowed)
    recovery_evidence = _evidence_steps(raw.get("recovery_evidence_steps"), recovery_allowed)
    transient_error = bool(raw.get("transient_error_found"))
    recovered_before_decision = bool(raw.get("recovered_before_decision")) and bool(recovery_evidence)
    end_of_step_equivalent = bool(raw.get("end_of_step_equivalent")) and bool(
        recovery_evidence or (small_evidence and large_evidence)
    )
    unresolved_consequence = str(raw.get("unresolved_consequence") or "").strip()
    one_sided_step_difference = bool(small_events) != bool(large_events)
    missing_trajectory_side = (
        "large"
        if small_events and not large_events
        else "small"
        if large_events and not small_events
        else "none"
    )
    absence_significance = str(raw.get("absence_significance") or "").strip()
    critical = bool(raw.get("critical_difference_found"))
    causal = bool(raw.get("causal_to_output_failure"))
    reason = str(raw.get("reason") or "")
    invalid_recovery_claim = bool(
        raw.get("recovered_before_decision") or raw.get("end_of_step_equivalent")
    ) and not (
        recovered_before_decision or end_of_step_equivalent
    )
    if invalid_recovery_claim:
        reason = "comparison claimed recovery without valid recovery evidence steps"
        critical = False
        causal = False
    elif recovered_before_decision or end_of_step_equivalent:
        critical = False
        causal = False
        reason = reason or "transient difference recovered before a consequential decision"
    elif critical and causal and one_sided_step_difference and not absence_significance:
        critical = False
        causal = False
        reason = "comparison did not explain why one-sided Key Step absence is behaviorally meaningful"
    elif critical and causal and one_sided_step_difference and (
        (small_events and not set(small_evidence).intersection(small_current_allowed))
        or (large_events and not set(large_evidence).intersection(large_current_allowed))
    ):
        critical = False
        causal = False
        reason = "comparison claimed a one-sided difference without evidence from the present side"
    elif critical and causal and not one_sided_step_difference and (
        not set(small_evidence).intersection(small_current_allowed)
        or not set(large_evidence).intersection(large_current_allowed)
    ):
        critical = False
        causal = False
        reason = "comparison claimed a critical difference without valid evidence steps"
    elif critical and causal and not unresolved_consequence:
        critical = False
        causal = False
        reason = "comparison did not identify an observable unresolved consequence"
    elif critical and causal and _uses_speculative_causality(reason, unresolved_consequence):
        critical = False
        causal = False
        reason = "comparison used speculative rather than observable causal evidence"
    return {
        "step_id": step_id,
        "critical_difference_found": critical,
        "causal_to_output_failure": causal,
        "small_error": str(raw.get("small_error") or ""),
        "large_correct_behavior": str(raw.get("large_correct_behavior") or ""),
        "small_evidence_steps": small_evidence,
        "large_evidence_steps": large_evidence,
        "transient_error_found": transient_error,
        "recovered_before_decision": recovered_before_decision,
        "recovery_evidence_steps": recovery_evidence,
        "end_of_step_equivalent": end_of_step_equivalent,
        "one_sided_step_difference": one_sided_step_difference,
        "missing_trajectory_side": missing_trajectory_side,
        "absence_significance": absence_significance,
        "unresolved_consequence": unresolved_consequence,
        "reason": reason,
    }


def _uses_speculative_causality(*values: str) -> bool:
    text = " ".join(values).casefold()
    return any(
        marker in text
        for marker in (
            "可能导致",
            "也许",
            "或许",
            "为后续奠定基础",
            "might lead",
            "may lead",
            "possibly",
        )
    )


def _recovery_lookahead(
    mappings: list[dict[str, Any]],
    mapping_index: int,
    trajectory: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose only the next Key Step, solely to disprove an earlier causal claim."""
    next_index = mapping_index + 1
    if next_index >= len(mappings):
        return []
    return _events_for_evaluation(mappings[next_index], trajectory)


def select_large_key_step_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project every event in the current key step without crossing its boundaries."""
    projected: list[dict[str, Any]] = []
    for event in events:
        item = {
            key: event.get(key)
            for key in ("step", "event_type", "action", "content", "tool_calls", "success")
        }
        reasoning = event.get("reasoning")
        if reasoning is not None and (not isinstance(reasoning, str) or bool(reasoning.strip())):
            item["reasoning"] = reasoning
        projected.append(item)
    return projected


def _deduplicate_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep trajectory events in order while removing repeated multi-mapped steps."""
    seen: set[int] = set()
    result: list[dict[str, Any]] = []
    for event in events:
        step = event.get("step")
        if not isinstance(step, int) or isinstance(step, bool) or step in seen:
            continue
        seen.add(step)
        result.append(event)
    return result


def _invalid_knowledge(case_id: Any, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "valid": False,
        "case_analysis": None,
        "reason": reason,
        **extra,
    }


def _result_score_dict(score: float, threshold: float) -> dict[str, Any]:
    """Adapt evo's result dimension into the distill result-score shape the
    divergence prompt expects (``dimension_scores.result.score`` / ``failure_focus``).
    """
    return {
        "evaluation_mode": "result_only",
        "threshold": threshold,
        "result_failure_threshold": threshold,
        "output_score": float(score),
        "dimension_scores": {"result": {"score": float(score)}},
        "result_success": float(score) >= threshold,
        "task_success": float(score) >= threshold,
        "failure_focus": "result",
        "evaluation_error": "",
    }


def extract_case_knowledge(
    *,
    case: dict[str, Any],
    small_trajectory: List[TrajectoryStep],
    large_trajectory: List[TrajectoryStep],
    small_result_score: float,
    large_result_score: float,
    result_threshold: float,
    client: Any,
    current_skill_excerpt: str,
    knowledge_dir: Path,
) -> dict[str, Any]:
    """Extract one natural-text knowledge experience from a KNOWLEDGE case.

    ``case`` carries ``case_id`` / ``utterance`` / ``key_steps`` / ``sum_answer`` /
    ``output`` / ``pre_condition``. Returns the ``knowledge.json`` content; the same
    dict is also written to ``knowledge_dir`` alongside the alignment / divergence
    audit artifacts.
    """
    case_id = case.get("case_id") or case.get("id")
    try:
        key_steps = validate_key_steps(case.get("key_steps"))
    except ValueError as exc:
        artifact = _invalid_knowledge(case_id, f"invalid_knowledge_case: {exc}")
        write_json(knowledge_dir / "knowledge.json", artifact)
        return artifact

    small_result = _result_score_dict(small_result_score, result_threshold)
    large_result = _result_score_dict(large_result_score, result_threshold)

    try:
        small_evaluation = build_model_evaluation_trajectory(
            case_id=str(case_id), trajectory=small_trajectory)
        large_evaluation = build_model_evaluation_trajectory(
            case_id=str(case_id), trajectory=large_trajectory)
        small_alignment = align_trajectory_to_key_steps(
            case=case,
            key_steps=key_steps,
            evaluation_trajectory=small_evaluation["trajectory"],
            client=client,
            model_role="small",
        )
        large_alignment = align_trajectory_to_key_steps(
            case=case,
            key_steps=key_steps,
            evaluation_trajectory=large_evaluation["trajectory"],
            client=client,
            model_role="large",
        )
    except QuotaExceededError:
        raise
    except Exception as exc:
        artifact = _invalid_knowledge(case_id, f"key_step_alignment_failed: {exc}")
        write_json(knowledge_dir / "knowledge.json", artifact)
        return artifact

    write_json(knowledge_dir / "key_step_alignment.small.json", small_alignment)
    write_json(knowledge_dir / "key_step_alignment.large.json", large_alignment)

    checks: list[dict[str, Any]] = []
    first_divergence: dict[str, Any] | None = None
    first_small_events: list[dict[str, Any]] = []
    first_large_events: list[dict[str, Any]] = []
    small_mappings = small_alignment["key_step_mappings"]
    large_mappings = large_alignment["key_step_mappings"]
    prior_key_step_evidence: list[dict[str, Any]] = []
    prior_small_events: list[dict[str, Any]] = []
    prior_large_events: list[dict[str, Any]] = []
    use_prior_evidence = False
    for mapping_index, (configured, small_step, large_step) in enumerate(
        zip(key_steps, small_mappings, large_mappings)
    ):
        small_events = _events_for_evaluation(small_step, small_evaluation["trajectory"])
        large_events = _events_for_evaluation(large_step, large_evaluation["trajectory"])
        small_recovery_lookahead = _recovery_lookahead(
            small_mappings, mapping_index, small_evaluation["trajectory"])
        large_recovery_lookahead = _recovery_lookahead(
            large_mappings, mapping_index, large_evaluation["trajectory"])
        available_prior_small_events = prior_small_events if use_prior_evidence else []
        available_prior_large_events = prior_large_events if use_prior_evidence else []
        comparison_input = {
            "case_context": {
                "user_request": case.get("utterance") or "",
                "key_step": configured,
                "expected_output_summary": case.get("output") or case.get("sum_answer") or "",
                "ground_truth": {
                    "expected_output": case.get("output") or [],
                    "summary_answer": case.get("sum_answer") or "",
                    "pre_conditions": case.get("pre_condition") or [],
                },
                "small_output_failure": small_result,
                "large_output_success": large_result,
            },
            "small_trajectory_for_key_step": small_events,
            "large_trajectory_for_key_step": large_events,
            "small_recovery_lookahead": small_recovery_lookahead,
            "large_recovery_lookahead": large_recovery_lookahead,
        }
        if use_prior_evidence:
            comparison_input["previous_key_step_evidence"] = prior_key_step_evidence
        raw = complete_json_with_retry(
            client,
            FIRST_DIVERGENCE_SYSTEM,
            first_divergence_prompt(comparison_input),
            max_tokens=16_384,
        )
        check = _validated_divergence(
            raw,
            step_id=configured["step_id"],
            small_events=small_events,
            large_events=large_events,
            prior_small_events=available_prior_small_events,
            prior_large_events=available_prior_large_events,
            small_recovery_lookahead=small_recovery_lookahead,
            large_recovery_lookahead=large_recovery_lookahead,
        )
        verification_rejected_candidate = False
        if check["critical_difference_found"] and check["causal_to_output_failure"]:
            candidate = check
            verification_input = {
                **comparison_input,
                "candidate_divergence": candidate,
                "verification_rule": (
                    "lookahead 只能用于否证恢复情况，不能把后续错误提前归因到当前 Key Step；"
                    "此前 Key Steps 的证据可用于判断当前单边缺失是否源于正确停止、等待或错误分支"
                ),
            }
            raw_verification = complete_json_with_retry(
                client,
                DIVERGENCE_VERIFICATION_SYSTEM,
                divergence_verification_prompt(verification_input),
                max_tokens=16_384,
            )
            verified = _validated_divergence(
                raw_verification,
                step_id=configured["step_id"],
                small_events=small_events,
                large_events=large_events,
                prior_small_events=available_prior_small_events,
                prior_large_events=available_prior_large_events,
                small_recovery_lookahead=small_recovery_lookahead,
                large_recovery_lookahead=large_recovery_lookahead,
            )
            verification_rejected_candidate = not (
                verified["critical_difference_found"]
                and verified["causal_to_output_failure"]
            )
            check = {
                **verified,
                "verification_performed": True,
                "candidate_divergence": candidate,
            }
        else:
            check["verification_performed"] = False
        checks.append(check)
        prior_key_step_evidence.append(
            {
                "key_step": configured,
                "small_trajectory": select_large_key_step_events(small_events),
                "large_trajectory": select_large_key_step_events(large_events),
                "comparison_result": check,
            }
        )
        prior_small_events = _deduplicate_events([*prior_small_events, *small_events])
        prior_large_events = _deduplicate_events([*prior_large_events, *large_events])
        if verification_rejected_candidate:
            use_prior_evidence = True
        write_json(knowledge_dir / "divergence_checks.json", {"case_id": case_id, "checks": checks})
        if check["critical_difference_found"] and check["causal_to_output_failure"]:
            first_divergence = check
            first_small_events = _deduplicate_events(
                [*available_prior_small_events, *small_events])
            first_large_events = _deduplicate_events(
                [*available_prior_large_events, *large_events])
            break

    if first_divergence is None:
        artifact = _invalid_knowledge(
            case_id,
            "no_causally_supported_key_step_divergence",
            divergence_checks=checks,
        )
        write_json(knowledge_dir / "knowledge.json", artifact)
        return artifact

    large_key_step_events = select_large_key_step_events(first_large_events)
    reasoning_available = any("reasoning" in event for event in large_key_step_events)
    extraction_input = {
        "case_context": {
            "user_request": case.get("utterance") or "",
            "key_steps": key_steps,
        },
        "first_divergence": first_divergence,
        "small_key_step_trajectory": first_small_events,
        "large_key_step_trajectory": large_key_step_events,
        "small_output_evaluation": small_result,
        "large_output_evaluation": large_result,
        "current_skill_excerpt": current_skill_excerpt,
        "reasoning_available": reasoning_available,
    }
    raw_knowledge = complete_json_with_retry(
        client,
        KNOWLEDGE_EXTRACTION_SYSTEM,
        knowledge_extraction_prompt(extraction_input),
        max_tokens=16_384,
    )

    raw_case_analysis = raw_knowledge.get("case_analysis") or {}
    if not isinstance(raw_case_analysis, dict):
        raw_case_analysis = {}
    case_analysis = {
        "root_cause": str(raw_case_analysis.get("root_cause") or "").strip(),
        "suggestion": str(raw_case_analysis.get("suggestion") or "").strip(),
        "improvement_direction": str(raw_case_analysis.get("improvement_direction") or "").strip(),
        "applicability_boundary": str(raw_case_analysis.get("applicability_boundary") or "").strip(),
        "evidence_summary": str(raw_case_analysis.get("evidence_summary") or "").strip(),
    }
    artifact: dict[str, Any] = {
        "case_id": case_id,
        "valid": bool(raw_knowledge.get("valid", True)),
        "case_analysis": case_analysis,
        "first_divergent_step_id": first_divergence["step_id"],
        "reasoning_available": reasoning_available,
        "evidence": {
            "small_steps": first_divergence["small_evidence_steps"],
            "large_steps": first_divergence["large_evidence_steps"],
        },
        "divergence_checks": checks,
    }
    # 无有效自然文本知识 -> 标记无效，避免污染经验池。
    if artifact["valid"] and not (case_analysis["root_cause"] and case_analysis["suggestion"]):
        artifact["valid"] = False
        artifact["reason"] = "no_actionable_natural_text_knowledge"

    write_json(knowledge_dir / "knowledge.json", artifact)
    return artifact


__all__ = [
    "extract_case_knowledge",
    "select_large_key_step_events",
]
