"""AgentCompass 集成测试：能力清单、参数组装、报告映射、执行器进度与收尾。

不真实起 docker / 容器；async_run_evaluation 不在本测试触发（延迟 import）。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from app.evaluation.agentcompass_adapter import (AGENTCOMPASS_CAPABILITIES, AGENTCOMPASS_BENCHMARK_ID,
                                                 AGENTCOMPASS_IDS, AgentCompassRunner, build_run_kwargs)
from app.evaluation.agentcompass_reports import map_run_to_report
from app.model_registry import ModelRegistry
from app.store import Store


# --------------------------------------------------------------------------- #
# 测试基建
# --------------------------------------------------------------------------- #
def _make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "data")
    store.update(lambda d: d["models"].append({
        "id": "m1", "name": "模型1", "provider": "x",
        "endpoints": [{"url": "http://127.0.0.1:9001/v1", "api_key": "local-secret"}],
        "scores": [],
    }))
    return store


def _seed_eval(store: Store, benchmark: str = "Agent") -> None:
    def op(data):
        data["evaluations"].append({
            "id": "eval-1", "model_id": "m1", "dataset_id": None,
            "benchmark": benchmark, "model_name": "模型1", "dataset_name": "AgentCompass 内置数据",
            "status": "queued", "progress": 0, "logs": [], "report_id": None,
            "simulated": False, "result": None,
            "agent": {"benchmark": "swebench_verified", "harness": "mini_swe_agent",
                      "environment": "docker", "model_api_protocol": "auto", "params": None},
        })
    store.update(op)


def _get_eval(store: Store) -> dict:
    return next(t for t in store.snapshot()["evaluations"] if t["id"] == "eval-1")


def _agent_task(**overrides) -> dict:
    agent = {"benchmark": "swebench_verified", "harness": "mini_swe_agent",
             "environment": "docker", "model_api_protocol": "openai-chat"}
    agent.update(overrides)
    return {"benchmark": "Agent", "agent": agent}


# --------------------------------------------------------------------------- #
# 能力白名单 manifest
# --------------------------------------------------------------------------- #
def test_capabilities_manifest():
    caps = AGENTCOMPASS_CAPABILITIES
    assert set(caps) == {"benchmarks", "harnesses", "environments", "protocols"}
    bench_ids = {b["id"] for b in caps["benchmarks"]}
    harness_ids = {h["id"] for h in caps["harnesses"]}
    env_ids = {e["id"] for e in caps["environments"]}
    proto_values = {p["value"] for p in caps["protocols"]}
    # 白名单范围
    assert "swebench_verified" in bench_ids and "gaia" in bench_ids
    assert "aisf_iot" not in bench_ids and "terminalbench2_1" not in bench_ids
    assert "claude_code" not in harness_ids and "aisf_agent" not in harness_ids
    assert env_ids == {"docker", "host_process"}
    assert proto_values == {"auto", "openai-chat", "openai-responses", "anthropic"}
    # suggested_harnesses 均指向白名单内的 harness
    for b in caps["benchmarks"]:
        assert b.get("suggested_harnesses"), f"{b['id']} 缺少 suggested_harnesses"
        for h in b["suggested_harnesses"]:
            assert h in harness_ids, f"{b['id']} 引用了未暴露的 harness {h}"
    for h in caps["harnesses"]:
        assert h.get("preferred_protocols"), f"{h['id']} 缺少 preferred_protocols"


def test_agent_benchmark_id():
    assert AGENTCOMPASS_BENCHMARK_ID == "Agent"
    assert AGENTCOMPASS_IDS == frozenset({"Agent"})


# --------------------------------------------------------------------------- #
# build_run_kwargs
# --------------------------------------------------------------------------- #
def test_build_run_kwargs_basic():
    task = _agent_task()
    kwargs = build_run_kwargs(task, {"id": "m1"}, {"url": "http://x/v1/"}, "secret",
                              data_dir="/d", results_dir="/r")
    assert kwargs["benchmark"] == "swebench_verified"
    assert kwargs["harness"] == "mini_swe_agent"
    assert kwargs["environment"] == "docker"
    assert kwargs["model"] == "m1"
    assert kwargs["model_base_url"] == "http://x/v1"      # url 原样，仅 rstrip("/")
    assert kwargs["model_api_key"] == "secret"
    assert kwargs["model_api_protocol"] == "openai-chat"
    assert kwargs["data_dir"] == "/d"
    assert kwargs["results_dir"] == "/r"
    assert kwargs["task_concurrency"] == 1
    assert kwargs["benchmark_params"] is None
    assert kwargs["progress"] == "none"


def test_build_run_kwargs_auto_protocol():
    task = _agent_task(benchmark="gaia", harness="openai_chat", model_api_protocol="auto")
    kwargs = build_run_kwargs(task, {"id": "m1"}, {"url": "http://x"}, "", data_dir="/d", results_dir="/r")
    assert kwargs["model_api_protocol"] == ""            # auto → 空（harness 默认）


def test_build_run_kwargs_sample_ids():
    task = _agent_task(params={"sample_ids": ["a__a-1", "b__b-2"]})
    kwargs = build_run_kwargs(task, {"id": "m1"}, {"url": "http://x"}, "", data_dir="/d", results_dir="/r")
    assert kwargs["benchmark_params"]["sample_ids"] == ["a__a-1", "b__b-2"]


def test_build_run_kwargs_params():
    task = _agent_task(params={"task_concurrency": 2, "k": 3, "max_retries": 5})
    kwargs = build_run_kwargs(task, {"id": "m1"}, {"url": "http://x"}, "", data_dir="/d", results_dir="/r")
    assert kwargs["task_concurrency"] == 2
    assert kwargs["k"] == 3
    assert kwargs["max_retries"] == 5


def test_build_run_kwargs_missing_harness():
    task = {"benchmark": "Agent", "agent": {"benchmark": "swebench_verified"}}
    with pytest.raises(ValueError):
        build_run_kwargs(task, {"id": "m1"}, {"url": "http://x"}, "", data_dir="/d", results_dir="/r")


# --------------------------------------------------------------------------- #
# map_run_to_report
# --------------------------------------------------------------------------- #
def _write_run_dir(run_dir: Path, *, correct=0.5, total=4, evaluated=4, error=0, run_id="20261008_175223"):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_info.json").write_text(json.dumps({
        "run_id": run_id, "status": "completed", "finished_at": "2026-10-08T17:52:23+08:00"}))
    (run_dir / "metrics.json").write_text(json.dumps({
        "series": [{"role": "headline", "value": correct,
                    "counts": {"total": total, "evaluated": evaluated, "error": error}}]}))
    (run_dir / "summary.md").write_text("# 评测完成\n正确率 50%")


def test_map_run_to_report(tmp_path):
    run_dir = tmp_path / "run"
    _write_run_dir(run_dir, correct=0.5)
    task = {"id": "eval-1", "benchmark": "Agent", "model_name": "模型1",
            "agent": {"benchmark": "swebench_verified", "harness": "mini_swe_agent", "environment": "docker"},
            "owner_user_id": None, "visibility": "private"}
    report, result = map_run_to_report(task, run_dir)
    assert report["score"] == 50.0
    assert result["total"] == 50.0
    assert result["dimensions"] == [{"key": "resolve", "label": "任务解决率", "score": 0.5, "weight": 1.0}]
    assert result["extra"]["run_id"] == "20261008_175223"
    assert result["extra"]["total"] == 4
    assert report["simulated"] is False
    assert report["ruler_id"] is None
    assert report["agent"]["benchmark"] == "swebench_verified"
    assert report["title"].startswith("swebench_verified")   # title 用 agent benchmark 名
    assert report["summary"].startswith("# 评测完成")


def test_map_run_to_report_empty():
    task = {"id": "eval-1", "benchmark": "Agent", "model_name": "模型1", "agent": {}}
    report, result = map_run_to_report(task, None)
    assert report["score"] == 0.0
    assert result["total"] == 0.0
    assert result["summary"]  # 有兜底 summary


# --------------------------------------------------------------------------- #
# 执行器：进度回写与收尾（不触发 agentcompass import）
# --------------------------------------------------------------------------- #
def test_on_progress_writes_store(tmp_path):
    store = _make_store(tmp_path)
    _seed_eval(store)
    runner = AgentCompassRunner(store, ModelRegistry(store),
                                data_dir=str(tmp_path / "d"), results_dir=str(tmp_path / "r"),
                                enabled=True)

    class _Ev:
        run_id = "run-1"
        event = "tasks_loaded"
        payload = {"total_tasks": 5}
        task_id = None
        status = None

    runner._on_progress("eval-1", _Ev())
    assert runner._runs["eval-1"] == "run-1"
    assert "已加载 5 条任务" in _get_eval(store)["logs"][-1]


def test_finalize_writes_report(tmp_path):
    store = _make_store(tmp_path)
    _seed_eval(store)
    runner = AgentCompassRunner(store, ModelRegistry(store),
                                data_dir=str(tmp_path / "d"), results_dir=str(tmp_path / "r"),
                                enabled=True)
    runner._runs["eval-1"] = "run-1"

    # 构造 AgentCompass 原生 results 目录（results_dir/<benchmark>/<model>/<run_id>）
    run_dir = Path(runner.results_dir) / "swebench_verified" / "m1" / "run-1"
    _write_run_dir(run_dir, correct=1.0, total=1, evaluated=1, run_id="run-1")

    async def run():
        fut = asyncio.Future()
        fut.set_result({"status": "completed"})
        await runner._finalize("eval-1", fut)

    asyncio.run(run())

    task = _get_eval(store)
    assert task["status"] == "completed"
    assert task["progress"] == 100
    assert task["report_id"]
    reports = store.snapshot()["reports"]
    assert reports and reports[0]["evaluation_id"] == "eval-1"
    assert reports[0]["score"] == 100.0


# --------------------------------------------------------------------------- #
# vendor 完整性：import agentcompass + 组件注册（依赖装完后生效）
# --------------------------------------------------------------------------- #
def test_vendor_import_complete():
    vendor = Path(__file__).resolve().parents[1] / "vendor"
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
    from agentcompass.runtime.registry import BENCHMARKS, HARNESSES, load_builtin_components

    load_builtin_components()
    benchmarks = BENCHMARKS.names()
    harnesses = HARNESSES.names()
    assert "swebench_verified" in benchmarks
    assert "aisf_iot" in benchmarks          # aisf 保留（vendor 完整，仅 iCAP 前端不暴露）
    assert "mini_swe_agent" in harnesses
    assert "aisf_agent" in harnesses          # aisf 保留
