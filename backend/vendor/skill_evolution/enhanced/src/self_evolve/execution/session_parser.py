"""Parse Hermes exported sessions and preserve the messages field."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..schemas import HermesRunResult, SessionRecord, to_plain_dict


SESSION_ID_PATTERNS = [
    re.compile(r"session[_ -]?id[:=]\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE),
    re.compile(r"Session\s+([A-Za-z0-9_.:-]{8,})", re.IGNORECASE),
]


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            parsed = {"text": line}
        if isinstance(parsed, dict):
            records.append(parsed)
        else:
            records.append({"value": parsed})
    return records


def _extract_messages_from_value(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("messages", "conversation", "turns", "events"):
            nested = _extract_messages_from_value(value.get(key))
            if nested:
                return nested
    return []


def extract_messages(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Find and return the first non-empty messages-like array."""
    collected: List[Dict[str, Any]] = []
    records_list = list(records)
    for record in reversed(records_list):
        direct = _extract_messages_from_value(record.get("messages"))
        if direct:
            return direct
        nested = _extract_messages_from_value(record)
        if nested:
            collected.extend(nested)
    return list(reversed(collected))


def extract_session_id(text: str, records: Iterable[Dict[str, Any]]) -> str:
    for record in reversed(list(records)):
        for key in ("session_id", "sessionId", "id"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for pattern in SESSION_ID_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return match.group(1)
    return ""


def _final_text_from_messages(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages):
        role = str(message.get("role", "")).lower()
        if role not in {"assistant", "final"}:
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        parts.append(str(text))
                elif isinstance(item, str):
                    parts.append(item)
            if parts:
                return "\n".join(parts).strip()
    return ""


class SessionParser:
    """Normalize Hermes session exports."""

    def parse(
        self,
        *,
        case_id: str,
        run_result: HermesRunResult,
        export_path: Optional[Path],
        output_path: Optional[Path] = None,
    ) -> SessionRecord:
        records = _read_jsonl(export_path) if export_path else []
        raw_path = Path(run_result.raw_session_path)
        if not records and raw_path.exists():
            records = _read_jsonl(raw_path)

        messages = extract_messages(records)
        session_id = run_result.session_id or extract_session_id(run_result.stdout + "\n" + run_result.stderr, records)
        final_output = _final_text_from_messages(messages) or run_result.final_output or run_result.stdout.strip()

        session = SessionRecord(
            case_id=case_id,
            session_id=session_id,
            source=run_result.source,
            messages=messages,
            raw_records=records,
            final_output=final_output,
            metadata={
                "returncode": run_result.returncode,
                "command": run_result.command,
                "hermes_home": run_result.hermes_home,
                "error": run_result.error,
            },
        )

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(to_plain_dict(session), ensure_ascii=False, indent=2), encoding="utf-8")

        return session
