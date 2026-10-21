"""Small JSONL diagnostics logger for self-evolve runs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


class DiagnosticLogger:
    """Write concise, key-safe diagnostics to a JSONL log file."""

    def __init__(self, log_path: Optional[Path] = None):
        self.log_path = Path(log_path) if log_path else None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def bind(self, log_path: Path) -> "DiagnosticLogger":
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        return self

    def info(self, event: str, **fields: Any) -> None:
        self._write("INFO", event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._write("WARNING", event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._write("ERROR", event, fields)

    def _write(self, level: str, event: str, fields: Dict[str, Any]) -> None:
        if not self.log_path:
            return
        record = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(),
            "level": level,
            "event": event,
            **fields,
        }
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
