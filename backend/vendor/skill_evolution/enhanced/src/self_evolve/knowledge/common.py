"""Shared non-patch helpers for the knowledge-extraction pipeline.

Mirrors ``skill_distill_multi_dim/src/trace2skill/common.py``'s non-patch utilities
(``read_json`` / ``write_json`` / ``complete_json_with_retry``), adapted to evo's
``ModelClient`` (``generate_with_system`` + ``complete_json``). Patch/edit machinery
is intentionally absent.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ...model_client import QuotaExceededError

DEFAULT_KNOWLEDGE_LLM_MAX_TOKENS = 16_384
KNOWLEDGE_LLM_MAX_RETRIES = 3
KNOWLEDGE_LLM_RETRY_SLEEP_SECONDS = 1.0


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def is_retryable_knowledge_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "429",
            "500",
            "502",
            "503",
            "504",
            "could not reach",
            "connection reset",
            "timed out",
            "timeout",
            "empty",
            "json",
            "空响应",
        )
    )


def complete_json_with_retry(
    client: Any,
    system: str,
    user: str,
    *,
    max_tokens: int = DEFAULT_KNOWLEDGE_LLM_MAX_TOKENS,
    max_retries: int = KNOWLEDGE_LLM_MAX_RETRIES,
    retry_sleep_seconds: float = KNOWLEDGE_LLM_RETRY_SLEEP_SECONDS,
    retry_hint: str | None = None,
) -> dict[str, Any]:
    """Call ``client.complete_json`` with fixed-interval retries for transient failures.

    ``client`` is evo's ``ModelClient`` (provides ``complete_json``); a ``retry_hint``
    (if supplied) is appended on subsequent attempts so the model can correct its shape.
    """
    attempts = max(1, max_retries + 1)
    last_error = ""
    for attempt in range(1, attempts + 1):
        current_user = user
        if attempt > 1 and retry_hint and last_error:
            current_user = f"{user}\n\n{retry_hint}\n原始错误：{last_error[:700]}"
        try:
            return client.complete_json(system, current_user, max_tokens=max_tokens)
        except QuotaExceededError:
            raise
        except Exception as exc:  # noqa: BLE001 - retry any transient failure
            last_error = str(exc)
            if attempt >= attempts or not is_retryable_knowledge_error(exc):
                raise
            print(
                f"知识提取 LLM 调用失败 (attempt {attempt}/{attempts})，"
                f"等待 {retry_sleep_seconds}s 后重试: {last_error[:300]}",
                flush=True,
            )
            time.sleep(retry_sleep_seconds)
    raise RuntimeError("知识提取 LLM 重试循环耗尽")
