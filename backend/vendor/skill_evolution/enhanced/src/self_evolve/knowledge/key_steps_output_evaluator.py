"""evo ``TrajectoryStep`` → distill evaluation trajectory 形状适配。

知识提取的对齐逻辑（``key_steps_judge``）消费 distill 的 evaluation trajectory：
一组有序事件，每个事件含 ``step``（1 起整数）、``event_type``、``role``、``action``、
``content``、``tool_calls``、``tool_call_id``、``finish_reason``、``success``。

evo 的 ``TrajectoryStep`` 已经是归一化、紧凑的步骤，这里做一次字段名/语义映射即可，
无需 distill ``build_evaluation_trajectory`` 里的路径脱敏 / 终端命令解析等针对原始
Hermes session 的压缩逻辑。
"""

from __future__ import annotations

from typing import Any, List

from ..schemas import TrajectoryStep

EVALUATION_TRAJECTORY_SCHEMA = "1.1"

# action_type -> distill event_type（用于对齐启发式与 LLM 映射的语义提示）
_ACTION_EVENT_TYPE = {
    "user_request": "query",
    "assistant_response": "assistant",
    "assistant_tool_request": "assistant",
    "tool_call": "tool_call",
    "tool_result": "observation",
    "message": "message",
}


def _step_to_event(step: TrajectoryStep) -> dict[str, Any]:
    action_type = step.action_type or "message"
    event_type = _ACTION_EVENT_TYPE.get(action_type, "message")

    tool_calls: list[dict[str, Any]] = []
    content = step.content or ""
    if action_type == "tool_call" and step.tool_name:
        tool_calls = [
            {"function": {"name": step.tool_name, "arguments": step.tool_input}}
        ]
        content = ""
    elif action_type == "assistant_tool_request" and step.tool_name:
        tool_calls = [
            {"function": {"name": step.tool_name, "arguments": step.tool_input}}
        ]

    return {
        "step": step.index + 1,
        "event_type": event_type,
        "role": step.role,
        "action": action_type,
        "content": content,
        "tool_calls": tool_calls,
        "tool_call_id": None,
        "finish_reason": None,
        "success": step.success,
    }


def build_model_evaluation_trajectory(
    *,
    case_id: str,
    trajectory: List[TrajectoryStep],
) -> dict[str, Any]:
    """Build the compact evaluation trajectory consumed by key-step alignment.

    ``trajectory`` is the evo ``TrajectoryStep`` list (as returned by
    ``TrajectoryExtractor.extract``); the result carries the same ``trajectory``
    shape expected by ``align_trajectory_to_key_steps``.
    """
    events = [_step_to_event(step) for step in trajectory]
    return {
        "schema_version": EVALUATION_TRAJECTORY_SCHEMA,
        "case_id": case_id,
        "trajectory": events,
    }


def has_non_empty_key_steps(case: dict[str, Any]) -> bool:
    return isinstance(case.get("key_steps"), list) and bool(case["key_steps"])


__all__ = [
    "build_model_evaluation_trajectory",
    "has_non_empty_key_steps",
]
