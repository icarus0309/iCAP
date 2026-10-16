"""Run-level runtime telemetry aggregation for ``run_metrics.json``.

This module extracts per-task runtime telemetry (trajectory step count, wall-clock
latency, and token usage) from persisted task details
and aggregates it into the ``run_metrics.json`` schema.

It is deliberately independent of the metric-report pipeline: ``metrics.json``
holds benchmark judgements (correct/score), while ``run_metrics.json`` holds
runtime diagnostics. See ``DevDocs/run_metrics_json_遥测指标汇总设计.md``.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional

RUN_METRICS_SCHEMA_VERSION = "agentcompass.run_metrics.v1"

# Harnesses whose ``raw_codex_events`` carry a cumulative ``turn.completed.usage``.
_CODEX_HARNESS_IDS = frozenset({"codex"})

_TOKEN_KEYS = ("input_tokens", "output_tokens", "reasoning_output_tokens")


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Parse a trajectory/run timestamp (``YYYY-MM-DD HH:MM:SS.ffffff+HH:MM``).

    In-memory trajectories store ``datetime`` objects (``asdict`` leaves them
    unstringified); persisted detail records store ISO strings. Accept both.
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        normalized = text.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None


def _trajectory_step_bounds(steps: Any) -> tuple[Optional[datetime], Optional[datetime]]:
    """Infer trajectory start/end from step timestamps when top-level fields are absent."""
    if not isinstance(steps, list) or not steps:
        return None, None
    first = steps[0] if isinstance(steps[0], dict) else {}
    last = steps[-1] if isinstance(steps[-1], dict) else {}
    return _parse_iso_datetime(first.get("started_at")), _parse_iso_datetime(last.get("finished_at"))


def _trajectory_bounds(trajectory: Any) -> tuple[Optional[datetime], Optional[datetime]]:
    """Return (started_at, finished_at) datetimes for a trajectory's top-level fields."""
    if not isinstance(trajectory, dict):
        return None, None
    started = _parse_iso_datetime(trajectory.get("started_at"))
    finished = _parse_iso_datetime(trajectory.get("finished_at"))
    if started is None or finished is None:
        s, f = _trajectory_step_bounds(trajectory.get("steps"))
        if started is None:
            started = s
        if finished is None:
            finished = f
    return started, finished


def _latency_seconds(trajectory: Any) -> Optional[float]:
    """Wall-clock latency from the trajectory's top-level timestamps.

    Step-internal ``started_at``/``finished_at`` are placeholders (they equal the
    trajectory start time); only the trajectory-level fields are real wall time.
    When a harness omits the top-level fields (e.g. mini_swe_agent serializes only
    ``schema_version`` + ``steps``), fall back to the first step's ``started_at``
    and the last step's ``finished_at``.
    """
    if not isinstance(trajectory, dict):
        return None
    started = _parse_iso_datetime(trajectory.get("started_at"))
    finished = _parse_iso_datetime(trajectory.get("finished_at"))
    if started is None or finished is None:
        started, finished = _trajectory_step_bounds(trajectory.get("steps"))
    if started is None or finished is None:
        return None
    seconds = (finished - started).total_seconds()
    # Placeholder timestamps (started == finished, e.g. aisf_agent) carry no
    # meaningful wall time; treat sub-millisecond deltas as absent.
    if seconds < 0.001:
        return None
    return seconds if seconds >= 0 else None


def _steps_count(trajectory: Any) -> Optional[int]:
    if not isinstance(trajectory, dict):
        return None
    steps = trajectory.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    return len(steps)


def _extract_codex_tokens(raw_events: Any) -> Optional[Dict[str, int]]:
    """Extract cumulative token usage from codex's ``raw_codex_events``.

    The last ``turn.completed`` event carries the cumulative usage in its
    top-level ``usage`` field.
    """
    if not isinstance(raw_events, list):
        return None
    usage: Optional[Dict[str, Any]] = None
    for event in raw_events:
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        candidate = event.get("usage")
        if isinstance(candidate, dict):
            usage = candidate
    if usage is None:
        return None

    tokens: Dict[str, int] = {}
    for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            tokens[key] = value
    if "input_tokens" not in tokens and "output_tokens" not in tokens:
        return None
    return tokens


