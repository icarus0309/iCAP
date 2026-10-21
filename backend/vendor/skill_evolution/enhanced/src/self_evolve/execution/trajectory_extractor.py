"""Extract raw execution trajectories from normalized session messages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from ..schemas import SessionRecord, TrajectoryStep, to_plain_dict


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("input")
                if text:
                    parts.append(str(text))
                elif item.get("type"):
                    parts.append(f"[{item.get('type')}]")
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _iter_tool_calls(message: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for key in ("tool_calls", "toolCalls", "function_calls", "actions"):
        calls = message.get(key)
        if isinstance(calls, list):
            for call in calls:
                if isinstance(call, dict):
                    yield call
    content = message.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and str(item.get("type", "")).lower() in {"tool_call", "function_call"}:
                yield item


def _tool_name(call: Dict[str, Any]) -> str:
    fn = call.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name") or "")
    return str(call.get("name") or call.get("tool_name") or call.get("tool") or "")


def _tool_input(call: Dict[str, Any]) -> Any:
    fn = call.get("function")
    if isinstance(fn, dict) and "arguments" in fn:
        return fn.get("arguments")
    for key in ("arguments", "args", "input", "parameters"):
        if key in call:
            return call.get(key)
    return None


class TrajectoryExtractor:
    """Convert Hermes messages into a compact trajectory."""

    def extract(self, session: SessionRecord, output_path: Path | None = None) -> List[TrajectoryStep]:
        steps: List[TrajectoryStep] = []
        for message in session.messages:
            role = str(message.get("role", "unknown")).lower()
            content = _content_to_text(message.get("content"))
            action_type = "message"
            tool_name = ""
            tool_output = None
            success = None

            if role in {"tool", "tool_result", "function"}:
                action_type = "tool_result"
                tool_name = str(message.get("name") or message.get("tool_name") or "")
                tool_output = message.get("content")
                if "success" in message:
                    success = bool(message.get("success"))
                elif "error" in message:
                    success = not bool(message.get("error"))
            elif list(_iter_tool_calls(message)):
                action_type = "assistant_tool_request"
            elif role == "assistant":
                action_type = "assistant_response"
            elif role == "user":
                action_type = "user_request"

            steps.append(
                TrajectoryStep(
                    index=len(steps),
                    role=role,
                    action_type=action_type,
                    content=content,
                    tool_name=tool_name,
                    tool_output=tool_output,
                    success=success,
                    evidence={"message_keys": sorted(message.keys())},
                )
            )

            for call in _iter_tool_calls(message):
                steps.append(
                    TrajectoryStep(
                        index=len(steps),
                        role=role,
                        action_type="tool_call",
                        content="",
                        tool_name=_tool_name(call),
                        tool_input=_tool_input(call),
                        success=None,
                        evidence={"call": call},
                    )
                )

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps([to_plain_dict(step) for step in steps], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        return steps
