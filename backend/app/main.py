"""InnovationCore: one FastAPI entry point with replaceable business adapters."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .access import Visibility, can_view, ownership, require_edit, require_view, visible
from .auth import AuthService, source_ip
from .model_registry import ModelInput, ModelRegistry
from .store import Store

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def find(items: list[dict], identifier: str) -> dict:
    match = next((item for item in items if item["id"] == identifier), None)
    if match is None:
        raise HTTPException(404, "记录不存在")
    return match


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class SecurityQuestionInput(BaseModel):
    question: str = Field(min_length=1, max_length=120)
    answer: str = Field(min_length=1, max_length=128)


class Register(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    phone: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    security_questions: list[SecurityQuestionInput] = Field(min_length=3, max_length=3)


class SecurityQuestionsInput(BaseModel):
    security_questions: list[SecurityQuestionInput] = Field(min_length=3, max_length=3)


class SecurityAnswerInput(BaseModel):
    index: int = Field(ge=0, le=2)
    answer: str = Field(min_length=1, max_length=128)


class PasswordReset(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    new_password: str = Field(min_length=8, max_length=128)
    answers: list[SecurityAnswerInput] = Field(min_length=2, max_length=3)


class PasswordChange(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)
    answers: list[SecurityAnswerInput] = Field(min_length=2, max_length=3)


class DatasetInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    category: str = Field(default="通用", max_length=40)
    version: str = Field(default="v1.0", max_length=30)
    description: str = Field(default="", max_length=500)
    visibility: Visibility = "private"


class EvalInput(BaseModel):
    model_id: str
    dataset_id: str
    benchmark: Literal["Ruler1", "Ruler2", "VLM", "AISF", "Dev", "ASR", "MT", "TTS"] = "Ruler1"
    visibility: Visibility = "private"


class BatchEvalInput(BaseModel):
    model_ids: list[str] = Field(min_length=1, max_length=4)
    dataset_id: str
    benchmark: Literal["Ruler1", "Ruler2", "VLM", "AISF", "Dev", "ASR", "MT", "TTS"] = "Ruler1"
    visibility: Visibility = "private"


class AnnotationInput(BaseModel):
    dataset_id: str
    content: str = Field(min_length=1, max_length=8000)
    label: str = Field(min_length=1, max_length=80)
    visibility: Visibility = "private"


class DeploymentInput(BaseModel):
    model_id: str
    reason: str = Field(min_length=3, max_length=500)
    hardware: str = Field(default="910B", max_length=60)
    visibility: Visibility = "private"


class Decision(BaseModel):
    action: Literal["approve", "reject"]


class TaskAction(BaseModel):
    action: Literal["pause", "resume", "cancel"]


class SchoolJobInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    accelerator: Literal["NPU", "GPU"] = "NPU"
    script: str = Field(min_length=1, max_length=20000)
    visibility: Visibility = "private"


class InferenceInput(BaseModel):
    model_id: str
    prompt: str = Field(min_length=1, max_length=8000)
    zone: Literal["黄区", "绿区"] = "黄区"


class ChatInput(BaseModel):
    model_id: str
    message: str = Field(min_length=1, max_length=8000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=20)
    thinking: bool = False
    top_k: int | None = Field(default=None, ge=0, le=1000)
    top_p: float | None = Field(default=None, ge=0, le=1)
    temperature: float | None = Field(default=None, ge=0, le=2)


class ToolInput(BaseModel):
    content: str = Field(min_length=1, max_length=10000)


async def advance_jobs(store: Store, stop: asyncio.Event) -> None:
    """A visible demo executor. No user-submitted code is ever executed."""
    while not stop.is_set():
        def tick(data: dict) -> None:
            for group in ("evaluations", "school_jobs"):
                for task in data[group]:
                    if task["status"] not in ("queued", "running"):
                        continue
                    if task["status"] == "queued":
                        task["status"] = "running"
                        task["logs"].append(f"{now()} 开始模拟执行")
                    task["progress"] = min(100, task["progress"] + 10)
                    task["updated_at"] = now()
                    if task["progress"] % 20 == 0:
                        task["logs"].append(f"{now()} 模拟任务进度 {task['progress']}%")
                    if task["progress"] == 100:
                        task["status"] = "completed"
                        task["logs"].append(f"{now()} 模拟任务完成")
                        if group == "evaluations":
                            model = next((m for m in data["models"] if m["id"] == task["model_id"]), None)
                            scores = model.get("scores", []) if model else []
                            score = round(sum(scores) / len(scores), 1) if scores else 0
                            report = dict(id=uid("report"), evaluation_id=task["id"], title=f"{task['benchmark']} · {task['model_name']}",
                                          score=score, created_at=now(), published=False, simulated=True,
                                          owner_user_id=task.get("owner_user_id"),
                                          visibility=task.get("visibility", "public"),
                                          summary=f"演示评测报告：{task['model_name']} 在 {task['dataset_name']} 上得到示例分数 {score}。"
                                                  "未调用真实测评脚本，不能作为模型能力结论。")
                            data["reports"].insert(0, report)
                            task["report_id"] = report["id"]
        store.update(tick)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.3)
        except asyncio.TimeoutError:
            pass


def create_app(data_dir: str | Path | None = None, demo: bool | None = None) -> FastAPI:
    demo = (os.getenv("DEMO_MODE", "true").lower() == "true") if demo is None else demo
    admin_user = os.getenv("INNOVATION_ADMIN_USER", "admin" if demo else "")
    admin_password = os.getenv("INNOVATION_ADMIN_PASSWORD", "demo1234" if demo else "")
    secret = os.getenv("INNOVATION_SECRET_KEY", "demo-only-secret-please-change-in-production" if demo else "")
    if not demo and (not admin_user or not admin_password or len(secret) < 32):
        raise RuntimeError("生产模式必须设置管理员账号、密码和至少 32 字符的签名密钥")
    base = Path(data_dir or os.getenv("INNOVATION_DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
    store = Store(base)
    auth = AuthService(base, secret, admin_user, admin_password, demo)
    registry = ModelRegistry(store)
    stop = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = asyncio.create_task(advance_jobs(store, stop))
        yield
        stop.set()
        await worker

    app = FastAPI(title="InnovationCore AI Platform", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.auth = auth
    origins = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in origins], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(HTTPException)
    def http_error(request: Request, exc: HTTPException):
        body = {"detail": exc.detail}
        if exc.status_code in (423, 429) and exc.headers and exc.headers.get("Retry-After"):
            body["retry_after"] = int(exc.headers["Retry-After"])
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    def authorized(authorization: str | None = Header(default=None)) -> dict:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "请先登录")
        return auth.current(authorization[7:])

    def admin_authorized(principal: dict = Depends(authorized)) -> dict:
        if principal["role"] != "admin":
            raise HTTPException(403, "需要管理员权限")
        return principal

    def scoped_data(principal: dict) -> dict:
        """Filter both records and their referenced resources before returning data."""
        data = store.snapshot()
        model_by_id = {row["id"]: row for row in data["models"]}
        datasets = visible(data["datasets"], principal)
        dataset_ids = {row["id"] for row in datasets}

        def model_allowed(identifier: str) -> bool:
            model = model_by_id.get(identifier)
            # An unstored model is from the public provider catalog.
            return model is None or can_view(model, principal)

        evaluations = [row for row in data["evaluations"]
                       if can_view(row, principal) and model_allowed(row["model_id"])
                       and row["dataset_id"] in dataset_ids]
        evaluation_ids = {row["id"] for row in evaluations}
        return {
            "models": [row for row in data["models"] if can_view(row, principal)],
            "datasets": datasets,
            "evaluations": evaluations,
            "reports": [row for row in data["reports"] if can_view(row, principal)
                        and row["evaluation_id"] in evaluation_ids],
            "deployments": [row for row in data["deployments"] if can_view(row, principal)
                            and model_allowed(row["model_id"])],
            "school_jobs": visible(data["school_jobs"], principal),
            "annotations": [row for row in data["annotations"] if can_view(row, principal)
                            and row["dataset_id"] in dataset_ids],
        }

    @app.get("/health")
    def health():
        return {"status": "ok", "demo": demo}

    @app.post("/api/auth/register", status_code=201)
    def register(body: Register, response: Response, request: Request):
        response.headers["Cache-Control"] = "no-store"
        return auth.register(body.username, body.phone, body.password,
                             [item.model_dump() for item in body.security_questions], source_ip(request))

    @app.post("/api/auth/login")
    def login(body: Login, response: Response, request: Request):
        response.headers["Cache-Control"] = "no-store"
        return auth.login_password(body.username.strip(), body.password, source_ip(request))

    @app.get("/api/auth/security-questions")
    def security_questions(username: str, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return auth.get_security_questions(username, source_ip(request))

    @app.put("/api/auth/security-questions")
    def set_security_questions(body: SecurityQuestionsInput, response: Response,
                               principal: dict = Depends(authorized)):
        response.headers["Cache-Control"] = "no-store"
        return auth.enroll_security_questions(
            principal["id"], [item.model_dump() for item in body.security_questions])

    @app.post("/api/auth/password/reset")
    def reset_password(body: PasswordReset, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return auth.reset_password(body.username, body.new_password,
                                   [item.model_dump() for item in body.answers], source_ip(request))

    @app.post("/api/auth/password/change")
    def change_password(body: PasswordChange, request: Request, response: Response,
                        principal: dict = Depends(authorized)):
        response.headers["Cache-Control"] = "no-store"
        if principal["role"] == "admin":
            raise HTTPException(403, "管理员密码由服务配置管理")
        return auth.reset_password(principal["username"], body.new_password,
                                   [item.model_dump() for item in body.answers],
                                   source_ip(request), expected_user_id=principal["id"])

    @app.post("/api/auth/logout", status_code=204, dependencies=[Depends(authorized)])
    def logout(authorization: str = Header()):
        auth.logout(authorization[7:])
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.get("/api/auth/me")
    def me(principal: dict = Depends(authorized)):
        return {**principal, "demo": demo}

    @app.get("/api/dashboard")
    def dashboard(principal: dict = Depends(authorized)):
        data = scoped_data(principal)
        counts = {k: len(data[k]) for k in ("models", "datasets", "evaluations", "reports", "deployments", "school_jobs")}
        try:
            counts["models"] = len(registry.list_models(principal))
        except HTTPException:
            pass
        return {"counts": counts,
                "recent_evaluations": data["evaluations"][:5], "recent_deployments": data["deployments"][:5],
                "model_scores": [{"name": m["name"], "score": round(sum(m["scores"]) / len(m["scores"]), 1)}
                                 for m in data["models"] if m.get("scores")],
                "demo": demo}

    @app.get("/api/models")
    def models(principal: dict = Depends(authorized)):
        return [registry.public(model, principal) for model in registry.list_models(principal)]

    @app.post("/api/models", status_code=201)
    def create_model(body: ModelInput, principal: dict = Depends(authorized)):
        return registry.save(body, actor=principal)

    @app.put("/api/models/{model_id:path}")
    def update_model(model_id: str, body: ModelInput, principal: dict = Depends(authorized)):
        return registry.save(body, model_id, actor=principal)

    @app.delete("/api/models/{model_id:path}")
    def delete_model(model_id: str, principal: dict = Depends(authorized)):
        return registry.delete(model_id, principal)

    @app.get("/api/leaderboard")
    def leaderboard(board: Literal["Ruler1", "Ruler2", "VLM", "Arena"] = "Ruler1",
                    principal: dict = Depends(authorized)):
        weights = {"Ruler1": [0.3, 0.3, 0.25, 0.05, 0.1], "Ruler2": [0.2, 0.15, 0.15, 0.1, 0.4],
                   "VLM": [0.1, 0.1, 0.1, 0.6, 0.1], "Arena": [0.25, 0.25, 0.25, 0.15, 0.1]}[board]
        rows = [{"id": m["id"], "name": m["name"], "provider": m["provider"],
                 "score": round(sum(a * b for a, b in zip(m["scores"], weights)), 1), "scores": m["scores"]}
                for m in scoped_data(principal)["models"] if len(m.get("scores", [])) == 5]
        rows.sort(key=lambda m: m["score"], reverse=True)
        return {"board": board, "rows": [{**m, "rank": i + 1} for i, m in enumerate(rows)], "simulated": True}

    @app.get("/api/recommendations")
    def recommendations(scenario: Literal["language", "code", "reasoning", "vision", "telecom"] = "language",
                        principal: dict = Depends(authorized)):
        index = {"language": 0, "code": 2, "reasoning": 1, "vision": 3, "telecom": 4}[scenario]
        models = [m for m in scoped_data(principal)["models"] if len(m.get("scores", [])) == 5]
        ranked = sorted(models, key=lambda m: m["scores"][index], reverse=True)[:3]
        return {"scenario": scenario, "rows": [registry.public(model, principal) for model in ranked],
                "simulated": True}

    @app.get("/api/deployments")
    def deployments(principal: dict = Depends(authorized)):
        return scoped_data(principal)["deployments"]

    @app.post("/api/deployments", status_code=201)
    def create_deployment(body: DeploymentInput, principal: dict = Depends(authorized)):
        model = registry.get_model(body.model_id, principal)
        def op(data):
            item = {"id": uid("deployment"), **body.model_dump(), "model_name": model["name"], "status": "pending",
                    "created_at": now(), "simulated": True, **ownership(principal, body.visibility)}
            data["deployments"].insert(0, item)
            return item
        return store.update(op)

    @app.patch("/api/deployments/{identifier}")
    def decide_deployment(identifier: str, body: Decision, principal: dict = Depends(admin_authorized)):
        def op(data):
            item = find(data["deployments"], identifier)
            if item["status"] != "pending":
                raise HTTPException(409, "该申请已处理")
            item["status"] = "approved" if body.action == "approve" else "rejected"
            item["decided_at"] = now()
            return item
        return store.update(op)

    @app.get("/api/datasets")
    def datasets(principal: dict = Depends(authorized)):
        return visible(store.snapshot()["datasets"], principal)

    @app.post("/api/datasets", status_code=201)
    def create_dataset(body: DatasetInput, principal: dict = Depends(authorized)):
        def op(data):
            item = {"id": uid("ds"), **body.model_dump(), "size": 0, "files": [],
                    **ownership(principal, body.visibility)}
            data["datasets"].insert(0, item)
            return item
        return store.update(op)

    @app.delete("/api/datasets/{dataset_id}")
    def delete_dataset(dataset_id: str, principal: dict = Depends(authorized)):
        def op(data):
            item = require_edit(find(data["datasets"], dataset_id), principal)
            if any(t["dataset_id"] == dataset_id for t in data["evaluations"]):
                raise HTTPException(409, "数据集已被评测任务引用")
            data["datasets"].remove(item)
            return item
        item = store.update(op)
        for entry in item["files"]:
            (store.uploads / entry["storage_name"]).unlink(missing_ok=True)
        return {"deleted": dataset_id}

    @app.post("/api/datasets/{dataset_id}/files", status_code=201)
    async def upload_dataset_file(dataset_id: str, file: UploadFile = File(...),
                                  principal: dict = Depends(authorized)):
        require_edit(find(store.snapshot()["datasets"], dataset_id), principal)
        original = (file.filename or "upload.bin").replace("\\", "/").split("/")[-1][:160]
        if not original or original in (".", ".."):
            raise HTTPException(400, "文件名无效")
        file_id = uid("file")
        storage_name = file_id + Path(original).suffix[:12]
        target = store.uploads / storage_name
        length = 0
        try:
            with target.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    length += len(chunk)
                    if length > 25 * 1024 * 1024:
                        raise HTTPException(413, "演示环境单文件上限为 25 MiB")
                    output.write(chunk)
            entry = {"id": file_id, "name": original, "storage_name": storage_name, "bytes": length, "uploaded_at": now()}
            def add_file(data):
                dataset = require_edit(find(data["datasets"], dataset_id), principal)
                dataset["files"].append(entry)
                return entry
            return store.update(add_file)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @app.get("/api/datasets/{dataset_id}/files/{file_id}")
    def download_dataset_file(dataset_id: str, file_id: str, principal: dict = Depends(authorized)):
        dataset = require_view(find(store.snapshot()["datasets"], dataset_id), principal)
        entry = find(dataset["files"], file_id)
        target = store.uploads / entry["storage_name"]
        if not target.is_file():
            raise HTTPException(404, "文件不存在")
        def count_download(data):
            current = find(find(data["datasets"], dataset_id)["files"], file_id)
            current["downloads"] = current.get("downloads", 0) + 1
        store.update(count_download)
        return FileResponse(target, filename=entry["name"], media_type="application/octet-stream")

    @app.get("/api/data-governance")
    def data_governance(principal: dict = Depends(authorized)):
        data = scoped_data(principal)
        categories = {}
        for dataset in data["datasets"]:
            categories[dataset["category"]] = categories.get(dataset["category"], 0) + 1
        return {"categories": categories, "files": sum(len(x["files"]) for x in data["datasets"]),
                "downloads": sum(f.get("downloads", 0) for x in data["datasets"] for f in x["files"]),
                "annotations": len(data["annotations"]), "vla": {"status": "待接入", "scenes": None, "applications": None, "simulated": True}}

    @app.get("/api/annotations")
    def annotations(dataset_id: str | None = None, principal: dict = Depends(authorized)):
        rows = scoped_data(principal)["annotations"]
        return [row for row in rows if dataset_id is None or row["dataset_id"] == dataset_id]

    @app.post("/api/annotations", status_code=201)
    def create_annotation(body: AnnotationInput, principal: dict = Depends(authorized)):
        def op(data):
            dataset = require_view(find(data["datasets"], body.dataset_id), principal)
            item = {"id": uid("annotation"), **body.model_dump(), "dataset_name": dataset["name"],
                    "created_at": now(), **ownership(principal, body.visibility)}
            data["annotations"].insert(0, item)
            return item
        return store.update(op)

    @app.get("/api/evaluations")
    def evaluations(principal: dict = Depends(authorized)):
        return scoped_data(principal)["evaluations"]

    @app.post("/api/evaluations", status_code=201)
    def create_evaluation(body: EvalInput, principal: dict = Depends(authorized)):
        model = registry.get_model(body.model_id, principal)
        def op(data):
            dataset = require_view(find(data["datasets"], body.dataset_id), principal)
            item = {"id": uid("eval"), **body.model_dump(), "model_name": model["name"], "dataset_name": dataset["name"],
                    "status": "queued", "progress": 0, "created_at": now(), "updated_at": now(), "logs": [f"{now()} 任务已提交"],
                    "report_id": None, "simulated": True, **ownership(principal, body.visibility)}
            data["evaluations"].insert(0, item)
            return item
        return store.update(op)

    @app.post("/api/evaluations/batch", status_code=201)
    def create_batch_evaluations(body: BatchEvalInput, principal: dict = Depends(authorized)):
        if len(set(body.model_ids)) != len(body.model_ids):
            raise HTTPException(422, "模型不能重复")
        models = [registry.get_model(identifier, principal) for identifier in body.model_ids]
        def op(data):
            dataset = require_view(find(data["datasets"], body.dataset_id), principal)
            created = []
            for model in models:
                task = {"id": uid("eval"), "model_id": model["id"], "dataset_id": dataset["id"],
                        "benchmark": body.benchmark, "model_name": model["name"], "dataset_name": dataset["name"],
                        "status": "queued", "progress": 0, "created_at": now(), "updated_at": now(),
                        "logs": [f"{now()} 并行评测任务已提交"], "report_id": None, "simulated": True,
                        **ownership(principal, body.visibility)}
                data["evaluations"].insert(0, task)
                created.append(task)
            return created
        return store.update(op)

    @app.get("/api/evaluations/{identifier}")
    def evaluation(identifier: str, principal: dict = Depends(authorized)):
        return find(scoped_data(principal)["evaluations"], identifier)

    @app.patch("/api/evaluations/{identifier}")
    def action_evaluation(identifier: str, body: TaskAction, principal: dict = Depends(authorized)):
        find(scoped_data(principal)["evaluations"], identifier)
        def op(data):
            task = require_edit(find(data["evaluations"], identifier), principal)
            allowed = {"pause": ("queued", "running"), "resume": ("paused",), "cancel": ("queued", "running", "paused")}
            if task["status"] not in allowed[body.action]:
                raise HTTPException(409, "当前状态不能执行该操作")
            task["status"] = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[body.action]
            task["updated_at"] = now()
            task["logs"].append(f"{now()} 用户操作：{body.action}")
            return task
        return store.update(op)

    @app.get("/api/evaluations/{identifier}/events")
    async def evaluation_events(identifier: str, request: Request, principal: dict = Depends(authorized)):
        find(scoped_data(principal)["evaluations"], identifier)
        async def stream():
            previous = ""
            while not await request.is_disconnected():
                try:
                    state = find(scoped_data(principal)["evaluations"], identifier)
                except HTTPException:
                    break
                serialized = json.dumps(state, ensure_ascii=False)
                if serialized != previous:
                    yield f"event: state\ndata: {serialized}\n\n"
                    previous = serialized
                if state["status"] in ("completed", "cancelled", "failed"):
                    break
                await asyncio.sleep(0.7)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/reports")
    def reports(principal: dict = Depends(authorized)):
        return scoped_data(principal)["reports"]

    @app.patch("/api/reports/{identifier}/publish")
    def publish_report(identifier: str, principal: dict = Depends(admin_authorized)):
        def op(data):
            report = find(data["reports"], identifier)
            report["published"] = not report["published"]
            return report
        return store.update(op)

    @app.get("/api/agents/papers", dependencies=[Depends(authorized)])
    def papers(q: str = ""):
        rows = store.snapshot()["papers"]
        return {"rows": [p for p in rows if q.lower() in (p["title"] + p["category"] + p["abstract"]).lower()],
                "simulated": True, "message": "当前使用示例论文目录；arXiv 抓取与翻译需要接入外部服务。"}

    @app.get("/api/agents/integrations", dependencies=[Depends(authorized)])
    def integrations():
        return [{"name": x, "status": "待接入", "simulated": True} for x in
                ("arXiv", "Deep Research", "PPT 生成", "HuggingFace 下载", "ModelScope 下载", "Dify / Coze / Owl")]

    @app.post("/api/agents/tools/{kind}", dependencies=[Depends(authorized)])
    def agent_tool(kind: Literal["research", "ppt", "prompt", "translate", "download"], body: ToolInput):
        outputs = {
            "research": "研究任务草案：明确问题 → 收集来源 → 交叉验证 → 形成结构化报告。",
            "ppt": "演示文稿草案：背景与目标 / 方案架构 / 核心流程 / 价值与实施计划。",
            "prompt": "优化建议：写明角色、目标、输入字段、约束、输出格式与验收例子。",
            "translate": "论文翻译服务尚未接入。请配置实际翻译模型后生成正式译文。",
            "download": "下载适配器尚未接入。此请求没有下载任何外部资源。",
        }
        return {"kind": kind, "input": body.content[:200], "output": outputs[kind], "simulated": True}

    @app.post("/api/agents/chat/stream")
    async def chat(body: ChatInput, principal: dict = Depends(authorized)):
        model = await asyncio.to_thread(registry.get_model, body.model_id, principal)
        endpoint = registry.next_endpoint(model)
        api_key = registry.api_key(endpoint)
        endpoint_url = endpoint["url"]
        url = endpoint_url if endpoint_url.endswith("/chat/completions") else endpoint_url.rstrip("/") + "/chat/completions"
        messages = [{"role": item["role"], "content": item["content"][:8000]}
                    for item in body.history[-20:]
                    if item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str)]
        messages.append({"role": "user", "content": body.message})
        payload = {
            "model": model["id"], "messages": messages, "stream": True,
            "top_p": body.top_p if body.top_p is not None else endpoint["top_p"],
            "temperature": body.temperature if body.temperature is not None else endpoint["temperature"],
        }
        if model.get("provider", "").lower() != "deepseek" or endpoint.get("self_deployed"):
            payload["top_k"] = body.top_k if body.top_k is not None else endpoint["top_k"]
        schema = endpoint.get("thinking_schema") or {}
        payload.update(schema.get("enabled" if body.thinking else "disabled", {}))
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        def event(name: str, data: dict) -> str:
            return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        async def stream():
            produced = False
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20)) as client:
                        async with client.stream("POST", url, headers=headers, json=payload) as upstream:
                            if upstream.status_code >= 400:
                                yield event("error", {"message": f"模型服务返回 HTTP {upstream.status_code}"})
                                return
                            if "application/json" in upstream.headers.get("content-type", ""):
                                try:
                                    message = (await upstream.aread())
                                    message = json.loads(message)["choices"][0]["message"]
                                    if message.get("reasoning_content"):
                                        produced = True
                                        yield event("reasoning", {"text": message["reasoning_content"]})
                                    if message.get("content"):
                                        produced = True
                                        yield event("chunk", {"text": message["content"]})
                                except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                                    yield event("error", {"message": "模型服务返回了无法解析的响应"})
                                    return
                                break
                            async for line in upstream.aiter_lines():
                                if not line.startswith("data:"):
                                    continue
                                data = line[5:].strip()
                                if data == "[DONE]":
                                    break
                                try:
                                    packet = json.loads(data)
                                    if packet.get("error"):
                                        yield event("error", {"message": "模型服务返回错误"})
                                        return
                                    delta = packet.get("choices", [{}])[0].get("delta", {})
                                except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                                    continue
                                reasoning = delta.get("reasoning_content")
                                content = delta.get("content")
                                if reasoning:
                                    produced = True
                                    yield event("reasoning", {"text": reasoning})
                                if content:
                                    produced = True
                                    yield event("chunk", {"text": content})
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError):
                    if produced or attempt == 2:
                        yield event("error", {"message": "连接模型服务失败，请检查端点 URL 和网络"})
                        return
                    await asyncio.sleep(0.4 * (attempt + 1))
                except httpx.HTTPError:
                    yield event("error", {"message": "模型服务连接中断"})
                    return
            if not produced:
                yield event("error", {"message": "模型服务未返回内容"})
                return
            yield event("done", {})
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/school/jobs")
    def school_jobs(principal: dict = Depends(authorized)):
        return visible(store.snapshot()["school_jobs"], principal)

    @app.post("/api/school/jobs", status_code=201)
    def create_school_job(body: SchoolJobInput, principal: dict = Depends(authorized)):
        def op(data):
            item = {"id": uid("school"), **body.model_dump(), "status": "queued", "progress": 0,
                    "created_at": now(), "updated_at": now(), "logs": [f"{now()} 已提交脚本；演示模式不会执行代码"],
                    "simulated": True, **ownership(principal, body.visibility)}
            data["school_jobs"].insert(0, item)
            return item
        return store.update(op)

    @app.post("/api/school/infer")
    def infer(body: InferenceInput, principal: dict = Depends(authorized)):
        model = registry.get_model(body.model_id, principal)
        return {"model": model["name"], "zone": body.zone, "output": f"[模拟推理] {model['name']} 收到输入：{body.prompt[:200]}",
                "tokens": len(body.prompt), "simulated": True}

    def metrics():
        t = time.time()
        return {"devices": [
            {"name": f"910B-{i}", "utilization": round(45 + 22 * math.sin(t / 8 + i)),
             "memory": round(50 + 15 * math.cos(t / 11 + i)), "status": "模拟在线"} for i in range(4)],
            "timestamp": now(), "simulated": True}

    @app.get("/api/monitor", dependencies=[Depends(authorized)])
    def monitor():
        return metrics()

    @app.websocket("/ws/monitor")
    async def monitor_ws(websocket: WebSocket):
        await websocket.accept()
        try:
            first = await asyncio.wait_for(websocket.receive_json(), timeout=5)
            auth.current(first.get("token", ""))
            while True:
                await websocket.send_json(metrics())
                await asyncio.sleep(2)
        except (WebSocketDisconnect, asyncio.TimeoutError, HTTPException, ValueError):
            try:
                await websocket.close()
            except RuntimeError:
                pass

    # A built SPA can be served from the same process on both Windows and Linux.
    # The API and WebSocket routes above keep their own prefixes.
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if (dist / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{spa_path:path}", include_in_schema=False)
        def spa(spa_path: str):
            if spa_path.startswith(("api/", "ws/")):
                raise HTTPException(404, "路由不存在")
            candidate = (dist / spa_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