def _extract_step_tokens(trajectory: Any) -> Optional[Dict[str, int]]:
    """Sum per-step token usage from a trajectory's ``steps[].metric``.

    Non-codex harnesses that either call the LLM through the framework's client
    (openai_chat, scicode_tool_use, ...) or record the native agent's ``usage``
    (openclaw, claude_code, mini_swe_agent) populate ``prompt_tokens_len`` and
    ``completion_tokens_len`` on each step's metric. Reasoning tokens are not
    part of the shared ``TrajMetric`` schema, so only input/output are
    aggregated here. Returns ``None`` when no step carries a token count
    (e.g. aisf_agent's black-box responses).
    """
    if not isinstance(trajectory, dict):
        return None
    steps = trajectory.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    for step in steps:
        if not isinstance(step, dict):
            continue
        metric = step.get("metric")
        if not isinstance(metric, dict):
            continue
        prompt = metric.get("prompt_tokens_len")
        completion = metric.get("completion_tokens_len")
        if isinstance(prompt, int) and not isinstance(prompt, bool):
            input_tokens = (input_tokens or 0) + prompt
        if isinstance(completion, int) and not isinstance(completion, bool):
            output_tokens = (output_tokens or 0) + completion
    if input_tokens is None and output_tokens is None:
        return None
    # 全 0 表示数据源未统计 token（如 claude_code 的 usage 恒为 0），视为无数据而非 0
    if (input_tokens or 0) == 0 and (output_tokens or 0) == 0:
        return None
    tokens: Dict[str, int] = {}
    if input_tokens is not None:
        tokens["input_tokens"] = input_tokens
    if output_tokens is not None:
        tokens["output_tokens"] = output_tokens
    return tokens


def _outcome(status: Any, correct: Any) -> str:
    """Derive the three-state outcome: correct / incorrect / error."""
    if status != "completed":
        return "error"
    return "correct" if correct is True else "incorrect"


