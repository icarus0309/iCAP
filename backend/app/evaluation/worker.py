"""评测任务执行器：EvaluationRunner 持有内存 session，逐用例推进任务状态机。

职责边界：报告生成、report_id 写入、models.scores 回写全部发生在 worker 内部的
一次 store.update 事务中；main.py 的 advance_jobs 不再生成评测报告。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from .cases import get_cases_for_ruler
from .client import EndpointUnavailableError, demo_answer, live_answer
from .engine import build_result, flatten_cases, run_cases
from .specs import get_ruler_spec


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class _Session:
    """内存执行会话：缓存当前任务的用例列表、已得结果与答题函数。

    说明：`sample_limit` 仅记录任务装配时的取样参数（调试/日志辅助），
    采样本身在 `_setup_session` 调用 `flatten_cases(..., limit)` 时已生效。
    """

    def __init__(self, spec, cases, answer_fn, sample_limit):
        self.spec = spec
        self.cases = cases
        self.answer_fn = answer_fn
        self.sample_limit = sample_limit
        self.case_results: list[dict] = []


class EvaluationRunner:
    """单 worker 串行执行器；active_task_id 防重入，session 支持暂停续跑。"""

    def __init__(self, store, registry, *, live: bool, stop: asyncio.Event | None = None):
        self.store = store
        self.registry = registry
        self.live = live
        self.stop = stop or asyncio.Event()   # 全局停止信号：应用关闭时置位
        self.active_task_id: str | None = None
        self._sessions: dict[str, _Session] = {}

    def snapshot_task(self, task_id: str) -> dict | None:
        data = self.store.snapshot()
        return next((t for t in data["evaluations"] if t["id"] == task_id), None)

    # ---- 公开入口：advance_jobs 每 tick 调用一次 ----
    async def step(self) -> None:
        """调度一回合：取首个 queued/running 评测任务并推进一个用例。"""
        selected = self.store.update(self._select_task)
        if selected is None:
            # 无活跃任务：清理 session
            self._sessions.clear()
            self.active_task_id = None
            return
        task = selected
        self.active_task_id = task["id"]

        if task["status"] == "queued":
            self._sessions.pop(task["id"], None)
            self._mark(task["id"], status="running",
                       log=f"{now()} 开始执行 {task.get('ruler_label', task['benchmark'])}，任务初始化")
            task = self.snapshot_task(task["id"])
            if task is None:                       # 任务在推进过程中被删除
                self.active_task_id = None
                return

        session = self._sessions.get(task["id"])
        if session is None:
            session = await self._setup_session(task)   # async：内部组装 answer_fn
            if session is None:                         # 装配失败已置 failed，不缓存空会话
                return
            self._sessions[task["id"]] = session

        if task["status"] == "cancelled":
            self._sessions.pop(task["id"], None)
            self.active_task_id = None
            return
        if task["status"] == "paused":
            return                                        # 原地等待 resume，不动 session

        await self._advance_one_case(task, session)       # 推进一个用例（内部原子持久化）

    # app 关闭时调用，清理内存态
    def shutdown(self) -> None:
        self._sessions.clear()
        self.active_task_id = None

    # ---- 私有实现 ----
    def _select_task(self, data: dict) -> dict | None:
        """防重入 + 重启续跑：选出唯一应推进的任务。

        - 优先第一个 status in (queued, running)；无则返回 None；
        - running 但无内存 session（进程重启）→ 回写 queued、progress=0，并追加日志；
        - running 且已有 session 但与 active_task_id 不符（并发推进冲突，单 worker 不可达）
          → 回写 queued 并追加日志（防御分支，避免双前进）。
        """
        candidates = [t for t in data["evaluations"] if t["status"] in ("queued", "running")]
        if not candidates:
            return None
        task = candidates[0]
        if task["status"] == "running":
            if task["id"] not in self._sessions:
                task["status"] = "queued"
                task["progress"] = 0
                task["logs"].append(f"{now()} 进程重启，任务重新排队（重新开始执行）")
            elif self.active_task_id is not None and task["id"] != self.active_task_id:
                task["status"] = "queued"
                task["progress"] = 0
                task["logs"].append(f"{now()} 检测到并发推进冲突，任务重置排队")
        return task

    async def _setup_session(self, task: dict):
        try:
            spec = get_ruler_spec(task["benchmark"])
        except ValueError as exc:
            self._fail(task["id"], f"不支持的尺子：{exc}")
            return None
        try:
            dim_cases = get_cases_for_ruler(spec.id)
            config = task.get("config") or {}
            sample_limit = config.get("sample_limit", spec.default_sample_limit)
            # 逐维度取样：任务 config 显式给出 sample_limits 时用它，否则回落统一 sample_limit。
            # 未列出的维度按 sample_limit 取样；条数超过实际样例数时按实际条数执行。
            raw_limits = config.get("sample_limits") or None
            if raw_limits:
                limits = {d["key"]: int(raw_limits.get(d["key"], sample_limit)) for d in spec.dimensions}
                cases = flatten_cases(dim_cases, limits)
            else:
                cases = flatten_cases(dim_cases, sample_limit)
            self._mark(task["id"], log=f"{now()} 共 {len(cases)} 条用例待执行（{spec.label}）")
        except Exception as exc:                      # 样例装配异常视为失败
            self._fail(task["id"], f"组装用例失败：{exc}")
            return None
        answer_fn = await self._make_answer_fn(task)  # async：live 模式需 await
        return _Session(spec, cases, answer_fn, sample_limit)

    async def _make_answer_fn(self, task: dict):
        if not self.live:
            return demo_answer
        config = task.get("config") or {}
        try:
            model = self.registry.get_model(task["model_id"])
            fn = live_answer(model,
                             api_key_of=self.registry.api_key,
                             endpoint_of=self.registry.next_endpoint,
                             params=config)
            if fn is None:
                raise EndpointUnavailableError("模型未配置 API 端点")
            return fn
        except Exception as exc:                      # 端点/密钥缺失：回退演示作答并留痕
            self._mark(task["id"], log=f"{now()} 模型端点不可用，回退演示作答：{exc}")
            return demo_answer

    async def _advance_one_case(self, task: dict, session: _Session) -> None:
        """推进当前任务的下一个未完成用例；完成时收尾（报告/回写/终态）。"""
        i = len(session.case_results)
        if i >= len(session.cases):
            self._finish(task, session)               # 全部完成
            return
        dim, case = session.cases[i]
        task_id = task["id"]

        def is_stop():
            """停止条件：任务被取消，或全局 stop 已置位（应用关闭）。

            该回调同时被 run_cases 用于退出暂停等待循环，避免停止时卡死。
            """
            if self.stop.is_set():
                return True
            t = self.snapshot_task(task_id)
            return bool(t) and t["status"] == "cancelled"

        def is_pause():
            t = self.snapshot_task(task_id)
            return bool(t) and t["status"] == "paused" and not self.stop.is_set()

        try:
            answers = await run_cases(
                [(dim, case)], session.answer_fn,
                should_stop=is_stop, should_pause=is_pause,
            )
        except EndpointUnavailableError as exc:
            self._fail(task_id, f"执行失败：{exc}")
            return
        except asyncio.CancelledError:
            raise                                  # wait_for 兜底超时取消：向上传递，交由 lifespan 收尾
        if not answers:                            # should_stop 提前 break（取消或关闭）
            if self.stop.is_set():
                self._mark(task_id, log=f"{now()} 应用关闭，评测任务挂起（可于下次启动后重排续跑）")
            else:
                self._mark(task_id, log=f"{now()} 任务已取消")
            self._sessions.pop(task_id, None)
            self.active_task_id = None
            return
        session.case_results.append(answers[0])
        self._persist_progress(task, session)          # 原子写 progress/logs
        if len(session.case_results) >= len(session.cases):
            self._finish(task, session)

    def _persist_progress(self, task: dict, session: _Session) -> None:
        done = len(session.case_results)
        total = len(session.cases)
        progress = round(done / total * 100)
        last = session.case_results[-1]

        def op(data):
            t = next((x for x in data["evaluations"] if x["id"] == task["id"]), None)
            if not t:
                return
            t["progress"] = progress
            t["updated_at"] = now()
            t["logs"].append(f"{now()} 维度[{last['dimension']}] 用例 {done}/{total} 判分："
                             f"{last['verdict']}（{last['points']}分）")
        self.store.update(op)

    def _finish(self, task: dict, session: _Session) -> None:
        """终态收尾：同一事务写 result + 生成报告 + 可选 scores 回写 + completed。"""
        result = build_result(session.spec, session.case_results)
        result["mode"] = "live" if self.live else "demo"
        result["finished_at"] = now()
        report = {
            "id": _uid("report"),
            "evaluation_id": task["id"],
            "title": f"{task['benchmark']} · {task['model_name']}",
            "score": result["total"],
            "ruler_id": session.spec.id,
            "dimensions": [dict(d) for d in result["dimensions"]],
            "summary": result["summary"],
            "created_at": now(),
            "published": False,
            "simulated": not self.live,
        }
        config = task.get("config") or {}

        def op(data):
            t = next((x for x in data["evaluations"] if x["id"] == task["id"]), None)
            if not t or t["status"] == "cancelled":
                return
            t["status"] = "completed"
            t["progress"] = 100
            t["updated_at"] = now()
            t["result"] = result
            t["report_id"] = report["id"]
            t["logs"].append(f"{now()} 测评完成：综合分 {result['total']}（{result['summary']}）")
            data["reports"].insert(0, report)
            # ---- sync_scores 回写：仅 Ruler1 生效，Ruler2 不写模型 scores ----
            if config.get("sync_scores") and session.spec.id == "Ruler1":
                for m in data["models"]:
                    if m["id"] == t["model_id"]:
                        m["scores"] = [d["score"] for d in result["dimensions"]]
                        break
        self.store.update(op)
        self._sessions.pop(task["id"], None)
        self.active_task_id = None

    def _mark(self, task_id: str, *, status: str | None = None, log: str) -> None:
        def op(data):
            t = next((x for x in data["evaluations"] if x["id"] == task_id), None)
            if t:
                if status:
                    t["status"] = status
                t["updated_at"] = now()
                t["logs"].append(log)
        self.store.update(op)

    def _fail(self, task_id: str, message: str) -> None:
        def op(data):
            t = next((x for x in data["evaluations"] if x["id"] == task_id), None)
            if t and t["status"] not in ("completed", "cancelled"):
                t["status"] = "failed"
                t["updated_at"] = now()
                t["logs"].append(f"{now()} {message}")
        self.store.update(op)
        self._sessions.pop(task_id, None)
        self.active_task_id = None
