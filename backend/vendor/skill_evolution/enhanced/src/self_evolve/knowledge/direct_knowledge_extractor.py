"""Output-comparison knowledge with verifiable quotes and cache identity."""
import json
from ..evaluation_policy import fingerprint, model_identity, read_json, write_json
from ..evaluators.components.session_analyzer import SessionAnalyzer
from ...model_client import QuotaExceededError
from .direct_prompts import KNOWLEDGE_SYSTEM


def extract_direct_knowledge(*, case_id, input_messages, skill_text, small_output, large_output,
                             small_score, large_score, small_judge, large_judge,
                             result_threshold, client, knowledge_dir, cache_context=None):
    payload = {"input_messages": input_messages, "current_skill_excerpt": skill_text[:4000],
        "small_output": small_output, "large_output": large_output,
        "small_judge_result": small_judge, "large_judge_result": large_judge,
        "small_score": small_score, "large_score": large_score, "result_threshold": result_threshold}
    identity = fingerprint({"version": 1, "payload": payload, "model": model_identity(client), "context": cache_context})
    path = knowledge_dir / "knowledge.json"
    cached = read_json(path)
    if cached.get("cache_id") == identity:
        return cached
    artifact = {"schema_version": 1, "extractor_version": 1, "case_id": case_id, "artifact_type": "knowledge",
        "execution_mode": "direct", "evidence_type": "final_output_comparison", "reasoning_available": False,
        "valid": False, "cache_id": identity, "reason": "insufficient_or_unverifiable_final_output_evidence"}
    raw = {}
    if small_score < result_threshold <= large_score and small_output.strip() and large_output.strip() and small_output.strip() != large_output.strip():
        analyzer = SessionAnalyzer(client)
        for _ in range(2):
            try:
                response = client.generate([{"role": "system", "content": KNOWLEDGE_SYSTEM},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
                raw = analyzer._parse_json_strict(response)
                if not isinstance(raw, dict):
                    raise ValueError("knowledge response must be object")
                artifact["raw_response"] = response
                break
            except QuotaExceededError:
                raise
            except Exception as exc:
                artifact["error"] = str(exc)
    analysis = raw.get("case_analysis") if isinstance(raw.get("case_analysis"), dict) else {}
    keys = ("root_cause", "pattern", "improvement_direction", "applicability_boundary", "evidence_summary")
    analysis = {k: analysis.get(k, "") if isinstance(analysis.get(k, ""), str) else "" for k in keys}
    claimed = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    sources = {"small_output_spans": small_output, "large_output_spans": large_output,
               "judge_findings": json.dumps([small_judge, large_judge], ensure_ascii=False)}
    evidence = {}
    for key, text in sources.items():
        spans = claimed.get(key)
        evidence[key] = spans if isinstance(spans, list) and spans and all(isinstance(s, str) and s.strip() and s in text for s in spans) else []
    artifact.update(case_analysis=analysis, evidence=evidence,
        valid=raw.get("valid") is True and all(v.strip() for v in analysis.values()) and all(evidence.values()))
    if artifact["valid"]:
        artifact.pop("reason", None)
    write_json(path, artifact)
    write_json(knowledge_dir / "evidence.json", payload)
    return artifact
