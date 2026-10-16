"""Pi coding agent CLI harness."""

from __future__ import annotations

import json
import logging
import shlex
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal

from agentcompass.harnesses.utils.model import build_model_api_config
from agentcompass.runtime import (HARNESSES, AssistantContent, BaseHarness, EnvironmentSession, EnvironmentSpec,
                                  ExecResult, HarnessPlan, ModelSpec, PreparedTask, RunRequest, RunResult, StepInfo,
                                  TaskStatus, Trajectory, TrajMetric)
from agentcompass.runtime.config import (RuntimeHarnessConfig, config_field, normalize_choice, parse_bool,
                                         parse_optional_positive_int)
from agentcompass.runtime.llm import ApiProtocol, select_api_protocol
from agentcompass.utils.call import maybe_call

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PiConfig(RuntimeHarnessConfig):
    """User-facing params for the Pi coding agent harness."""

    binary: str = config_field(default="pi", description="Executable name or path.")
    install_strategy: Literal["preinstalled", "install_if_missing", "upload"] = config_field(
        default="install_if_missing",
        description="How to prepare the executable.",
    )
    install_command: str = config_field(
        default="npm install -g --ignore-scripts @earendil-works/pi-coding-agent",
        description="Command run when install_strategy needs to install the tool.",
    )
    upload_src: str = config_field(default="", description="Local file path uploaded by the upload install strategy.")
    upload_dst: str = config_field(default="/usr/bin/pi",
                                   description="Target path used by the upload install strategy.")
    config_dir: str = config_field(
        default="",
        description="Pi agent config directory (PI_CODING_AGENT_DIR). Empty derives <workspace>/.pi-agent.",
    )
    provider_id: str = config_field(
        default="agentcompass",
        description="Provider name written into models.json and used as the --model prefix.",
    )
    wire_api: Literal["openai-responses", "openai-completions"] = config_field(
        default="openai-responses",
        description="Wire protocol written into models.json `api` field.",
    )
    thinking: str = config_field(
        default="",
        description="Reasoning level (off/minimal/low/medium/high/xhigh/max). Empty omits --thinking.",
    )
    timeout: int | None = config_field(default=None, description="Task wall-clock timeout in seconds.")
    prompt_append: str = config_field(
        default="",
        description="Text appended to the task prompt before the expected-answer spec.",
    )
    approve: bool = config_field(default=True, description="Whether to pass -a to trust project-local config.")
    no_context_files: bool = config_field(
        default=True,
        description="Whether to pass -nc to disable AGENTS.md/CLAUDE.md discovery.",
    )
    no_session: bool = config_field(default=True, description="Whether to pass --no-session for per-task isolation.")
    tools: str = config_field(default="", description="Optional --tools allowlist (comma-separated).")
    skip_version_check: bool = config_field(
        default=True,
        description="Whether to set PI_SKIP_VERSION_CHECK=1.",
    )
    clear_proxy: bool = config_field(
        default=True,
        description="Clear HTTP_PROXY/HTTPS_PROXY for the Pi process (undici does not support socks5h:// proxies).",
    )

    def __post_init__(self) -> None:
        self.binary = str(self.binary or "").strip()
        if not self.binary:
            raise ValueError("binary is required")
        self.install_strategy = normalize_choice(
            self.install_strategy,
            "install_strategy",
            {"preinstalled", "install_if_missing", "upload"},
        )
        self.install_command = str(self.install_command or "").strip()
        self.upload_src = str(self.upload_src or "").strip()
        self.upload_dst = str(self.upload_dst or "").strip()
        self.config_dir = str(self.config_dir or "").strip()
        self.provider_id = str(self.provider_id or "").strip() or "agentcompass"
        self.wire_api = normalize_choice(
            self.wire_api,
            "wire_api",
            {"openai-responses", "openai-completions"},
        )
        self.thinking = str(self.thinking or "").strip()
        self.timeout = parse_optional_positive_int(self.timeout, "timeout")
        self.prompt_append = str(self.prompt_append or "").strip()
        self.approve = parse_bool(self.approve, "approve")
        self.no_context_files = parse_bool(self.no_context_files, "no_context_files")
        self.no_session = parse_bool(self.no_session, "no_session")
        self.tools = str(self.tools or "").strip()
        self.skip_version_check = parse_bool(self.skip_version_check, "skip_version_check")
        self.clear_proxy = parse_bool(self.clear_proxy, "clear_proxy")


