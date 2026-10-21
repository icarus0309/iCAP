"""Direct runner — execute a SKILL as a single LLM call (no Hermes agent).

direct 模式：
- SKILL 全文作为 system prompt；
- 用例的用户输入 messages 数组（dataset input type=="json"）作为 user prompt；
- 调用一次执行模型，返回文本即本次执行结果（例如记忆抽取场景的 memory_list）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..diagnostics import DiagnosticLogger
from ..schemas import HermesRunResult, to_plain_dict


class DirectRunner:
    """Single-call SKILL executor used when ``SKILL_EXEC_MODE=direct``."""

    def __init__(
        self,
        model_client: Any,
        run_dir: Path,
        source: str,
        logger: Optional[DiagnosticLogger] = None,
    ):
        self.model_client = model_client
        self.run_dir = Path(run_dir).resolve()
        self.source = source
        self.logger = logger or DiagnosticLogger()

    def run_case(
        self,
        *,
        case_id: str,
        skill_path: Path,
        input_messages: Optional[List[Dict[str, Any]]] = None,
        case_dir: Path,
        pre_condition: Optional[Any] = None,
    ) -> HermesRunResult:
        case_dir.mkdir(parents=True, exist_ok=True)

        try:
            skill_text = skill_path.read_text(encoding="utf-8")
        except OSError as exc:
            skill_text = ""
            self.logger.warning("direct_skill_read_failed", case_id=case_id, error=str(exc))

        input_messages = input_messages or []
        messages: List[Dict[str, str]] = [{"role": "system", "content": skill_text}]
        for msg in input_messages:
            if isinstance(msg, dict):
                role = str(msg.get("role", "user")).lower() or "user"
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = json.dumps(content, ensure_ascii=False)
                messages.append({"role": role, "content": str(content)})

        model_name = getattr(self.model_client.config, "model_name", "?") if self.model_client else "?"
        command = ["direct-llm", str(model_name)]

        returncode = 0
        error = ""
        output = ""
        if not self.model_client:
            returncode = 1
            error = "未配置执行模型（EXEC_MODEL/EXEC_API_BASE/EXEC_API_KEY）"
        else:
            try:
                output = self.model_client.generate(messages) or ""
            except Exception as exc:  # pragma: no cover - defensive
                returncode = 1
                error = f"执行模型调用失败: {exc}"
            if not output.strip() and not error:
                error = "执行模型返回空响应"

        raw_path = (case_dir / "session.raw.jsonl").resolve()
        raw_records = [
            {
                "case_id": case_id,
                "command": command,
                "returncode": returncode,
                "source": self.source,
                "messages": input_messages + [{"role": "assistant", "content": output}],
            }
        ]
        raw_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in raw_records) + "\n",
            encoding="utf-8",
        )

        result = HermesRunResult(
            case_id=case_id,
            command=command,
            returncode=returncode,
            stdout=output,
            stderr=error,
            source=self.source,
            hermes_home="",
            raw_session_path=str(raw_path),
            exported_session_path=None,
            session_id="",
            final_output=output,
            error=error,
        )

        self.logger.info(
            "direct_case_complete",
            case_id=case_id,
            returncode=returncode,
            output_chars=len(output),
            error=error,
        )

        try:
            (case_dir / "run_result.json").write_text(
                json.dumps(to_plain_dict(result), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            self.logger.warning("direct_run_result_checkpoint_failed", case_id=case_id, error=str(exc))

        return result
