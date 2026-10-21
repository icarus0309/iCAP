from __future__ import annotations

import copy
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from dotenv import dotenv_values
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from ..access import ownership, require_edit, require_view, visible
from .schemas import JobInput

BACKEND = Path(__file__).resolve().parents[2]
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resource_name(name):
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or not p.parts or any(x in {"..", "."} for x in p.parts) or ":" in name:
        raise HTTPException(422, "资源文件名须为相对路径，不能包含 .. 或盘符")
    if p.suffix.lower() not in {".md", ".txt", ".json", ".csv"} or any(x.startswith(".") for x in p.parts):
        raise HTTPException(422, "资源只支持普通 md、txt、json、csv 文件")
    return str(p)


def redact(text, secrets=()):
    text = str(text)
    for value in secrets:
        if value:
            text = text.replace(value, "[密钥已隐藏]")
    return re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[密钥已隐藏]", text)


def stop_process(proc):
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        pass


class SkillService:
    def __init__(self, store, registry):
        self.store, self.registry = store, registry
        self.root = store.base / "skill-evolution"
        self.root.mkdir(exist_ok=True, mode=0o700)
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.processes = {}
        self.thread = None

    def default_model(self):
        # Credentials belong to the server, never to a task record or API response.
        private = dotenv_values(self.store.base / "skill-evolution.env")
        def value(name, default=""):
            return os.getenv(name) or private.get(name) or default
        return {"id": "platform-default", "name": value("SKILL_EVOLVE_MODEL", "qwen3.8-27b"),
                "endpoints": [{"url": value("SKILL_EVOLVE_API_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                               "api_key": value("SKILL_EVOLVE_API_KEY"), "thinking_schema": {}}]}

    def capabilities(self, principal):
        default = self.default_model()
        models = [{"id": default["id"], "name": default["name"], "provider": "平台专用 · 阿里云",
                   "configured": bool(default["endpoints"][0]["api_key"])}]
        try:
            models += [{"id": m["id"], "name": m["name"], "provider": m["provider"], "configured": True}
                       for m in self.registry.list_models(principal)]
        except HTTPException:
            pass
        runtime = Path(os.getenv("ICAP_HERMES_RUNTIME", "/opt/icap-hermes"))
        hermes = bool(os.name == "posix" and os.getenv("SKILL_EVOLVE_HERMES_ENABLED") == "true"
                      and (runtime / "READY").is_file() and (runtime / "venv/bin/hermes").is_file()
                      and shutil.which("bwrap"))
        return {"models": models, "execution_modes": ["direct"] + (["hermes"] if hermes else []),
                "upstream_commit": "61f8870e61c37f59781178f49247f7581aeddba7",
                "engines": [{"id": "opt", "name": "A · 业务推荐", "features": ["结构化编辑", "成功／失败分组", "拒绝经验"]},
                            {"id": "enhanced", "name": "B · 复杂场景进阶", "features": ["结构化编辑", "大模型对照与知识提炼", "经验池", "停滞探索"]}]}

    def resolve_model(self, identifier, principal):
        if identifier == "platform-default":
            model = self.default_model()
        else:
            registered = self.registry.get_model(identifier, principal)
            model = {"id": identifier, "name": registered["id"], "endpoints": []}
            for ep in registered.get("endpoints", []):
                model["endpoints"].append({"url": ep["url"], "api_key": self.registry.api_key(ep),
                                            "thinking_schema": ep.get("thinking_schema", {})})
        if not model["endpoints"] or not all(e.get("api_key") for e in model["endpoints"]):
            raise HTTPException(503, "所选模型尚未配置可用的后端凭据")
        return model

    def get(self, identifier):
        row = next((r for r in self.store.snapshot().get("skill_jobs", []) if r["id"] == identifier), None)
        if row is None:
            raise HTTPException(404, "任务不存在")
        return row

    def update(self, identifier, **changes):
        def change(data):
            row = next(r for r in data["skill_jobs"] if r["id"] == identifier)
            row.update(changes, updated_at=now())
            return row
        return self.store.update(change)

    @staticmethod
    def public(row, detail=False):
        value = {k: v for k, v in row.items() if k not in {"config", "principal"}}
        if not detail:
            value.pop("logs", None)
        else:
            cfg = row["config"]
            value["config"] = {k: v for k, v in cfg.items() if k not in {"dataset", "resources"}}
        return value

    def prepare(self, body):
        cfg = body.model_dump()
        assets = {resource_name(r.name): r.content for r in body.resources}
        if set(assets) & {"SKILL.md", "dataset.json", "rubric.md"}:
            raise HTTPException(422, "SKILL.md、dataset.json 和 rubric.md 是保留文件名，请为附属资源使用其他名称")
        if len(assets) != len(body.resources):
            raise HTTPException(422, "资源文件名不能重复")
        cfg["resources"] = [{"name": k, "content": v} for k, v in assets.items()]
        if body.operation == "bootstrap":
            return cfg
        dataset = copy.deepcopy(body.dataset)
        ids = set()
        for index, item in enumerate(dataset):
            cid = str(item.get("id", index + 1))
            if not re.fullmatch(r"[\w-]{1,80}", cid) or cid in ids:
                raise HTTPException(422, "用例 ID 必须唯一，且只包含文字、数字、下划线或连字符")
            ids.add(cid)
            item["id"] = cid
            item.setdefault("type", "custom")
            item.setdefault("sub_type", "通用")
            item.setdefault("key_steps", [])
            messages = []
            if not isinstance(item.get("input"), list):
                raise HTTPException(422, f"用例 {cid} 的 input 必须是数组")
            for entry in item.get("input", []):
                if not isinstance(entry, dict):
                    raise HTTPException(422, f"用例 {cid} 的 input 条目必须是对象")
                kind, data = entry.get("type"), entry.get("data")
                if kind == "text" and isinstance(data, str):
                    messages.append({"role": "user", "content": data})
                elif kind in {"json", "list"}:
                    try:
                        parsed = json.loads(data) if isinstance(data, str) else data
                    except ValueError:
                        raise HTTPException(422, f"用例 {cid} 的消息 JSON 无法解析")
                    if not isinstance(parsed, list):
                        raise HTTPException(422, f"用例 {cid} 的消息须为数组")
                    messages.extend(parsed)
            if not messages or any(not isinstance(m, dict) or m.get("role") not in {"system", "developer", "user", "assistant"}
                                   or not isinstance(m.get("content"), str) for m in messages):
                raise HTTPException(422, f"用例 {cid} 需要有效的纯文本消息数组")
            # Both upstream engines can consume this representation without losing text inputs.
            item["input"] = [{"type": "json", "data": json.dumps(messages, ensure_ascii=False)}]
            outputs = item.get("output") or [{"type": "onlyllm", "data": "rubric.md"}]
            if not isinstance(outputs, list) or not outputs:
                raise HTTPException(422, f"用例 {cid} 的 output 须为数组")
            for output in outputs:
                if not isinstance(output, dict):
                    raise HTTPException(422, f"用例 {cid} 的 output 条目必须是对象")
                kind = output.get("type")
                if kind not in {"onlyllm", "word", "format", "text", "file", "tool", "device_state"}:
                    raise HTTPException(422, f"用例 {cid} 含不支持的结果类型")
                if kind == "device_state":
                    raise HTTPException(422, "当前未配置设备服务，不能校验真实设备状态；Hermes 可执行终端和文件工具")
                if body.execution_mode == "direct" and kind == "tool":
                    raise HTTPException(422, "单 API 模式无法校验真实设备状态，请使用文本、关键词、JSON 格式或 Rubric 判据")
                if kind == "onlyllm":
                    name = output.get("data") or "rubric.md"
                    if not isinstance(name, str) or (name != "rubric.md" and name not in assets):
                        raise HTTPException(422, f"用例 {cid} 的 Rubric 文件未上传，可改为 rubric.md 使用页面评审标准")
                    if name == "rubric.md" and not body.rubric.strip():
                        raise HTTPException(422, "请填写非空评审标准")
                    output["data"] = name
                if kind == "format" and not output.get("data") and not body.format_example.strip():
                    raise HTTPException(422, f"用例 {cid} 缺少 JSON 格式示例")
                if kind == "format":
                    example = output.get("data") or body.format_example
                    try:
                        if isinstance(example, str):
                            example = json.loads(example)
                        if not isinstance(example, (dict, list)):
                            raise ValueError()
                    except ValueError:
                        raise HTTPException(422, f"用例 {cid} 的格式示例必须是 JSON 对象或数组")
                if kind == "file":
                    data = output.get("data")
                    name = output.get("expect_path") or (data.get("expect_path") if isinstance(data, dict) else None)
                    if name not in assets:
                        raise HTTPException(422, f"用例 {cid} 的期望文件未作为附属资源上传")
                    # Upstream reads these fields separately from data. Never allow
                    # user-supplied host paths to reach either engine.
                    actual = output.get("actual_path") or (data.get("actual_path") if isinstance(data, dict) else None) or "result.txt"
                    output.update(data="", expect_path=name, actual_path=resource_name(actual) if body.execution_mode == "hermes" else "")
            if not any(body.output_weights.get(o["type"], 0) > 0 for o in outputs):
                raise HTTPException(422, f"用例 {cid} 没有权重大于 0 的结果判据")
            item["output"] = outputs
            item["output_weights"] = cfg["output_weights"]
            # No arbitrary filesystem or environment hooks are accepted from a dataset.
            item.pop("pre_condition", None)
        cfg["dataset"] = dataset
        return cfg

    def create(self, body, principal):
        cfg = self.prepare(body)
        caps = self.capabilities(principal)
        if body.execution_mode not in caps["execution_modes"]:
            raise HTTPException(422, "服务器尚未启用 Hermes 执行环境，请选择单 API 执行")
        for role in (["gen"] if body.operation == "bootstrap" else ["exec", "eval", "gen"]):
            self.resolve_model(cfg[f"{role}_model_id"], principal)
        if body.large_model_id:
            self.resolve_model(body.large_model_id, principal)
        with self.lock:
            active = [r for r in self.store.snapshot().get("skill_jobs", []) if r["status"] not in TERMINAL]
            if len(active) >= 20 or sum(r["owner_user_id"] == principal["id"] for r in active) >= 3:
                raise HTTPException(429, "待执行任务过多，请等待当前任务完成")
            row = {"id": "skill-" + uuid.uuid4().hex[:16], "name": body.name.strip(), "engine": body.engine,
                   "operation": body.operation, "execution_mode": body.execution_mode, "status": "queued", "progress": 0,
                   "created_at": now(), "updated_at": now(), "logs": [], "result": None, "error": "",
                   "current_round": 0, "model_calls": 0, "case_count": min(len(body.dataset), body.cases_limit or 200),
                   "config": cfg, "principal": {"id": principal["id"], "role": principal["role"]},
                   **ownership(principal, body.visibility)}
            directory = self.root / row["id"]
            try:
                directory.mkdir(mode=0o700)
            except OSError:
                raise HTTPException(503, "任务存储目录不可写，请联系管理员检查数据目录权限") from None
            try:
                self.store.update(lambda data: data.setdefault("skill_jobs", []).insert(0, row))
            except Exception:
                directory.rmdir()
                raise
        return self.public(row, True)

    def start(self):
        def recover(data):
            for row in data.get("skill_jobs", []):
                if row["status"] == "running":
                    row.update(status="interrupted", error="服务重启中断了任务，可从已有产物续跑", updated_at=now())
        self.store.update(recover)
        self.thread = threading.Thread(target=self.loop, name="skill-evolution-scheduler", daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        with self.lock:
            for proc in list(self.processes.values()):
                if proc.poll() is None:
                    stop_process(proc)
        if self.thread:
            self.thread.join(timeout=8)

    def loop(self):
        while not self.stop.is_set():
            queued = next((r for r in reversed(self.store.snapshot().get("skill_jobs", [])) if r["status"] == "queued"), None)
            if queued:
                try:
                    self.run(queued)
                except Exception:
                    if self.get(queued["id"])["status"] not in TERMINAL:
                        self.update(queued["id"], status="failed", error="任务调度失败，请检查模型配置后重试")
            else:
                self.stop.wait(0.5)

    def run(self, row):
        identifier = row["id"]
        cfg = row["config"]
        models = {}
        try:
            for role in (["gen"] if row["operation"] == "bootstrap" else ["exec", "eval", "gen"]):
                models[role] = self.resolve_model(cfg[f"{role}_model_id"], row["principal"])
            if cfg.get("large_model_id"):
                models["large"] = self.resolve_model(cfg["large_model_id"], row["principal"])
        except HTTPException as exc:
            self.update(identifier, status="failed", error=exc.detail)
            return
        secret_values = [ep.get("api_key") for m in models.values() for ep in m["endpoints"]]
        env = {k: v for k, v in os.environ.items() if k in {
            "PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
        env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        # Explicit server-owned Hermes configuration only; a task cannot inject shell/environment settings.
        if row["execution_mode"] == "hermes":
            for key in ("ICAP_HERMES_RUNTIME",):
                if key in os.environ:
                    env[key] = os.environ[key]
        with self.lock:
            if self.get(identifier)["status"] != "queued":
                return
            self.update(identifier, status="running", error="", started_at=now())
            proc = subprocess.Popen([sys.executable, "-u", "-m", "app.skill_evolution.worker"], cwd=BACKEND,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    env=env, text=True, encoding="utf-8", errors="replace",
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                    start_new_session=os.name == "posix")
            self.processes[identifier] = proc
        deadline = time.monotonic() + 6 * 3600
        watchdog_done = threading.Event()
        def watchdog():
            while not watchdog_done.wait(1):
                if self.stop.is_set() or time.monotonic() > deadline or self.get(identifier)["status"] == "cancelled":
                    if proc.poll() is None:
                        stop_process(proc)
                    return
        threading.Thread(target=watchdog, daemon=True).start()
        try:
            proc.stdin.write(json.dumps({"config": cfg, "models": models, "job_dir": str((self.root / identifier).resolve()),
                                         "resume": row.get("resume", False), "initial_model_calls": row.get("model_calls", 0)}, ensure_ascii=False) + "\n")
            proc.stdin.close()
            logs = list(row.get("logs", []))
            last_save = 0
            for raw in proc.stdout:
                line = redact(raw.strip(), secret_values)
                if line.startswith("ICAP_EVENT:"):
                    try:
                        event = json.loads(line[len("ICAP_EVENT:"):])
                    except ValueError:
                        continue
                    allowed = {k: v for k, v in event.items() if k in {"progress", "current_round", "model_calls", "phase"}}
                    if allowed:
                        self.update(identifier, **allowed)
                elif line:
                    logs.append(line[:2000])
                    logs = logs[-300:]
                if time.monotonic() - last_save > 1:
                    self.update(identifier, logs=logs)
                    last_save = time.monotonic()
            proc.wait(timeout=10)
            self.update(identifier, logs=logs)
            if self.get(identifier)["status"] == "cancelled":
                return
            if self.stop.is_set():
                self.update(identifier, status="interrupted", error="服务重启中断了任务，可续跑")
                return
            report = self.root / identifier / "report.json"
            if proc.returncode == 0 and report.is_file():
                result = json.loads(report.read_text(encoding="utf-8"))
                summary = {k: result.get(k) for k in ("best_score", "initial_score", "final_score", "model_calls", "artifacts")}
                self.update(identifier, status="completed", progress=100, result=summary, completed_at=now())
            else:
                error_file = self.root / identifier / "error.txt"
                error = error_file.read_text(encoding="utf-8") if error_file.exists() else "执行进程未正常完成（可能超时），可从已有产物续跑"
                self.update(identifier, status="failed", error=redact(error, secret_values)[:1200])
        finally:
            watchdog_done.set()
            if proc.poll() is None:
                proc.kill()
            with self.lock:
                self.processes.pop(identifier, None)

    def action(self, identifier, action, principal):
        with self.lock:
            row = require_edit(self.get(identifier), principal)
            if action == "cancel":
                if row["status"] not in {"queued", "running"}:
                    raise HTTPException(409, "任务当前不可取消")
                self.update(identifier, status="cancelled", error="用户取消了任务")
                proc = self.processes.get(identifier)
                if proc and proc.poll() is None:
                    stop_process(proc)
            elif action == "resume":
                if row["status"] not in {"failed", "interrupted", "cancelled"} or identifier in self.processes:
                    raise HTTPException(409, "任务尚未停止，或当前不可续跑")
                self.update(identifier, status="queued", resume=True, error="")
            else:
                raise HTTPException(422, "未知操作")
        return self.public(self.get(identifier), True)

    def router(self, authorized):
        router = APIRouter(prefix="/api/skill-evolution", tags=["Skill evolution"])

        @router.get("/capabilities")
        def capabilities(principal=Depends(authorized)):
            return self.capabilities(principal)

        @router.get("/jobs")
        def jobs(principal=Depends(authorized)):
            return [self.public(r) for r in visible(self.store.snapshot().get("skill_jobs", []), principal)]

        @router.post("/jobs", status_code=201)
        def create(body: JobInput, principal=Depends(authorized)):
            return self.create(body, principal)

        @router.get("/jobs/{identifier}")
        def job(identifier: str, principal=Depends(authorized)):
            row = require_view(self.get(identifier), principal)
            result = self.public(row, True)
            live = self.root / identifier / ("report.json" if row["status"] == "completed" else "live.json")
            if live.is_file():
                try:
                    result["result"] = json.loads(live.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            return result

        @router.post("/jobs/{identifier}/{action}")
        def action(identifier: str, action: str, principal=Depends(authorized)):
            return self.action(identifier, action, principal)

        @router.get("/jobs/{identifier}/artifacts/{name}")
        def artifact(identifier: str, name: str, principal=Depends(authorized)):
            require_view(self.get(identifier), principal)
            if name not in {"optimized_skill.md", "final_skill.md", "initial_skill.md", "report.json", "artifacts.zip"}:
                raise HTTPException(404, "产物不存在")
            path = self.root / identifier / name
            if not path.is_file():
                raise HTTPException(404, "产物尚未生成")
            return FileResponse(path, filename=name)

        return router
