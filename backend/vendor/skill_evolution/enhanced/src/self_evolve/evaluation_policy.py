"""Shared direct-mode policy, aggregation and stable artifact identities."""
import hashlib
import json
import math
from dataclasses import asdict, is_dataclass
from pathlib import Path

RESULT_WEIGHTS = {"process": 0.0, "conciseness": 0.0, "result": 1.0}
DIRECT_POLICY = {"execution_mode": "direct", "evaluation_mode": "result_only",
                 "evaluated_dimensions": ["result"], "judgment_weights": RESULT_WEIGHTS,
                 "optimization_weights": RESULT_WEIGHTS, "aggregation_mode": "case_mean",
                 "aggregation_version": 1, "schema_version": 1, "evaluator_version": 1}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def model_identity(client):
    config = getattr(client, "config", None)
    data = asdict(config) if is_dataclass(config) else dict(vars(config)) if config else {}
    return {k: v for k, v in data.items() if k not in {"api_key", "token", "password"}}


def case_mean(scores):
    values = [s.result for s in scores if not s.evaluation_error]
    if any(not math.isfinite(v) or not 0 <= v <= 100 for v in values):
        raise ValueError("result must be finite and within 0..100")
    return sum(values) / len(values) if values else None


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def prepare_direct_data(data, judge_prompt=""):
    from .evaluators.components.direct_judge import render_judge_prompt
    if not data:
        raise ValueError("direct dataset is empty")
    if len({str(i.id) for i in data}) != len(data):
        raise ValueError("direct case ids must be unique")
    for instance in data:
        md = instance.metadata
        if not md.get("input_messages"):
            raise ValueError(f"case {instance.id}: no input messages")
        expected = dict(md.get("expected_outputs") or {})
        weights = dict(md.get("output_weights") or {})
        result_kinds = ("text", "tool", "file", "onlyllm", "word", "format")

        def has_judge(kind: str) -> bool:
            if kind not in expected or weights.get(kind, 0) <= 0:
                return False
            if kind in ("word", "format"):
                return True  # 空 dict / 空字符串仍构成判据（分别回退到"期望空输出"/env 格式示例）
            return bool(expected[kind])

        if not any(has_judge(k) for k in result_kinds) and not expected.get("onlyllm") and judge_prompt:
            expected["onlyllm"] = str(Path(judge_prompt).resolve())
        if expected.get("onlyllm"):
            path = Path(expected["onlyllm"])
            if not path.is_absolute():
                local = Path(md["dataset_dir"]) / path
                path = local if local.is_file() else Path.cwd() / path
            path = path.resolve()
            try:
                template = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"case {instance.id}: cannot read judge template: {path}") from exc
            if not template.strip():
                raise ValueError(f"case {instance.id}: empty judge template: {path}")
            expected["onlyllm"] = str(path)
            md["judge_template"] = template
            md["judge_template_hash"] = fingerprint(template)
            render_judge_prompt(template, messages=md["input_messages"], output="", fields=md.get("judge_fields"))
            if not any(has_judge(k) for k in ("text", "tool", "file", "word", "format")):
                weights["onlyllm"] = 1.0
        if expected.get("file"):
            spec = dict(expected["file"])
            path = Path(spec.get("expect_path", ""))
            if not path.is_absolute():
                local = Path(md["dataset_dir"]) / path
                path = local if local.is_file() else Path.cwd() / path
            spec["expect_path"] = str(path.resolve())
            md["expected_file_content"] = path.read_text(encoding="utf-8")
            md.setdefault("judge_fields", {})["expected_file_content"] = md["expected_file_content"]
            expected["file"] = spec
        if any(not isinstance(w, (int, float)) or not math.isfinite(w) or w < 0 for w in weights.values()):
            raise ValueError(f"case {instance.id}: invalid output weights")
        if not any(has_judge(k) for k in result_kinds):
            raise ValueError(f"case {instance.id}: no effective result judge; configure DIRECT_JUDGE_PROMPT")
        md.update(expected_outputs=expected, output_weights=weights, execution_mode="direct")
    evaluation_set_id = fingerprint([str(i.id) for i in data])
    for instance in data:
        instance.metadata["evaluation_set_id"] = evaluation_set_id
    return evaluation_set_id
