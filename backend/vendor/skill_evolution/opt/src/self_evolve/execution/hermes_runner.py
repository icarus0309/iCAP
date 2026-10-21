"""Hermes CLI adapter used by the self-evolution flow."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..diagnostics import DiagnosticLogger
from .executor import ExecutorConfig, create_executor
from .pre_condition import PreConditionHook, NoOpPreConditionHook
from ..schemas import HermesRunResult, to_plain_dict
from .session_parser import extract_session_id


@dataclass
class HermesCliConfig:
    hermes_bin: str = "hermes"
    hermes_model: Optional[str] = None
    hermes_provider: Optional[str] = None
    hermes_timeout: int = 180
    hermes_env: Dict[str, str] = field(default_factory=dict)
    source_hermes_home: Optional[str] = None
    seed_user_config: bool = True
    toolsets: Optional[str] = None
    max_turns: Optional[int] = None
    ignore_user_config: bool = False
    ignore_rules: bool = False
    yolo: bool = False
    # Executor configuration for cross-platform support
    executor_config: Optional[ExecutorConfig] = None
    # Pre-condition hook (injected by orchestrator)
    pre_condition_hook: PreConditionHook = field(default_factory=NoOpPreConditionHook)


def _safe_skill_name(skill_path: Path) -> str:
    name = _read_skill_frontmatter_name(skill_path) or skill_path.stem or "skill"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "skill"


def _read_skill_frontmatter_name(skill_path: Path) -> str:
    try:
        text = skill_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    frontmatter = text[3:end]
    for line in frontmatter.splitlines():
        if line.strip().startswith("name:"):
            value = line.split(":", 1)[1].strip().strip('"').strip("'")
            return value
    return ""


class HermesCliRunner:
    """Run Hermes in an isolated HERMES_HOME and export its session JSONL."""

    def __init__(self, config: HermesCliConfig, run_dir: Path, source: str, logger: Optional[DiagnosticLogger] = None):
        self.config = config
        self.run_dir = Path(run_dir).resolve()
        self.source = source
        self.logger = logger or DiagnosticLogger()
        self.hermes_home = self.run_dir / ".hermes"
        self.hermes_home.mkdir(parents=True, exist_ok=True)

        # Initialize executor
        if self.config.executor_config is None:
            self.config.executor_config = ExecutorConfig()
        self.executor = create_executor(self.config.executor_config)

        # Pre-condition hook
        self._pre_condition_hook = self.config.pre_condition_hook

        self._seed_hermes_home()

    def _seed_hermes_home(self) -> None:
        """Copy user Hermes runtime inputs into the isolated home without editing them."""
        if not self.config.seed_user_config or self.config.ignore_user_config:
            self.logger.info(
                "hermes_home_seed_skipped",
                hermes_home=str(self.hermes_home),
                reason="disabled_or_ignore_user_config",
            )
            return

        source_home = Path(
            self.config.source_hermes_home
            or os.environ.get("HERMES_HOME")
            or (Path.home() / ".hermes")
        ).expanduser()
        if not source_home.exists():
            self.logger.info(
                "hermes_home_seed_skipped",
                hermes_home=str(self.hermes_home),
                source_home=str(source_home),
                reason="source_missing",
            )
            return
        try:
            if source_home.resolve() == self.hermes_home.resolve():
                return
        except OSError:
            pass

        copied: List[str] = []
        for name in ("config.yaml", "models_dev_cache.json", "channel_directory.json", "SOUL.md"):
            src = source_home / name
            dst = self.hermes_home / name
            if src.is_file() and not dst.exists():
                shutil.copy2(src, dst)
                copied.append(name)

        for name in ("memories", "hooks"):
            src = source_home / name
            dst = self.hermes_home / name
            if src.is_dir() and not dst.exists():
                shutil.copytree(src, dst, symlinks=True)
                copied.append(f"{name}/")

        self.logger.info(
            "hermes_home_seeded",
            hermes_home=str(self.hermes_home),
            source_home=str(source_home),
            copied=copied,
            skipped_sensitive=[".env", "auth.json", "auth/"],
        )

    @staticmethod
    def _copy_skill_package(skill_path: Path, skill_dir: Path) -> None:
        """Install a candidate SKILL.md together with adjacent runtime resources."""
        resource_root = skill_path.parent if skill_path.name == "SKILL.md" else None
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        skill_dir.mkdir(parents=True, exist_ok=True)
        if resource_root and resource_root.exists():
            shutil.copytree(
                resource_root,
                skill_dir,
                dirs_exist_ok=True,
                ignore=HermesCliRunner._ignore_skill_entries,
            )
        shutil.copy2(skill_path, skill_dir / "SKILL.md")

    @staticmethod
    def _ignore_skill_entries(_directory: str, names: List[str]) -> List[str]:
        ignored = {
            ".git",
            ".hg",
            ".svn",
            ".DS_Store",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".hermes",
            "node_modules",
        }
        return [name for name in names if name in ignored or name.endswith((".pyc", ".pyo"))]

    def prepare_skill(self, skill_path: Path) -> str:
        """Copy SKILL.md into the isolated Hermes home and return skill name."""
        skill_path = Path(skill_path)
        skill_name = _safe_skill_name(skill_path)
        skill_dir = self.hermes_home / "skills" / skill_name
        self._copy_skill_package(skill_path, skill_dir)
        self.logger.info(
            "hermes_skill_prepared",
            skill_name=skill_name,
            skill_path=str(skill_path),
            install_dir=str(skill_dir),
        )
        return skill_name

    def run_case(
        self,
        *,
        case_id: str,
        skill_path: Path,
        input_text: str,
        case_dir: Path,
        pre_condition: Optional[Any] = None,
    ) -> HermesRunResult:
        case_dir.mkdir(parents=True, exist_ok=True)
        skill_name = self.prepare_skill(skill_path)
        raw_path = (case_dir / "session.raw.jsonl").resolve()
        export_path = (case_dir / "session.export.jsonl").resolve()

        # ── Pre-condition setup ──────────────────────────────────────
        if pre_condition:
            self._pre_condition_hook.setup(pre_condition)

        command = [
            self.config.hermes_bin,
            "chat",
            "-q",
            input_text,
            "-Q",
            "--source",
            self.source,
            "--skills",
            skill_name,
        ]
        if self.config.hermes_model:
            command.extend(["--model", self.config.hermes_model])
        if self.config.hermes_provider:
            command.extend(["--provider", self.config.hermes_provider])
        if self.config.toolsets:
            command.extend(["--toolsets", self.config.toolsets])
        if self.config.max_turns:
            command.extend(["--max-turns", str(self.config.max_turns)])
        if self.config.ignore_user_config:
            command.append("--ignore-user-config")
        if self.config.ignore_rules:
            command.append("--ignore-rules")
        if self.config.yolo:
            command.append("--yolo")

        env = os.environ.copy()
        env["HERMES_HOME"] = str(self.hermes_home)
        self._load_source_env_into_process_env(env)
        env.update(self.config.hermes_env)
        self.logger.info(
            "hermes_case_start",
            case_id=case_id,
            skill_name=skill_name,
            hermes_home=str(self.hermes_home),
            source=self.source,
            command=[part if part not in self.config.hermes_env.values() else "<redacted>" for part in command],
            extra_env_keys=sorted(self.config.hermes_env.keys()),
        )

        try:
            completed = self.executor.run(
                command,
                cwd=self.run_dir,
                env=env,
                timeout=self.config.hermes_timeout,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            returncode = completed.returncode
            error = "" if returncode == 0 else f"hermes exited with code {returncode}"
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
            stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
            returncode = 124
            error = f"hermes timed out after {self.config.hermes_timeout}s"
            self.logger.error("hermes_case_timeout", case_id=case_id, timeout=self.config.hermes_timeout)

        raw_records = [
            {
                "case_id": case_id,
                "command": command,
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "source": self.source,
                "hermes_home": str(self.hermes_home),
            }
        ]
        raw_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in raw_records) + "\n", encoding="utf-8")
        self.logger.info(
            "hermes_case_complete",
            case_id=case_id,
            returncode=returncode,
            stdout_chars=len(stdout),
            stderr_chars=len(stderr),
            raw_session_path=str(raw_path),
            error=error,
        )

        export_error = ""
        try:
            export_cmd = [self.config.hermes_bin, "sessions", "export", "--source", self.source, str(export_path)]
            export_completed = self.executor.run(
                export_cmd,
                cwd=self.run_dir,
                env=env,
                timeout=max(30, min(self.config.hermes_timeout, 120)),
            )
            if export_completed.returncode != 0:
                export_error = (export_completed.stderr or export_completed.stdout or "").strip()
                self.logger.warning(
                    "hermes_session_export_failed",
                    case_id=case_id,
                    returncode=export_completed.returncode,
                    message=export_error[:500],
                )
        except Exception as exc:  # pragma: no cover - defensive
            export_error = str(exc)
            self.logger.error("hermes_session_export_exception", case_id=case_id, error=str(exc))

        exported_records = []
        if export_path.exists():
            for line in export_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    exported_records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        session_id = extract_session_id(stdout + "\n" + stderr, exported_records)
        if session_id and not exported_records:
            id_export_error = self._export_session_by_id(env=env, session_id=session_id, export_path=export_path, case_id=case_id)
            if id_export_error:
                export_error = (export_error + "; " if export_error else "") + id_export_error
            exported_records = []
            if export_path.exists():
                for line in export_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        exported_records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            session_id = extract_session_id(stdout + "\n" + stderr, exported_records) or session_id
        final_output = stdout.strip()
        if export_error:
            error = (error + "; " if error else "") + f"session export failed: {export_error}"
        self.logger.info(
            "hermes_session_export_complete",
            case_id=case_id,
            exported_session_path=str(export_path) if export_path.exists() else "",
            exported_records=len(exported_records),
            session_id=session_id,
        )
        self._cleanup_sensitive_hermes_state(case_id=case_id)

        # ── Pre-condition teardown ────────────────────────────────────
        if pre_condition:
            self._pre_condition_hook.teardown()

        result = HermesRunResult(
            case_id=case_id,
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            source=self.source,
            hermes_home=str(self.hermes_home),
            raw_session_path=str(raw_path),
            exported_session_path=str(export_path) if export_path.exists() else None,
            session_id=session_id,
            final_output=final_output,
            error=error,
        )
        # 落盘续跑检查点：后续 --resume 时跳过已执行用例。
        try:
            (case_dir / "run_result.json").write_text(
                json.dumps(to_plain_dict(result), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            self.logger.warning("run_result_checkpoint_failed", case_id=case_id, error=str(exc))
        return result

    def _load_source_env_into_process_env(self, env: Dict[str, str]) -> None:
        """Load source Hermes .env values into the child env without writing them to output."""
        if not self.config.seed_user_config or self.config.ignore_user_config:
            return
        source_home = Path(
            self.config.source_hermes_home
            or os.environ.get("HERMES_HOME")
            or (Path.home() / ".hermes")
        ).expanduser()
        env_path = source_home / ".env"
        if not env_path.is_file():
            return
        loaded_keys: List[str] = []
        try:
            for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if not key or key in env:
                    continue
                env[key] = value
                loaded_keys.append(key)
        except OSError as exc:
            self.logger.warning("hermes_source_env_load_failed", source_home=str(source_home), error=str(exc))
            return
        if loaded_keys:
            self.logger.info("hermes_source_env_loaded", source_home=str(source_home), env_keys=sorted(loaded_keys))

    def _cleanup_sensitive_hermes_state(self, *, case_id: str) -> None:
        """Remove credentials that Hermes may materialize in the isolated run directory."""
        removed: List[str] = []
        for relative in (".env", "auth.json", "auth.lock"):
            path = self.hermes_home / relative
            if path.exists():
                try:
                    path.unlink()
                    removed.append(relative)
                except OSError as exc:
                    self.logger.warning(
                        "hermes_sensitive_cleanup_failed",
                        case_id=case_id,
                        path=str(path),
                        error=str(exc),
                    )
        auth_dir = self.hermes_home / "auth"
        if auth_dir.exists():
            try:
                shutil.rmtree(auth_dir)
                removed.append("auth/")
            except OSError as exc:
                self.logger.warning(
                    "hermes_sensitive_cleanup_failed",
                    case_id=case_id,
                    path=str(auth_dir),
                    error=str(exc),
                )
        if removed:
            self.logger.info("hermes_sensitive_state_cleaned", case_id=case_id, removed=removed)

    def _export_session_by_id(
        self,
        *,
        env: Dict[str, str],
        session_id: str,
        export_path: Path,
        case_id: str,
    ) -> str:
        """Fallback export for Hermes versions that persist source as cli in oneshot mode."""
        export_cmd = [
            self.config.hermes_bin,
            "sessions",
            "export",
            "--session-id",
            session_id,
            str(export_path),
        ]
        self.logger.info("hermes_session_export_by_id_start", case_id=case_id, session_id=session_id)
        try:
            export_completed = self.executor.run(
                export_cmd,
                cwd=self.run_dir,
                env=env,
                timeout=max(30, min(self.config.hermes_timeout, 120)),
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.error("hermes_session_export_by_id_exception", case_id=case_id, session_id=session_id, error=str(exc))
            return str(exc)

        if export_completed.returncode != 0:
            message = (export_completed.stderr or export_completed.stdout or "").strip()
            self.logger.warning(
                "hermes_session_export_by_id_failed",
                case_id=case_id,
                session_id=session_id,
                returncode=export_completed.returncode,
                message=message[:500],
            )
            return message

        self.logger.info("hermes_session_export_by_id_complete", case_id=case_id, session_id=session_id)
        return ""
