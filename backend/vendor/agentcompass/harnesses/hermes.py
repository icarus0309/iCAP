"""Hermes Agent CLI harness."""

from __future__ import annotations

import json
import logging
import shlex
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Literal

from agentcompass.harnesses.utils.model import build_model_api_config
from agentcompass.runtime import (HARNESSES, AssistantContent, BaseHarness, EnvironmentSession, EnvironmentSpec,
                                  ExecResult, HarnessPlan, ModelSpec, PreparedTask, RunRequest, RunResult, StepInfo,
                                  TaskStatus, Trajectory, TrajMetric)
from agentcompass.runtime.config import (RuntimeHarnessConfig, config_field, normalize_choice, parse_bool,
                                         parse_optional_positive_int, parse_positive_int)
from agentcompass.runtime.llm import ApiProtocol, select_api_protocol
from agentcompass.utils.call import maybe_call

logger = logging.getLogger(__name__)

# 容器内读 state.db 轨迹的脚本（python3 -c 执行，输出 JSON 数组）。
# messages 表字段见 hermes_state.py / 实测 schema：role/content/tool_calls/tool_name/
# timestamp/token_count/finish_reason/reasoning。
_READ_TRAJECTORY_SCRIPT = (
    "import sqlite3, json, sys\n"
    "db = sqlite3.connect(sys.argv[1])\n"
    "db.row_factory = sqlite3.Row\n"
    "sid = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None\n"
    "if sid is None:\n"
    "    r0 = db.execute('SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1').fetchone()\n"
    "    sid = r0['id'] if r0 else None\n"
    "out = {'messages': [], 'session': None, 'session_id': sid}\n"
    "if sid:\n"
    "    for r in db.execute(\"SELECT role, content, tool_calls, tool_name, timestamp, token_count,"
    " finish_reason, reasoning FROM messages WHERE session_id=? ORDER BY id\", (sid,)):\n"
    "        out['messages'].append({'role': r['role'], 'content': r['content'], 'tool_calls': r['tool_calls'],"
    " 'tool_name': r['tool_name'], 'timestamp': r['timestamp'], 'token_count': r['token_count'],"
    " 'finish_reason': r['finish_reason'], 'reasoning': r['reasoning']})\n"
    "    row = db.execute(\"SELECT input_tokens, output_tokens, reasoning_tokens, tool_call_count, api_call_count,"
    " message_count FROM sessions WHERE id=?\", (sid,)).fetchone()\n"
    "    if row:\n"
    "        out['session'] = {'input_tokens': row['input_tokens'], 'output_tokens': row['output_tokens'],"
    " 'reasoning_tokens': row['reasoning_tokens'], 'tool_call_count': row['tool_call_count'],"
    " 'api_call_count': row['api_call_count'], 'message_count': row['message_count']}\n"
    "print(json.dumps(out, ensure_ascii=False))\n"
)


@dataclass(slots=True)
class HermesConfig(RuntimeHarnessConfig):
    """User-facing params for the Hermes Agent CLI harness."""

    binary: str = config_field(default="hermes", description="Executable name or path.")
    install_strategy: Literal["preinstalled", "install_if_missing", "upload"] = config_field(
        default="preinstalled",
        description="How to prepare the executable.",
    )
    install_command: str = config_field(
        default="",
        description="Command run when install_strategy needs to install the tool.",
    )
    upload_src: str = config_field(default="", description="Local file path uploaded by the upload install strategy.")
    upload_dst: str = config_field(
        default="/usr/local/bin/hermes",
        description="Target path used by the upload install strategy.",
    )
    home: str = config_field(
        default="",
        description="HERMES_HOME directory (isolation). Empty derives <workspace>/.hermes-eval.",
    )
    toolsets: str = config_field(
        default="",
        description="Comma-separated toolset allowlist passed via -t; empty = default toolsets.",
    )
    max_turns: int = config_field(default=90, description="Maximum tool-calling iterations (--max-turns).")
    yolo: bool = config_field(default=True, description="Pass --yolo to bypass dangerous-command approvals.")
    ignore_rules: bool = config_field(
        default=True,
        description="Pass --ignore-rules to skip AGENTS.md/SOUL.md/memory/preloaded skills.",
    )
    timeout: int | None = config_field(default=None, description="Task wall-clock timeout in seconds.")
    prompt_append: str = config_field(default="", description="Text appended to the task prompt.")

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
        self.home = str(self.home or "").strip()
        self.toolsets = str(self.toolsets or "").strip()
        self.max_turns = parse_positive_int(self.max_turns, "max_turns")
        self.yolo = parse_bool(self.yolo, "yolo")
        self.ignore_rules = parse_bool(self.ignore_rules, "ignore_rules")
        self.timeout = parse_optional_positive_int(self.timeout, "timeout")
        self.prompt_append = str(self.prompt_append or "").strip()