@dataclass(slots=True)
class PiHarnessPlan(HarnessPlan):
    # Plan values are execution state derived from PiConfig and the selected
    # environment. Recipes may adjust this plan without mutating config.
    binary: str = "pi"
    install_strategy: Literal["preinstalled", "install_if_missing", "upload"] = "install_if_missing"
    install_command: str = "npm install -g --ignore-scripts @earendil-works/pi-coding-agent"
    upload_src: str = ''
    upload_dst: str = '/usr/bin/pi'
    config_dir: str = ''
    provider_id: str = "agentcompass"
    wire_api: Literal["openai-responses", "openai-completions"] = "openai-responses"
    thinking: str = ""
    timeout: int | None = None
    prompt_append: str = ""
    approve: bool = True
    no_context_files: bool = True
    no_session: bool = True
    tools: str = ""
    skip_version_check: bool = True
    clear_proxy: bool = True


@HARNESSES.register()
class PiHarness(BaseHarness):
    """Run the Pi coding agent CLI non-interactively against a prepared workspace."""

    id = "pi"
    parallel_attempts_safe = True
    description = "Runs the Pi coding agent CLI as a non-interactive coding agent for prepared workspaces such as SWE-bench and ResearchClawBench (official website: https://pi.dev)."
    config_class = PiConfig
    plan_class = PiHarnessPlan

    API_KEY_ENV = "PI_API_KEY"

    # Map canonical model api_protocol values onto Pi's models.json `api` field.
    _WIRE_API_BY_PROTOCOL = {
        "openai-responses": "openai-responses",
        "openai-chat": "openai-completions",
    }

    def supports(self, environment: EnvironmentSpec, model: ModelSpec) -> bool:
        protocols = model.api_protocols
        preferred = [ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT]
        if protocols:
            select_api_protocol(model.api_protocol, preferred, component=self.id)
        return True

    async def start_session(self, env: EnvironmentSession, req: RunRequest, plan: PiHarnessPlan) -> Dict[str, Any]:
        binary = str(plan.binary)
        install_strategy = str(plan.install_strategy or "install_if_missing")
        if install_strategy == "install_if_missing":
            check_result = await env.exec(["command", "-v", shlex.quote(binary)])
            binary_exists = check_result.returncode == 0
            if not binary_exists:
                install_result = await env.exec(str(plan.install_command), shell=True)
                if install_result.returncode != 0:
                    raise RuntimeError(f"Pi install failed: {install_result.stderr.strip()}")
        elif install_strategy == "upload":
            if not plan.upload_src:
                raise RuntimeError("Pi `upload_src` is not specified.")
            await env.upload(plan.upload_src, plan.upload_dst)
            await env.exec(["chmod", "+x", plan.upload_dst])
            binary = str(plan.upload_dst)

        if not binary.startswith("/"):
            resolve_result = await env.exec(["command", "-v", shlex.quote(binary)])
            if resolve_result.returncode != 0:
                raise RuntimeError(f"Pi binary resolution failed: {binary}")
            binary = resolve_result.stdout.strip()

        return {
            "env": env,
            "binary": binary,
            "workspace_root": getattr(env, "default_workspace_root", None) or getattr(env, "workspace", None)
            or "workspace/",
            "model_config": build_model_api_config(req.model),
        }

    @classmethod
    def _resolve_wire_api(cls, api_protocol: Any, fallback: str) -> str:
        protocols = api_protocol if isinstance(api_protocol, (list, tuple)) else [api_protocol]
        for protocol in protocols:
            key = str(protocol or "").strip().lower()
            if not key:
                continue
            mapped = cls._WIRE_API_BY_PROTOCOL.get(key)
            if mapped is not None:
                return mapped
        return fallback

    @classmethod
    async def _write_models_json(
        cls,
        env: EnvironmentSession,
        config_dir: str,
        req: RunRequest,
        plan: PiHarnessPlan,
        model_config: Dict[str, Any],
    ) -> str:
        base_url = str(model_config.get("base_url") or "").strip()
        api_key = str(model_config.get("api_key") or "").strip()
        if not base_url:
            raise RuntimeError("Pi requires an OpenAI-compatible base url")
        if not api_key:
            raise RuntimeError("Pi requires an OpenAI-compatible api key")
        wire_api = cls._resolve_wire_api(model_config.get("api_protocol"), plan.wire_api)
        payload = {
            "$schema": "https://pi.dev/schemas/models.schema.json",
            "providers": {
                plan.provider_id: {
                    "baseUrl": base_url,
                    "api": wire_api,
                    "apiKey": f"${cls.API_KEY_ENV}",
                    "models": [{"id": req.model.id}],
                }
            },
        }
        models_path = str(Path(config_dir) / "models.json")
        await env.write_text(models_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return models_path

    @staticmethod
    def _build_prompt(prompt_text: str, answer_spec: str | None, output_files: list[Any],
                      prompt_append: str = "") -> str:
        # 跟随用例语言：Pi 默认英文，中文题必须显式约束，否则全英文回复。
        has_chinese = any('\u4e00' <= ch <= '\u9fff' for ch in prompt_text)
        lang = "请用中文回答。" if has_chinese else "Please answer in English."
        lines = [lang]
        lines += ["", prompt_text.rstrip()]
        if prompt_append:
            lines += ["", prompt_append.rstrip()]
        if answer_spec:
            lines.extend(["", f"Expected answer: {answer_spec}"])
        if output_files:
            lines.extend(["", "Write the following files:"])
            for output_file in output_files:
                lines.append(f"- {output_file.path}: {output_file.description}")
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _build_default_workspace(root: str, req: RunRequest, prepared: PreparedTask) -> str:
        task_id = str(prepared.task_id or "task").strip().replace("/", "_")
        return str(Path(root) / req.benchmark.id / task_id / uuid.uuid4().hex[:8])

    @staticmethod
    def _resolve_output_path(workspace: str, requested_path: str) -> str:
        candidate = Path(str(requested_path))
        if candidate.is_absolute():
            return str(candidate)
        return str(Path(workspace) / candidate)

    async def run_task(
        self,
        session: Dict[str, Any],
        prepared: PreparedTask,
        req: RunRequest,
        plan: PiHarnessPlan,
    ) -> RunResult:
        env: EnvironmentSession = session["env"]
        prompt_text = str(prepared.input.prompt or "").strip()
        if not prompt_text:
            raise ValueError("pi requires prepared.input.prompt")
        workspace = str(prepared.input.workspace or "").strip()
        if not workspace:
            workspace = self._build_default_workspace(str(session.get("workspace_root") or "workspace/"), req, prepared)
        mkdir_result = await env.exec(["mkdir", "-p", workspace])
        if mkdir_result.returncode != 0:
            raise RuntimeError(f"Failed to create Pi workspace {workspace}: {mkdir_result.stderr.strip()}")

        config_dir = str(plan.config_dir or str(Path(workspace) / ".pi-agent"))
        await env.exec(["mkdir", "-p", config_dir])
        models_path = await self._write_models_json(env, config_dir, req, plan, session["model_config"])

        requested_files = list(prepared.output.files)
        prompt = self._build_prompt(prompt_text, prepared.output.answer, requested_files, plan.prompt_append)

        api_key = str(session["model_config"].get("api_key") or "").strip()
        argv = [
            session["binary"],
            "--mode", "json",
            "--model", f"{plan.provider_id}/{req.model.id}",
        ]
        if plan.approve:
            argv.append("-a")
        if plan.no_context_files:
            argv.append("-nc")
        if plan.no_session:
            argv.append("--no-session")
        if plan.thinking:
            argv += ["--thinking", plan.thinking]
        if plan.tools:
            argv += ["--tools", plan.tools]
        # `--` 停止参数解析，后面整段 prompt 当位置参数。
        argv += ["--", prompt]

        exec_env: Dict[str, str] = {
            "PI_CODING_AGENT_DIR": config_dir,
            self.API_KEY_ENV: api_key,
            "PI_SKIP_VERSION_CHECK": "1" if plan.skip_version_check else "0",
        }
        if plan.clear_proxy:
            # undici（Pi 的 HTTP 客户端）不支持 socks5h:// 代理，读到会抛 "Invalid URL protocol"。
            # 模型端点走 no_proxy 直连，Pi 无需代理（版本检查/目录刷新已关），故清空代理变量。
            for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
                exec_env[proxy_var] = ""
        command_text = shlex.join(argv)

        started_at = datetime.now().astimezone()
        try:
            run_result = await maybe_call(
                env.exec,
                command_text,
                shell=True,
                cwd=workspace,
                env=exec_env,
                timeout=plan.timeout,
                flags={"polling_exec": True},
            )
        except Exception:
            error = traceback.format_exc()
            logger.exception("Pi command execution failed | task=%s", prepared.task_id)
            run_result = ExecResult(returncode=-1, stderr=error)
        finished_at = datetime.now().astimezone()

        raw_events = self._parse_json_events(run_result.stdout)
        final_answer = self._extract_final_answer(raw_events, run_result.stdout)
        trajectory = self._build_trajectory(raw_events, prompt, started_at, finished_at)

        collected_files: Dict[str, str] = {}
        resolved_output_paths: Dict[str, str] = {}
        missing_files: list[str] = []
        for output_file in requested_files:
            path = str(output_file.path)
            resolved_path = self._resolve_output_path(workspace, path)
            resolved_output_paths[path] = resolved_path
            try:
                collected_files[path] = await env.read_text(resolved_path)
            except Exception:
                missing_files.append(path)

        metrics: Dict[str, Any] = {
            "exit_code": run_result.returncode,
            "stdout": run_result.stdout,
            "stderr": run_result.stderr,
            "workspace": workspace,
            "pi_models_path": models_path,
            "pi_config_dir": config_dir,
            "model_base_url": session["model_config"].get("base_url"),
            "model_api_protocol": session["model_config"].get("api_protocol"),
            "requested_output_files": [output_file.path for output_file in requested_files],
            "resolved_output_files": resolved_output_paths,
        }
        if missing_files:
            metrics["missing_output_files"] = missing_files

        error = None
        if run_result.timed_out:
            error = f"Pi command timed out after {plan.timeout} seconds"
        elif run_result.returncode != 0:
            error = run_result.stderr.strip() or f"Pi command failed: returncode={run_result.returncode}"
        else:
            error = self._run_error(raw_events)
        if error is None and missing_files:
            error = "Requested output files missing: " + ", ".join(missing_files)

        return RunResult(
            task_id=prepared.task_id,
            status=TaskStatus.RUN_ERROR if error else TaskStatus.COMPLETED,
            final_answer=final_answer,
            trajectory=trajectory,
            telemetry=metrics,
            artifacts={
                "file": collected_files,
                "raw_pi_events": raw_events,
            },
            error=error,
        )

    @staticmethod
    def _parse_json_events(stdout: str) -> list[Dict[str, Any]]:
        text = str(stdout or "").strip()
        if not text:
            return []
        events: list[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
        return events

    @staticmethod
    def _content_text(message: Dict[str, Any], kind: str) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content if kind == "text" else ""
        if not isinstance(content, list):
            return ""
        field = "text" if kind == "text" else "thinking"
        parts = [
            str(item.get(field) or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == kind
        ]
        return "\n".join(part for part in parts if part)

    @classmethod
    def _tool_calls(cls, message: Dict[str, Any]) -> list[Dict[str, Any]]:
        content = message.get("content")
        if not isinstance(content, list):
            return []
        return [item for item in content if isinstance(item, dict) and item.get("type") == "toolCall"]

    @classmethod
    def _extract_final_answer(cls, events: list[Dict[str, Any]], stdout: str) -> str:
        # 最后一条 assistant message_end 的 text 块即最终答案。
        answer = ""
        for event in events:
            if str(event.get("type") or "") != "message_end":
                continue
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            if message.get("role") != "assistant":
                continue
            text = cls._content_text(message, "text")
            if text:
                answer = text
        if answer:
            return answer
        # 兜底：agent_end.messages 里最后一条 assistant。
        for event in reversed(events):
            if str(event.get("type") or "") == "agent_end":
                messages = event.get("messages")
                if isinstance(messages, list):
                    for message in reversed(messages):
                        if isinstance(message, dict) and message.get("role") == "assistant":
                            text = cls._content_text(message, "text")
                            if text:
                                return text
        return str(stdout or "")

    @classmethod
    def _run_error(cls, events: list[Dict[str, Any]]) -> str | None:
        aborted = False
        for event in events:
            event_type = str(event.get("type") or "")
            if event_type == "agent_settled" and event.get("aborted"):
                aborted = True
            elif event_type == "message_end":
                message = event.get("message") if isinstance(event.get("message"), dict) else {}
                if message.get("role") == "assistant" and message.get("stopReason") in {"error", "aborted"}:
                    return str(message.get("errorMessage") or message.get("stopReason"))
        if aborted:
            return "Pi run aborted"
        return None

    @staticmethod
    def _event_timestamp(value: Any) -> datetime | None:
        if isinstance(value, dict):
            value = value.get("timestamp")
        if value is None:
            return None
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e12 else value
            try:
                return datetime.fromtimestamp(seconds, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    @classmethod
    def _build_trajectory(
        cls,
        events: list[Dict[str, Any]],
        prompt: str,
        started_at: datetime,
        finished_at: datetime,
    ) -> Trajectory:
        trajectory = Trajectory(started_at=started_at, finished_at=finished_at, steps=[])
        previous_at = started_at
        step_id = 1
        for event in events:
            if str(event.get("type") or "") != "turn_end":
                continue
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            text = cls._content_text(message, "text")
            thinking = cls._content_text(message, "thinking")
            tool_calls = cls._tool_calls(message)
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
            tool_results = event.get("toolResults")
            if not isinstance(tool_results, list):
                tool_results = []
            current_at = cls._event_timestamp(message) or previous_at
            trajectory.steps.append(
                StepInfo(
                    step_id=step_id,
                    assistant_content=AssistantContent(
                        content=text,
                        reasoning_content=thinking,
                        tool_calls=tool_calls,
                    ),
                    metric=TrajMetric(
                        prompt_tokens_len=usage.get("input"),
                        completion_tokens_len=usage.get("output"),
                    ),
                    system_prompt="",
                    user_content=prompt if step_id == 1 else "",
                    tools=tool_calls,
                    observation=tool_results,
                    started_at=previous_at,
                    finished_at=current_at,
                )
            )
            previous_at = current_at
            step_id += 1
        return trajectory
