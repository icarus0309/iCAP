"""AISF agent harness over HTTP or WebSocket.

Adapts an external "question -> answer" service (e.g. the Huawei AISF bridge app,
which exposes ``POST /ask {"question": ...} -> {"answer": ...}``) to AgentCompass.
Unlike codex/openclaw harnesses there is no local binary to install and no model
endpoint to drive: each task question is sent to the service and the reply carries
the final answer.

Transport is chosen from the ``base_url`` scheme:

- ``http://`` / ``https://`` -> HTTP (single POST, JSON body/response)
- ``ws://`` / ``wss://``   -> WebSocket (streamed messages)

Usage::

    agentcompass run <benchmark> aisf_agent <model-id> \\
        --harness-params '{"base_url": "http://ip:8080/ask", "question_field": "question", "answer_field": "answer"}'

Protocol knobs (tune to your service):

- ``question_field``: outgoing JSON key for the question ("" = send the raw
  question text as a plain message instead of a JSON object).
- ``answer_field``: incoming JSON key holding the answer ("" = every text message
  / the whole HTTP body is the answer; supports dot paths like ``data.answer``).
- ``done_field`` / ``done_value``: WebSocket-only completion signal. When an
  incoming message has ``done_field`` equal to ``done_value`` (or any truthy value
  if ``done_value`` is empty), the exchange is complete. Leave ``done_field`` empty
  to rely on the server closing the connection instead. Ignored for HTTP.
- ``aggregate``: join all answer fragments vs. keep only the last one (WS only).
- ``extra_body``: JSON object merged into the outgoing message/body (session ids,
  tokens, model selection, etc.).
- ``headers``: extra HTTP headers / WS handshake headers as a JSON object string.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict

import aiohttp

from agentcompass.runtime import (HARNESSES, AssistantContent, BaseHarness, EnvironmentSession, EnvironmentSpec,
                                  HarnessPlan, ModelSpec, PreparedTask, RunRequest, RunResult, StepInfo, TaskStatus,
                                  TrajMetric, Trajectory)
from agentcompass.runtime.config import RuntimeHarnessConfig, config_field

logger = logging.getLogger(__name__)

_TERMINAL_MSG_TYPES = {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}

_HTTP_SCHEMES = ("http://", "https://")
_WS_SCHEMES = ("ws://", "wss://")


@dataclass(slots=True)
class AisfAgentConfig(RuntimeHarnessConfig):
    """User-facing params for the AISF agent harness (HTTP or WebSocket)."""

    base_url: str = config_field(default="", description="HTTP or WebSocket endpoint URL, e.g. http://ip:8080/ask or wss://ip:8443/ws")
    question_field: str = config_field(
        default="question",
        description="JSON key carrying the question in the outgoing message/body (empty = send raw question text)",
    )
    answer_field: str = config_field(
        default="answer",
        description="JSON key holding the answer in incoming messages/response; empty = each text message / whole body "
        "is the answer (supports dot paths like data.answer)",
    )
    done_field: str = config_field(
        default="done",
        description="WS only: incoming JSON key that signals the final answer has arrived; empty = rely on server closing",
    )
    done_value: str = config_field(
        default="",
        description="WS only: value done_field must equal to finish (empty = any truthy presence)",
    )
    aggregate: bool = config_field(default=False, description="WS only: join all answer fragments instead of keeping only the last")
    extra_body: str = config_field(default="", description="JSON object merged into the outgoing message/body")
    headers: str = config_field(default="", description="Extra HTTP/WS handshake headers as a JSON object string")
    timeout: int = config_field(default=600, description="Whole-exchange timeout in seconds")
    max_retries: int = config_field(default=3, description="Retry count for transient connect failures")

    def __post_init__(self) -> None:
        self.base_url = str(self.base_url or "").strip()
        self.question_field = str(self.question_field or "").strip()
        self.answer_field = str(self.answer_field or "").strip()
        self.done_field = str(self.done_field or "").strip()
        self.done_value = str(self.done_value or "").strip()
        self.aggregate = bool(self.aggregate)
        self.extra_body = str(self.extra_body or "").strip()
        self.headers = str(self.headers or "").strip()
        self.timeout = int(self.timeout or 600)
        self.max_retries = int(self.max_retries or 3)


@dataclass(slots=True)
class AisfAgentPlan(HarnessPlan):
    base_url: str = ""
    question_field: str = "question"
    answer_field: str = "answer"
    done_field: str = "done"
    done_value: str = ""
    aggregate: bool = False
    extra_body: str = ""
    headers: str = ""
    timeout: int = 600
    max_retries: int = 3


def _extract_question(prompt: str) -> str:
    """Turn prepared.input.prompt (plain text or JSON message list) into one question string."""
    text = str(prompt or "").strip()
    if not text:
        return text
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(data, list):
        chunks: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("text"):
                        chunks.append(str(part["text"]))
        return "\n".join(c for c in chunks if c)
    if isinstance(data, dict):
        for key in ("question", "prompt", "content", "query", "input"):
            if data.get(key):
                return str(data[key])
        return text
    return text


def _dig(payload: Any, path: str) -> Any:
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _is_done(raw: str, done_field: str, done_value: str) -> bool:
    if not done_field:
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(data, dict) or done_field not in data:
        return False
    value = data[done_field]
    if not done_value:
        return bool(value)
    return str(value) == done_value


def _answer_from(raw: str, answer_field: str) -> str | None:
    """Extract the answer from one text message / HTTP body, or None when it has no answer field."""
    if not answer_field:
        return raw
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    value = _dig(data, answer_field)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _resolve_transport(base_url: str) -> str:
    """Infer 'http' or 'ws' from the base_url scheme."""
    if base_url.startswith(_HTTP_SCHEMES):
        return "http"
    if base_url.startswith(_WS_SCHEMES):
        return "ws"
    raise ValueError(f"base_url must start with http(s):// or ws(s)://, got: {base_url!r}")


@HARNESSES.register()
class AisfAgentHarness(BaseHarness):
    """Send each task question to an AISF bridge app over HTTP or WebSocket and collect the answer."""

    id = "aisf_agent"
    parallel_attempts_safe = True
    description = ("Sends each task question to an AISF bridge app (HTTP POST /ask or WebSocket) and collects its "
                   "answer (question -> answer benchmarks).")
    config_class = AisfAgentConfig
    plan_class = AisfAgentPlan

    def supports(self, environment: EnvironmentSpec, model: ModelSpec) -> bool:
        # The AISF agent is a self-contained service: no local environment is needed.
        return environment.id == "host_process"

    async def start_session(self, env: EnvironmentSession, req: RunRequest, plan: AisfAgentPlan) -> Dict[str, Any]:
        if not plan.base_url:
            raise RuntimeError("aisf_agent requires harness param `base_url` (http(s):// or ws(s):// endpoint)")
        transport = _resolve_transport(plan.base_url)
        headers: Dict[str, str] = {}
        if plan.headers:
            parsed = json.loads(plan.headers)
            if isinstance(parsed, dict):
                headers = {str(k): str(v) for k, v in parsed.items()}
        extra: Dict[str, Any] = {}
        if plan.extra_body:
            parsed = json.loads(plan.extra_body)
            if isinstance(parsed, dict):
                extra = parsed
        session = aiohttp.ClientSession()
        return {
            "session": session,
            "transport": transport,
            "base_url": plan.base_url,
            "headers": headers,
            "question_field": plan.question_field,
            "answer_field": plan.answer_field,
            "done_field": plan.done_field,
            "done_value": plan.done_value,
            "aggregate": plan.aggregate,
            "extra_body": extra,
            "timeout": plan.timeout,
            "max_retries": plan.max_retries,
        }

    # ── outgoing helpers ────────────────────────────────────────────────────

    def _outgoing_body(self, session: Dict[str, Any], question: str) -> Dict[str, Any]:
        body = dict(session["extra_body"])
        body[session["question_field"]] = question
        return body

    def _outgoing_message(self, session: Dict[str, Any], question: str) -> str:
        return json.dumps(self._outgoing_body(session, question), ensure_ascii=False)

    # ── HTTP transport ──────────────────────────────────────────────────────

    @staticmethod
    def _build_trajectory(question: str, answer: str, started_at: datetime, finished_at: datetime,
                          elapsed_ms: float) -> Trajectory:
        """Build a single-step trajectory recording AISF response latency."""
        trajectory = Trajectory(started_at=started_at, finished_at=finished_at)
        trajectory.steps.append(
            StepInfo(
                step_id=0,
                assistant_content=AssistantContent(content=answer),
                metric=TrajMetric(llm_infer_ms=elapsed_ms),
                user_content=question,
                started_at=started_at,
                finished_at=finished_at,
            )
        )
        return trajectory

    async def _run_http(self, session: Dict[str, Any], question: str, prepared: PreparedTask) -> RunResult:
        """Single POST + parse JSON response."""
        kwargs: Dict[str, Any] = {"headers": session["headers"]}
        if session["question_field"]:
            kwargs["json"] = self._outgoing_body(session, question)
        else:
            kwargs["data"] = question

        started_at = datetime.now().astimezone()
        last_error = ""
        for attempt in range(session["max_retries"]):
            t0 = time.monotonic()
            try:
                async with asyncio.timeout(session["timeout"]):
                    async with session["session"].post(session["base_url"], **kwargs) as resp:
                        raw = await resp.text()
                        status = resp.status
            except TimeoutError:
                last_error = f"aisf_agent HTTP request timed out after {session['timeout']}s"
                logger.warning("%s | task=%s", last_error, prepared.task_id)
                continue
            except (aiohttp.ClientError, OSError) as exc:
                last_error = f"aisf_agent HTTP request failed: {exc}"
                logger.warning("%s | attempt=%d/%d | task=%s", last_error, attempt + 1, session["max_retries"],
                               prepared.task_id)
                await asyncio.sleep(min(2 ** attempt, 15))
                continue

            if status >= 400:
                last_error = f"aisf_agent HTTP {status}: {raw[:300]}"
                logger.warning("%s | task=%s", last_error, prepared.task_id)
                await asyncio.sleep(min(2 ** attempt, 15))
                continue

            answer = _answer_from(raw, session["answer_field"])
            if answer is None:
                answer = raw
            finished_at = datetime.now().astimezone()
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            trajectory = self._build_trajectory(question, answer, started_at, finished_at, elapsed_ms)
            return RunResult(
                task_id=prepared.task_id,
                status=TaskStatus.COMPLETED,
                final_answer=answer,
                trajectory=trajectory,
                telemetry={
                    "transport": "http",
                    "base_url": session["base_url"],
                    "attempts": attempt + 1,
                    "http_status": status,
                    "llm_infer_ms": elapsed_ms,
                },
                artifacts={"raw_response": raw, "final_answer": answer},
            )

        return RunResult(
            task_id=prepared.task_id,
            status=TaskStatus.RUN_ERROR,
            error=last_error or "aisf_agent HTTP request failed",
            trajectory=Trajectory(started_at=started_at, finished_at=datetime.now().astimezone()),
        )

    # ── WebSocket transport ─────────────────────────────────────────────────

    async def _exchange(self, session: Dict[str, Any], question: str) -> tuple[list[str], list[str], str]:
        """Connect, send, collect until done/close. Returns (answers, raw_messages, error)."""
        out_text = self._outgoing_message(session, question)
        answers: list[str] = []
        raw_messages: list[str] = []

        async with asyncio.timeout(session["timeout"]):
            async with session["session"].ws_connect(session["base_url"], headers=session["headers"]) as ws:
                await ws.send_str(out_text)
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        raw = msg.data
                        raw_messages.append(raw)
                        answer = _answer_from(raw, session["answer_field"])
                        if answer is not None:
                            answers.append(answer)
                        if _is_done(raw, session["done_field"], session["done_value"]):
                            break
                    elif msg.type in _TERMINAL_MSG_TYPES:
                        break

        if not answers and raw_messages:
            answers = [raw_messages[-1]]
        return answers, raw_messages, ""

    async def _run_ws(self, session: Dict[str, Any], question: str, prepared: PreparedTask) -> RunResult:
        started_at = datetime.now().astimezone()
        last_error = ""
        for attempt in range(session["max_retries"]):
            t0 = time.monotonic()
            try:
                answers, raw_messages, error = await self._exchange(session, question)
            except TimeoutError:
                last_error = f"aisf_agent exchange timed out after {session['timeout']}s"
                logger.warning("%s | task=%s", last_error, prepared.task_id)
                continue
            except (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError, OSError) as exc:
                last_error = f"aisf_agent connection failed: {exc}"
                logger.warning("%s | attempt=%d/%d | task=%s", last_error, attempt + 1, session["max_retries"],
                               prepared.task_id)
                await asyncio.sleep(min(2 ** attempt, 15))
                continue
            except Exception as exc:  # noqa: BLE001 - any unexpected failure is a run error
                logger.exception("aisf_agent exchange failed | task=%s", prepared.task_id)
                return RunResult(
                    task_id=prepared.task_id,
                    status=TaskStatus.RUN_ERROR,
                    error=f"aisf_agent exchange failed: {exc}",
                    trajectory=Trajectory(started_at=started_at, finished_at=datetime.now().astimezone()),
                )

            if error:
                return RunResult(
                    task_id=prepared.task_id,
                    status=TaskStatus.RUN_ERROR,
                    error=error,
                    trajectory=Trajectory(started_at=started_at, finished_at=datetime.now().astimezone()),
                )

            answer = "\n".join(answers) if session["aggregate"] else (answers[-1] if answers else "")
            finished_at = datetime.now().astimezone()
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            trajectory = self._build_trajectory(question, answer, started_at, finished_at, elapsed_ms)
            return RunResult(
                task_id=prepared.task_id,
                status=TaskStatus.COMPLETED,
                final_answer=answer,
                trajectory=trajectory,
                telemetry={
                    "transport": "ws",
                    "base_url": session["base_url"],
                    "attempts": attempt + 1,
                    "message_count": len(raw_messages),
                    "answer_fragments": len(answers),
                    "llm_infer_ms": elapsed_ms,
                },
                artifacts={"raw_messages": raw_messages, "final_answer": answer},
            )

        return RunResult(
            task_id=prepared.task_id,
            status=TaskStatus.RUN_ERROR,
            error=last_error or "aisf_agent exchange failed",
            trajectory=Trajectory(started_at=started_at, finished_at=datetime.now().astimezone()),
        )

    @staticmethod
    def _build_prompt(question: str, system_prompt: str | None = None) -> str:
        """构造发给 AISF 的 question，与 codex 的 prompt 保持一致（语言跟随 + 搜索引导）。"""
        has_chinese = any('\u4e00' <= ch <= '\u9fff' for ch in question)
        lang = "请用中文回答。" if has_chinese else "Please answer in English."
        lang = lang + " 使用联网搜索。"
        lines = [lang, "", question.rstrip()]
        if system_prompt:
            lines.insert(0, str(system_prompt))
        return "\n".join(lines).strip()

    # ── run_task dispatch ───────────────────────────────────────────────────

    async def run_task(
        self,
        session: Dict[str, Any],
        prepared: PreparedTask,
        req: RunRequest,
        plan: AisfAgentPlan,
    ) -> RunResult:
        question = _extract_question(prepared.input.prompt)
        if not question:
            return RunResult(
                task_id=prepared.task_id,
                status=TaskStatus.RUN_ERROR,
                error="aisf_agent: empty task question",
                trajectory=Trajectory(),
            )
        question = self._build_prompt(question, prepared.input.system_prompt)

        if session["transport"] == "http":
            return await self._run_http(session, question, prepared)
        return await self._run_ws(session, question, prepared)

    async def close_session(self, session: Dict[str, Any]) -> None:
        client = session.get("session")
        if client is not None:
            await client.close()