def _first_attempt(attempts: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(attempts, dict) or not attempts:
        return None
    attempt = attempts.get("1")
    if isinstance(attempt, dict):
        return attempt
    for candidate in attempts.values():
        if isinstance(candidate, dict):
            return candidate
    return None


def _extract_task(detail: Any, harness_id: str) -> Dict[str, Any]:
    if not isinstance(detail, dict):
        return {
            "task_id": "",
            "status": "",
            "outcome": "error",
            "steps": None,
            "latency_seconds": None,
            "tokens": None,
        }

    task_id = str(detail.get("task_id") or "")
    attempt = _first_attempt(detail.get("attempts"))

    status = ""
    correct = None
    trajectory: Any = {}
    artifacts: Any = {}
    if attempt is not None:
        status = str(attempt.get("status") or "")
        metrics = attempt.get("metrics")
        if isinstance(metrics, dict):
            correct = metrics.get("correct")
        trajectory = attempt.get("trajectory") or {}
        artifacts = attempt.get("artifacts") or {}

    tokens: Optional[Dict[str, int]] = None
    if harness_id in _CODEX_HARNESS_IDS:
        tokens = _extract_codex_tokens(artifacts.get("raw_codex_events"))
    else:
        tokens = _extract_step_tokens(trajectory)

    return {
        "task_id": task_id,
        "status": status,
        "outcome": _outcome(status, correct),
        "steps": _steps_count(trajectory),
        "latency_seconds": _latency_seconds(trajectory),
        "tokens": tokens,
    }


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return math.fsum(values) / len(values)


def _total_one(tokens: Dict[str, int]) -> int:
    return sum(tokens[key] for key in _TOKEN_KEYS if isinstance(tokens.get(key), int))


def _sum_token_field(token_tasks: List[Dict[str, int]], key: str) -> Optional[int]:
    values = [tokens[key] for tokens in token_tasks if isinstance(tokens.get(key), int)]
    return sum(values) if values else None


def _wall_seconds(started_at: Optional[str]) -> Optional[float]:
    """Approximate run wall time from ``started_at`` to now.

    ``save_results`` runs before ``finish`` writes the authoritative
    ``finished_at``, so this value is an approximation; the authoritative value
    is ``run_info.json``'s ``finished_at - started_at``.
    """
    started = _parse_iso_datetime(started_at)
    if started is None:
        return None
    seconds = (datetime.now().astimezone() - started).total_seconds()
    return seconds if seconds >= 0 else None


def wall_seconds_between(started_at: Optional[str], finished_at: Optional[str]) -> Optional[float]:
    """Compute exact wall time between two run timestamps (finished - started)."""
    started = _parse_iso_datetime(started_at)
    finished = _parse_iso_datetime(finished_at)
    if started is None or finished is None:
        return None
    seconds = (finished - started).total_seconds()
    return seconds if seconds >= 0 else None


def build_run_metrics_payload(
    details: List[Any],
    *,
    harness_id: str,
    benchmark_id: str,
    model_id: str,
    run_id: str,
    started_at: Optional[str],
) -> Dict[str, Any]:
    """Build the full ``run_metrics.json`` payload from persisted task details."""
    per_task = [_extract_task(detail, harness_id) for detail in details]
    completed = [task for task in per_task if task["status"] == "completed"]

    latencies = [task["latency_seconds"] for task in completed
                 if isinstance(task["latency_seconds"], (int, float))]
    steps = [task["steps"] for task in completed if isinstance(task["steps"], int)]

    token_tasks = [task["tokens"] for task in per_task if isinstance(task["tokens"], dict)]
    tokens_available = bool(token_tasks)

    # Execution wall time: 第一个用例开始 → 最后一个用例结束（纯 agent 跑用例，不含环境拉起/判分）
    starts: List[datetime] = []
    finishes: List[datetime] = []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        attempt = _first_attempt(detail.get("attempts"))
        trajectory = (attempt or {}).get("trajectory") or {}
        started, finished = _trajectory_bounds(trajectory)
        if started is not None:
            starts.append(started)
        if finished is not None:
            finishes.append(finished)
    execution_wall_seconds: Optional[float] = None
    if starts and finishes:
        delta = (max(finishes) - min(starts)).total_seconds()
        if delta >= 0:
            execution_wall_seconds = delta

    summary: Dict[str, Any] = {
        "total_tasks": len(per_task),
        "evaluated_tasks": len(completed),
        "total_wall_seconds": _wall_seconds(started_at),
        "execution_wall_seconds": execution_wall_seconds,
        "avg_latency_seconds": _mean(latencies),
        "avg_steps": _mean([float(step) for step in steps]),
        "tokens_available": tokens_available,
    }

    if tokens_available and token_tasks:
        totals = [_total_one(tokens) for tokens in token_tasks]
        summary["total_tokens"] = sum(totals)
        summary["input_tokens"] = _sum_token_field(token_tasks, "input_tokens")
        summary["output_tokens"] = _sum_token_field(token_tasks, "output_tokens")
        summary["reasoning_output_tokens"] = _sum_token_field(token_tasks, "reasoning_output_tokens")
        summary["avg_tokens_per_task"] = _mean([float(total) for total in totals])
    else:
        summary["total_tokens"] = None
        summary["input_tokens"] = None
        summary["output_tokens"] = None
        summary["reasoning_output_tokens"] = None
        summary["avg_tokens_per_task"] = None

    return {
        "schema_version": RUN_METRICS_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_id": run_id,
        "benchmark": benchmark_id,
        "harness": harness_id,
        "model": model_id,
        "started_at": started_at,
        "summary": summary,
        "per_task": per_task,
    }
