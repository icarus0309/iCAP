"""iCAP 侧 AgentCompass 评测执行器（进程内 vendor 适配层）。

把 iCAP 评测任务（Agent 域）通过进程内 async_run_evaluation 转成真实 Agent 评测。
进度经 on_progress 回调写回 store，完成后读 results 目录生成平台报告。
AgentCompass 原生结果目录/产物原样保留，本模块只读不改。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agentcompass_reports import map_run_to_report

# 把 vendor 目录加入 sys.path，使 iCAP 能 import vendor 的 agentcompass 包（进程内集成）。
# async_run_evaluation 延迟到 _start 里 import，避免依赖未装时 import 本模块即失败。
_VENDOR_DIR = Path(__file__).resolve().parents[2] / "vendor"
if str(_VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DIR))

# iCAP 评测枚举值：Agent 域统一走 AgentCompass 真实评测。
AGENTCOMPASS_BENCHMARK_ID = "Agent"
AGENTCOMPASS_IDS = frozenset({AGENTCOMPASS_BENCHMARK_ID})

# 能力白名单 manifest：只暴露本地有 data、且配「纯 Python + LLM 直连」harness 的组合。
# 排除：claude_code/codex/pi/hermes/openclaw/openhands/openevolve/terminus2/aisf_agent/
#       qwen3vl_gui/scicode_tool_use/researchharness，及 benchmark aisf_iot/terminalbench2_1。
AGENTCOMPASS_CAPABILITIES: dict[str, list[dict]] = {
    "benchmarks": [
        {"id": "swebench_verified", "label": "SWE-bench Verified",
         "description": "SWE-bench: Can Language Models Resolve Real-World GitHub Issues? "
                        "(https://arxiv.org/abs/2310.06770). AgentCompass uses the SWE-bench Verified subset.",
         "suggested_harnesses": ["mini_swe_agent"]},
        {"id": "swebench_lite", "label": "SWE-bench Lite",
         "description": "SWE-bench: Can Language Models Resolve Real-World GitHub Issues? "
                        "(https://arxiv.org/abs/2310.06770). AgentCompass uses the SWE-bench Lite subset.",
         "suggested_harnesses": ["mini_swe_agent"]},
        {"id": "swebench_pro", "label": "SWE-bench Pro",
         "description": "SWE-Bench Pro: Can AI Agents Solve Long-Horizon Software Engineering Tasks? "
                        "(https://arxiv.org/abs/2509.16941).",
         "suggested_harnesses": ["mini_swe_agent"]},
        {"id": "gaia", "label": "GAIA",
         "description": "GAIA: a benchmark for General AI Assistants (https://arxiv.org/abs/2311.12983).",
         "suggested_harnesses": ["naive_search_agent", "openai_chat"]},
        {"id": "browsecomp", "label": "BrowseComp",
         "description": "BrowseComp: A Simple Yet Challenging Benchmark for Browsing Agents "
                        "(https://arxiv.org/abs/2504.12516).",
         "suggested_harnesses": ["naive_search_agent"]},
        {"id": "browsecomp_zh", "label": "BrowseComp-ZH",
         "description": "BrowseComp-ZH: Benchmarking Web Browsing Ability of Large Language Models in Chinese "
                        "(https://arxiv.org/abs/2504.19314).",
         "suggested_harnesses": ["naive_search_agent"]},
        {"id": "hle", "label": "HLE",
         "description": "Humanity's Last Exam (https://arxiv.org/abs/2501.14249).",
         "suggested_harnesses": ["openai_chat"]},
        {"id": "skillsbench", "label": "SkillsBench",
         "description": "SkillsBench: Benchmarking How Well Agent Skills Work Across Diverse Tasks "
                        "(https://arxiv.org/abs/2602.12670).",
         "suggested_harnesses": ["openai_chat", "mini_swe_agent"]},
    ],
    "harnesses": [
        {"id": "openai_chat", "label": "OpenAI Chat",
         "description": "直接调用模型，无环境交互，用于问答类 benchmark（GAIA/HLE/SkillsBench）。",
         "preferred_protocols": ["openai-chat"]},
        {"id": "mini_swe_agent", "label": "mini-SWE-agent",
         "description": "Runs mini-SWE-agent for SWE-bench-style repository repair tasks.",
         "preferred_protocols": ["openai-chat"]},
        {"id": "naive_search_agent", "label": "NaiveSearchAgent",
         "description": "AgentCompass 内置搜索 agent，用于 GAIA/BrowseComp 等研究类任务。",
         "preferred_protocols": ["openai-chat"]},
    ],
    "environments": [
        {"id": "docker", "label": "Docker 容器"},
        {"id": "host_process", "label": "宿主进程"},
    ],
    "protocols": [
        {"value": "auto", "label": "自动（harness 默认）"},
        {"value": "openai-chat", "label": "OpenAI Chat Completions"},
        {"value": "openai-responses", "label": "OpenAI Responses"},
        {"value": "anthropic", "label": "Anthropic Messages"},
    ],
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_run_kwargs(task: dict, model: dict, endpoint: dict, api_key: str,
                     *, data_dir: str, results_dir: str) -> dict:
    """纯函数：把 iCAP 任务 + 模型端点组装成 async_run_evaluation 的 kwargs。

    组件选择（benchmark/harness/environment/protocol/params）直接读 task["agent"]，
    不再查硬编码映射表。model_base_url 原样使用端点的 url（base，不含 /chat/completions），
    由 AgentCompass 按 protocol 拼接，iCAP 不自行拼路径。
    """
    agent = task.get("agent") or {}
    benchmark = agent.get("benchmark")
    harness = agent.get("harness")
    if not benchmark or not harness:
        raise ValueError("agent 配置缺少 benchmark/harness")
    environment = agent.get("environment") or "docker"
    protocol = agent.get("model_api_protocol") or ""
    if protocol == "auto":
        protocol = ""  # 空 = harness 默认协议
    params = agent.get("params") or {}

    benchmark_params: dict[str, Any] = {}
    if params.get("sample_ids"):
        benchmark_params["sample_ids"] = [str(x) for x in params["sample_ids"]]

    kwargs: dict[str, Any] = {
        "benchmark": benchmark,
        "harness": harness,
        "model": model["id"],
        "environment": environment,
        "model_base_url": (endpoint.get("url") or "").rstrip("/"),
        "model_api_key": api_key or "",
        "model_api_protocol": protocol,
        "benchmark_params": benchmark_params or None,
        "data_dir": data_dir,
        "results_dir": results_dir,
        "task_concurrency": int(params.get("task_concurrency") or 1),
        "progress": "none",
        "log_level": "ERROR",
    }
    if params.get("k") is not None:
        kwargs["k"] = int(params["k"])
    if params.get("max_retries") is not None:
        kwargs["max_retries"] = int(params["max_retries"])
    return kwargs


class AgentCompassRunner:
    """单 worker 串行执行器：启动 async_run_evaluation 长任务并收尾。"""

    def __init__(self, store, registry, *, data_dir: str, results_dir: str,
                 stop: asyncio.Event | None = None, enabled: bool = False):
        self.store = store
        self.registry = registry
        self.data_dir = data_dir
        self.results_dir = results_dir
        self.stop = stop or asyncio.Event()
        self.enabled = enabled
        self.active_task_id: str | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._runs: dict[str, str] = {}
        self._totals: dict[str, int] = {}
        self._finished: dict[str, int] = {}

    # ---- 公开入口（advance_jobs 每 tick 调用）----
    async def step(self) -> None:
        if not self.enabled:
            return
        # 1. 收尾已完成的 asyncio.Task
        for task_id, t in list(self._tasks.items()):
            if t.done():
                self._tasks.pop(task_id)
                await self._finalize(task_id, t)
                self.active_task_id = None
        # 2. 启动下一个 queued 任务（单并发）
        if any(not t.done() for t in self._tasks.values()):
            return
        task = self._next_queued()
        if task is not None:
            self.active_task_id = task["id"]
            await self._start(task)

    def shutdown(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        self._tasks.clear()
        self.active_task_id = None

    # ---- 内部 ----
    def _snapshot_task(self, task_id: str) -> dict | None:
        return next((t for t in self.store.snapshot()["evaluations"] if t["id"] == task_id), None)

    def _next_queued(self) -> dict | None:
        return next((t for t in self.store.snapshot()["evaluations"]
                     if t["status"] == "queued" and t["benchmark"] in AGENTCOMPASS_IDS), None)

    def _benchmark_label(self, task: dict) -> str:
        return (task.get("agent") or {}).get("benchmark") or task.get("benchmark", "")

    async def _start(self, task: dict) -> None:
        from agentcompass.launcher import async_run_evaluation  # 延迟 import（依赖就绪后才调用）

        task_id = task["id"]
        self._mark(task_id, status="running",
                   log=f"{now()} 开始执行 {self._benchmark_label(task)} 真实评测")
        try:
            model = self.registry.get_model(task["model_id"])
            endpoint = self.registry.next_endpoint(model)
            api_key = self.registry.api_key(endpoint)
        except Exception as exc:  # noqa: BLE001 - 端点不可用回退失败
            self._fail(task_id, f"模型端点不可用：{exc}")
            return
        kwargs = build_run_kwargs(task, model, endpoint, api_key,
                                  data_dir=self.data_dir, results_dir=self.results_dir)
        kwargs["on_progress"] = lambda ev: self._on_progress(task_id, ev)

        async def _run():
            return await async_run_evaluation(**kwargs)

        self._tasks[task_id] = asyncio.create_task(_run())

    def _on_progress(self, task_id: str, event: Any) -> None:
        if event.run_id:
            self._runs[task_id] = event.run_id
        if event.event == "tasks_loaded":
            self._totals[task_id] = int(event.payload.get("total_tasks", 0) or 0)
            self._append_log(task_id, f"已加载 {self._totals[task_id]} 条任务")
        elif event.event == "task_finished":
            self._finished[task_id] = self._finished.get(task_id, 0) + 1
            total = self._totals.get(task_id, 0)
            if total:
                self._set_progress(task_id, round(self._finished[task_id] / total * 100))
            self._append_log(task_id, f"任务 {event.task_id} 完成（{event.status}）")
        elif event.event == "run_finished":
            self._append_log(task_id, f"评测结束（{event.status}）")

    def _find_run_dir(self, task: dict) -> Path | None:
        run_id = self._runs.get(task["id"])
        benchmark = (task.get("agent") or {}).get("benchmark") or ""
        bench_dir = Path(self.results_dir) / benchmark
        if not run_id or not bench_dir.is_dir():
            return None
        for model_dir in bench_dir.iterdir():
            if not model_dir.is_dir():
                continue
            exact = model_dir / run_id
            if exact.is_dir():
                return exact
            for child in model_dir.iterdir():
                if child.is_dir() and run_id in child.name:
                    return child
        return None

    async def _finalize(self, task_id: str, t: asyncio.Future) -> None:
        try:
            run_result = t.result()
        except asyncio.CancelledError:
            self._mark(task_id, log=f"{now()} 评测已取消")
            self._cleanup(task_id)
            return
        except Exception as exc:  # noqa: BLE001 - 引擎异常标记 failed
            self._fail(task_id, f"评测执行失败：{exc}")
            self._cleanup(task_id)
            return
        task = self._snapshot_task(task_id)
        if task is None:
            self._cleanup(task_id)
            return
        run_dir = self._find_run_dir(task)
        report, result = map_run_to_report(task, run_dir, run_result)
        self._finish(task_id, report, result)
        self._cleanup(task_id)

    def _cleanup(self, task_id: str) -> None:
        self._runs.pop(task_id, None)
        self._totals.pop(task_id, None)
        self._finished.pop(task_id, None)

    # ---- store 写操作 ----
    def _mark(self, task_id: str, *, status: str | None = None, log: str) -> None:
        def op(data):
            t = next((x for x in data["evaluations"] if x["id"] == task_id), None)
            if t:
                if status:
                    t["status"] = status
                t["updated_at"] = now()
                t["logs"].append(log)
        self.store.update(op)

    def _append_log(self, task_id: str, log: str) -> None:
        self._mark(task_id, log=log)

    def _set_progress(self, task_id: str, progress: int) -> None:
        def op(data):
            t = next((x for x in data["evaluations"] if x["id"] == task_id), None)
            if t:
                t["progress"] = progress
                t["updated_at"] = now()
        self.store.update(op)

    def _fail(self, task_id: str, message: str) -> None:
        def op(data):
            t = next((x for x in data["evaluations"] if x["id"] == task_id), None)
            if t and t["status"] not in ("completed", "cancelled"):
                t["status"] = "failed"
                t["updated_at"] = now()
                t["logs"].append(f"{now()} {message}")
        self.store.update(op)

    def _finish(self, task_id: str, report: dict, result: dict) -> None:
        def op(data):
            t = next((x for x in data["evaluations"] if x["id"] == task_id), None)
            if not t or t["status"] == "cancelled":
                return
            t["status"] = "completed"
            t["progress"] = 100
            t["updated_at"] = now()
            t["result"] = result
            t["report_id"] = report["id"]
            t["logs"].append(f"{now()} 评测完成：{result.get('summary', '')}")
            data["reports"].insert(0, report)
        self.store.update(op)
