"""Strict judge parsing and single-pass placeholder expansion."""
import json
import math
import re
from datetime import datetime


def parse_judge_result(result):
    if not isinstance(result, dict):
        raise ValueError("judge must return an object")
    if "issues" in result:
        if not isinstance(result["issues"], list):
            raise ValueError("judge issues must be an array")
        return 0.0 if result["issues"] else 100.0
    if "审核结果" in result:
        verdict = result["审核结果"]
        if not isinstance(verdict, str) or not verdict.strip():
            raise ValueError("judge 审核结果 must be a non-empty string")
        return 100.0 if verdict.strip() == "合规" else 0.0
    score = result.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 100:
        raise ValueError("judge must return issues, 审核结果, or finite 0..100 score")
    return float(score)


def render_judge_prompt(template, *, messages, output, fields=None, skill_text=""):
    values = dict(fields or {})
    values.update(current_year=datetime.now().year,
                  conversation="\n".join(m["content"] if isinstance(m["content"], str) else json.dumps(m["content"], ensure_ascii=False)
                                           for m in messages if m["role"] == "user"),
                  input_messages=json.dumps(messages, ensure_ascii=False), input=json.dumps(messages, ensure_ascii=False),
                  memory_list=output, candidate_output=output, model_output=output, assistant_output=output,
                  skill_prompt=skill_text, skill_excerpt=skill_text[:7000])
    for key in ("history_memory_list", "peer_memory_list", "fewshots"):
        values.setdefault(key, "无")
    def replace(match):
        key = match.group(1)
        if key not in values:
            raise ValueError(f"unknown judge placeholder: {key}")
        val = values[key]
        return val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
    # Only placeholders are replaced. JSON object braces (including adjacent closing braces) stay literal.
    return re.sub(r"(?<!\{)\{([A-Za-z_][A-Za-z_0-9]*)\}(?!\})", replace, template)
