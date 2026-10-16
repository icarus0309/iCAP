"""把 AgentCompass run 结果映射成 iCAP 的 report/result。

只读 results/<benchmark>/<model>/<run_id>/ 下的产物文件（run_info/metrics/run_metrics/
summary），生成 iCAP 平台报告；AgentCompass 原始结果目录与产物不动。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> Any:
    if not path or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_text(path: Path, limit: int = 2000) -> str:
    if not path or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")[:limit]
    except OSError:
        return ""


def _headline_series(metrics: Any) -> dict:
    """取 metrics.json 里 role=headline 的系列（正确率）。"""
    if not isinstance(metrics, dict):
        return {}
    for series in metrics.get("series") or []:
        if isinstance(series, dict) and series.get("role") == "headline":
            return series
    return {}


def map_run_to_report(task: dict, run_dir: Path | None, run_result: Any = None) -> tuple[dict, dict]:
    """读 run_dir 产物，生成 (report, result)。

    - score = 正确率 × 100
    - dimensions = 单一维度「任务解决率」
    - result.extra 存 run_id/run_dir/原始计数，供详情页 drill-down 到原始产物
    """
    metrics = _read_json(run_dir / "metrics.json") if run_dir else None
    run_info = _read_json(run_dir / "run_info.json") if run_dir else None
    run_metrics = _read_json(run_dir / "run_metrics.json") if run_dir else None
    summary_text = _read_text(run_dir / "summary.md") if run_dir else ""

    series = _headline_series(metrics)
    correct_rate = series.get("value")
    if not isinstance(correct_rate, (int, float)):
        correct_rate = 0.0
    counts = series.get("counts") or {}
    total = int(counts.get("total", 0) or 0)
    evaluated = int(counts.get("evaluated", 0) or 0)
    error = int(counts.get("error", 0) or 0)

    benchmark_label = (task.get("agent") or {}).get("benchmark") or task.get("benchmark", "")
    score = round(float(correct_rate) * 100, 1)
    summary = summary_text.strip() or (
        f"{benchmark_label} 评测：{evaluated}/{total} 条完成，解决率 {score}%"
    )

    dimensions = [{
        "key": "resolve",
        "label": "任务解决率",
        "score": round(float(correct_rate), 4),
        "weight": 1.0,
    }]

    run_id = (run_info or {}).get("run_id") or ""
    summary_metrics = (run_metrics or {}).get("summary") or {}
    result = {
        "mode": "live",
        "total": score,
        "summary": summary,
        "finished_at": (run_info or {}).get("finished_at") or now(),
        "dimensions": dimensions,
        "extra": {
            "run_id": run_id,
            "run_dir": str(run_dir) if run_dir else "",
            "evaluated": evaluated,
            "total": total,
            "error": error,
            "avg_latency_seconds": summary_metrics.get("avg_latency_seconds"),
            "avg_steps": summary_metrics.get("avg_steps"),
            "total_tokens": summary_metrics.get("total_tokens"),
        },
    }

    report = {
        "id": f"report-{uuid.uuid4().hex[:12]}",
        "evaluation_id": task["id"],
        "title": f"{benchmark_label} · {task.get('model_name', '')}",
        "score": score,
        "ruler_id": None,
        "agent": task.get("agent") or {},
        "dimensions": dimensions,
        "summary": summary,
        "created_at": now(),
        "published": False,
        "simulated": False,
        "owner_user_id": task.get("owner_user_id"),
        "visibility": task.get("visibility", "public"),
    }
    return report, result