@dataclass(slots=True)
class HermesHarnessPlan(HarnessPlan):
    binary: str = "hermes"
    install_strategy: Literal["preinstalled", "install_if_missing", "upload"] = "preinstalled"
    install_command: str = ""
    upload_src: str = ""
    upload_dst: str = "/usr/local/bin/hermes"
    home: str = ""
    toolsets: str = ""
    max_turns: int = 90
    yolo: bool = True
    ignore_rules: bool = True
    timeout: int | None = None
    prompt_append: str = ""


@HARNESSES.register()
class HermesHarness(BaseHarness):
    """Run the Hermes Agent CLI non-interactively against a prepared workspace."""

    id = "hermes"
    parallel_attempts_safe = True
    description = "Runs the Hermes Agent CLI (Nous Research) as a non-interactive coding agent for prepared workspaces such as SWE-bench (official website: https://github.com/NousResearch/hermes-agent)."
    config_class = HermesConfig
    plan_class = HermesHarnessPlan

    def supports(self, environment: EnvironmentSpec, model: ModelSpec) -> bool:
        protocols = model.api_protocols
        preferred = [ApiProtocol.OPENAI_CHAT, ApiProtocol.OPENAI_RESPONSES, ApiProtocol.ANTHROPIC]
        if protocols:
            select_api_protocol(model.api_protocol, preferred, component=self.id)
        return True

    async def start_session(self, env: EnvironmentSession, req: RunRequest,
                            plan: HermesHarnessPlan) -> Dict[str, Any]:
        binary = str(plan.binary)
        install_strategy = str(plan.install_strategy or "preinstalled")
        if install_strategy == "install_if_missing":
            check_result = await env.exec(f"command -v {shlex.quote(binary)} >/dev/null 2>&1", shell=True)
            binary_exists = check_result.returncode == 0
            if not binary_exists:
                if not plan.install_command:
                    raise RuntimeError("Hermes install_command is not specified")
                install_result = await env.exec(str(plan.install_command), shell=True)
                if install_result.returncode != 0:
                    raise RuntimeError(f"Hermes install failed: {install_result.stderr.strip()}")
        elif install_strategy == "upload":
            if not plan.upload_src:
                raise RuntimeError("Hermes `upload_src` is not specified.")
            await env.upload(plan.upload_src, plan.upload_dst)
            await env.exec(["chmod", "+x", plan.upload_dst])
            binary = str(plan.upload_dst)

        if not binary.startswith("/"):
            resolve_result = await env.exec(f"command -v {shlex.quote(binary)}", shell=True)
            if resolve_result.returncode != 0:
                raise RuntimeError(f"Hermes binary resolution failed: {binary}")
            binary = resolve_result.stdout.strip()

        version_result = await env.exec([binary, "--version"])
        if version_result.returncode != 0:
            error = version_result.stderr.strip() or version_result.stdout.strip()
            raise RuntimeError(f"Hermes validation failed: returncode={version_result.returncode}; {error}")
        version = version_result.stdout.strip().splitlines()[0] if version_result.stdout.strip() else ""
        logger.info("Hermes installation verified | binary=%s | version=%s", binary, version)

        return {
            "env": env,
            "binary": binary,
            "workspace_root": getattr(env, "default_workspace_root", None) or getattr(env, "workspace", None)
            or "workspace/",
            "model_config": build_model_api_config(req.model),
        }

    @staticmethod
    def _build_prompt(prompt_text: str, answer_spec: str | None, output_files: list[Any],
                      prompt_append: str = "") -> str:
        lines = [prompt_text.rstrip()]
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

    @staticmethod
    def _yaml_escape(value: str) -> str:
        # 单行字符串，只需处理双引号与反斜杠；冒号/井号只在无引号时才需转义。
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    @classmethod
    async def _write_hermes_config(
        cls,
        env: EnvironmentSession,
        home: str,
        req: RunRequest,
        model_config: Dict[str, Any],
    ) -> str:
        base_url = str(model_config.get("base_url") or "").strip()
        api_key = str(model_config.get("api_key") or "").strip()
        if not base_url:
            raise RuntimeError("Hermes requires an OpenAI-compatible base url")
        if not api_key:
            raise RuntimeError("Hermes requires an api key")

        esc = cls._yaml_escape
        lines = [
            "model:",
            f"  default: \"{esc(req.model.id)}\"",
            "  provider: custom",
            f"  base_url: \"{esc(base_url)}\"",
            f"  api_key: \"{esc(api_key)}\"",
            "# tirith 未安装时会往 stdout 打 warning，污染 final_answer 解析；显式关闭。",
            "security:",
            "  tirith_enabled: false",
            "",
        ]
        config_path = str(Path(home) / "config.yaml")
        await env.write_text(config_path, "\n".join(lines))
        return config_path

    async def _read_state_db(self, env: EnvironmentSession, home: str, session_id: str) -> Dict[str, Any] | None:
        """容器内用 python3 读 state.db，返回 {messages, session} 或 None。"""
        state_db = str(Path(home) / "state.db")
        try:
            result = await env.exec(["python3", "-c", _READ_TRAJECTORY_SCRIPT, state_db, session_id])
        except Exception as exc:
            logger.warning("Hermes state.db read failed | session=%s | error=%s", session_id, exc)
            return None
        if result.returncode != 0:
            logger.warning("Hermes state.db read failed | session=%s | stderr=%s", session_id, result.stderr.strip())
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    @staticmethod
    def _parse_tool_calls(raw: Any) -> list[Dict[str, Any]]:
        if not raw:
            return []
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return []
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        return []

    def _build_trajectory(self, state: Dict[str, Any], prompt: str, started_at: datetime,
                          finished_at: datetime) -> Trajectory:
        messages = state.get("messages") or []
        session_meta = state.get("session") or {}
        trajectory = Trajectory(started_at=started_at, finished_at=finished_at, steps=[])
        previous_at = started_at
        step_id = 1
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            content = str(msg.get("content") or "")
            reasoning = str(msg.get("reasoning") or "")
            tool_calls = self._parse_tool_calls(msg.get("tool_calls"))
            ts = msg.get("timestamp")
            current_at = previous_at
            if isinstance(ts, (int, float)):
                try:
                    current_at = datetime.fromtimestamp(ts)
                except (OverflowError, OSError, ValueError):
                    current_at = previous_at
            trajectory.steps.append(
                StepInfo(
                    step_id=step_id,
                    assistant_content=AssistantContent(
                        content=content,
                        reasoning_content=reasoning,
                        tool_calls=tool_calls,
                    ),
                    metric=TrajMetric(
                        prompt_tokens_len=session_meta.get("input_tokens"),
                        completion_tokens_len=session_meta.get("output_tokens"),
                    ),
                    system_prompt="",
                    user_content=prompt if step_id == 1 else "",
                    tools=tool_calls,
                    observation=[],
                    started_at=previous_at,
                    finished_at=current_at,
                ))
            previous_at = current_at
            step_id += 1
        return trajectory

    @staticmethod
    def _clean_stdout(stdout: str) -> str:
        """去掉混入 stdout 的启动 warning 行（如 tirith），只留 final_response。"""
        lines = str(stdout or "").splitlines()
        cleaned = [line for line in lines if "tirith" not in line.lower() and "⚠" not in line]
        return "\n".join(cleaned).strip()

    @staticmethod
    def _extract_session_id(stderr: str) -> str:
        match = None
        for line in str(stderr or "").splitlines():
            if line.strip().startswith("session_id:"):
                match = line.split(":", 1)[1].strip()
                break
        return match or ""

    async def run_task(
        self,
        session: Dict[str, Any],
        prepared: PreparedTask,
        req: RunRequest,
        plan: HermesHarnessPlan,
    ) -> RunResult:
        env: EnvironmentSession = session["env"]
        prompt_text = str(prepared.input.prompt or "").strip()
        if not prompt_text:
            raise ValueError("hermes requires prepared.input.prompt")
        workspace = str(prepared.input.workspace or "").strip()
        if not workspace:
            workspace = self._build_default_workspace(str(session.get("workspace_root") or "workspace/"), req, prepared)
        mkdir_result = await env.exec(["mkdir", "-p", workspace])
        if mkdir_result.returncode != 0:
            raise RuntimeError(f"Failed to create Hermes workspace {workspace}: {mkdir_result.stderr.strip()}")

        home = str(plan.home or str(Path(workspace) / ".hermes-eval"))
        await env.exec(["mkdir", "-p", home])
        config_path = await self._write_hermes_config(env, home, req, session["model_config"])

        requested_files = list(prepared.output.files)
        prompt = self._build_prompt(prompt_text, prepared.output.answer, requested_files, plan.prompt_append)

        argv = [session["binary"], "chat", "-q", prompt, "-Q"]
        if plan.ignore_rules:
            argv.append("--ignore-rules")
        if plan.yolo:
            argv.append("--yolo")
        if plan.max_turns:
            argv.extend(["--max-turns", str(plan.max_turns)])
        if plan.toolsets:
            argv.extend(["-t", plan.toolsets])
        command_text = shlex.join(argv)
        exec_env = {"HERMES_HOME": home}

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
            logger.exception("Hermes command execution failed | task=%s", prepared.task_id)
            run_result = ExecResult(returncode=-1, stderr=error)
        finished_at = datetime.now().astimezone()

        final_answer = self._clean_stdout(run_result.stdout)
        session_id = self._extract_session_id(run_result.stderr)

        # 超时(SIGKILL)时 stderr 拿不到 session_id，但 state.db 里已有轨迹；
        # _read_state_db 在 session_id 为空时会自动查最新 session，故不跳过。
        state = await self._read_state_db(env, home, session_id)
        if not session_id and state and state.get("session_id"):
            session_id = state["session_id"]
        trajectory = self._build_trajectory(state or {}, prompt, started_at, finished_at)

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
            "hermes_home": home,
            "hermes_config_path": config_path,
            "session_id": session_id,
            "model_base_url": session["model_config"].get("base_url"),
            "model_api_protocol": session["model_config"].get("api_protocol"),
            "requested_output_files": [output_file.path for output_file in requested_files],
            "resolved_output_files": resolved_output_paths,
        }
        if state and state.get("session"):
            metrics["session_meta"] = state["session"]
        if missing_files:
            metrics["missing_output_files"] = missing_files

        error = None
        if run_result.timed_out:
            error = f"Hermes command timed out after {plan.timeout} seconds"
        elif run_result.returncode != 0:
            error = run_result.stderr.strip() or f"Hermes command failed: returncode={run_result.returncode}"
        elif missing_files:
            error = "Requested output files missing: " + ", ".join(missing_files)

        return RunResult(
            task_id=prepared.task_id,
            status=TaskStatus.RUN_ERROR if error else TaskStatus.COMPLETED,
            final_answer=final_answer,
            trajectory=trajectory,
            telemetry=metrics,
            artifacts={
                "file": collected_files,
                "state_db": state or {},
            },
            error=error,
        )
