"""Map observable trajectory events to case-defined key steps.

Migrated from ``skill_distill_multi_dim/src/trace2skill/key_steps_judge.py``; the only
change is the client contract — ``client.complete_json(system, user, max_tokens=...)``
is provided by evo's ``ModelClient`` (see ``src/model_client.py``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_PROMPT_PATH = Path(__file__).resolve().parent / "key_steps_judge_prompt.md"
_KEY_STEP_FIELDS = {"step_id", "description", "key_info", "tools", "dependence", "weight"}


def _system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


def _case_user_request(case: dict[str, Any]) -> str:
    utterance = str(case.get("utterance") or "").strip()
    if utterance:
        return utterance
    texts: list[str] = []
    for item in case.get("input") or []:
        if isinstance(item, str) and item.strip():
            texts.append(item.strip())
        elif (
            isinstance(item, dict)
            and str(item.get("type") or "text") == "text"
            and str(item.get("data") or "").strip()
        ):
            texts.append(str(item["data"]).strip())
    return "\n".join(texts)


def _string_list(item: dict[str, Any], field: str, *, index: int) -> list[str]:
    value = item.get(field)
    if not isinstance(value, list) or any(not isinstance(entry, str) or not entry.strip() for entry in value):
        raise ValueError(f"key_steps[{index}].{field} must be an array of non-empty strings")
    return [entry.strip() for entry in value]


def validate_key_steps(key_steps: Any) -> list[dict[str, Any]]:
    if not isinstance(key_steps, list) or not key_steps:
        raise ValueError("case key_steps must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, item in enumerate(key_steps):
        if not isinstance(item, dict):
            raise ValueError(f"key_steps[{index}] must be an object")
        extra = set(item) - _KEY_STEP_FIELDS
        if extra:
            raise ValueError(f"key_steps[{index}] contains unsupported fields: {sorted(extra)}")
        step_id = str(item.get("step_id") or "").strip()
        description = str(item.get("description") or "").strip()
        if not step_id or step_id in ids:
            raise ValueError(f"key_steps[{index}].step_id must be non-empty and unique")
        if not description:
            raise ValueError(f"key_steps[{index}] requires a non-empty description")
        key_info = _string_list(item, "key_info", index=index) if "key_info" in item else None
        tools = _string_list(item, "tools", index=index) if "tools" in item else None
        dependence = item.get("dependence") if "dependence" in item else None
        if dependence is not None:
            if not isinstance(dependence, list) or any(
                isinstance(entry, bool) or not isinstance(entry, (str, int)) or not str(entry).strip()
                for entry in dependence
            ):
                raise ValueError(f"key_steps[{index}].dependence must be an array of step IDs")
            unknown = [entry for entry in dependence if str(entry).strip() not in ids]
            if unknown:
                raise ValueError(f"key_steps[{index}].dependence must reference earlier steps: {unknown}")
        ids.add(step_id)
        normalized_item: dict[str, Any] = {
            "step_id": step_id,
            "description": description,
        }
        if key_info is not None:
            normalized_item["key_info"] = key_info
        if tools is not None:
            normalized_item["tools"] = tools
        if dependence is not None:
            normalized_item["dependence"] = list(dependence)
        normalized.append(normalized_item)
    return normalized


def validate_key_step_mapping(
    key_steps: list[dict[str, Any]],
    trajectory: list[dict[str, Any]],
    mappings: Any,
) -> list[dict[str, Any]]:
    if not isinstance(mappings, list):
        raise ValueError("key_step_mappings must be an array")
    expected_ids = [item["step_id"] for item in key_steps]
    returned_ids = [item.get("step_id") for item in mappings if isinstance(item, dict)]
    if returned_ids != expected_ids or len(mappings) != len(expected_ids):
        raise ValueError("key step IDs or order do not match case config")
    expected_steps = [item["step"] for item in trajectory]
    mapped: list[int] = []
    cleaned: list[dict[str, Any]] = []
    for item in mappings:
        if not isinstance(item, dict):
            raise ValueError("key step mapping must be an object")
        actual = item.get("actual_trajectory_steps")
        if not isinstance(actual, list) or any(isinstance(step, bool) or not isinstance(step, int) for step in actual):
            raise ValueError(f"invalid actual_trajectory_steps for {item.get('step_id')}")
        if actual != sorted(actual):
            raise ValueError("actual trajectory steps must remain ordered")
        if len(actual) != len(set(actual)):
            raise ValueError("a trajectory step may appear at most once within one key step mapping")
        if any(step not in expected_steps for step in actual):
            raise ValueError("mapping references an unknown trajectory step")
        mapped.extend(actual)
        cleaned.append(
            {
                "step_id": item["step_id"],
                "actual_trajectory_steps": actual,
                "evidence": item.get("evidence") if isinstance(item.get("evidence"), list) else [],
                "reason": str(item.get("reason") or ""),
            }
        )
    missing = sorted(set(expected_steps) - set(mapped))
    if missing:
        raise ValueError(
            "actual trajectory mapping invalid: "
            f"expected={expected_steps}, mapped={mapped}, missing={missing}"
        )
    return cleaned


def _mapping_snapshot(mappings: Any) -> dict[str, Any]:
    if not isinstance(mappings, list):
        return {"returned": mappings}
    return {
        str(item.get("step_id")): item.get("actual_trajectory_steps")
        for item in mappings
        if isinstance(item, dict)
    }


def _contains_operation(event: dict[str, Any], operation: str) -> bool:
    def visit(value: Any) -> bool:
        if isinstance(value, dict):
            if value.get("operation") == operation:
                return True
            return any(visit(item) for item in value.values())
        if isinstance(value, list):
            return any(visit(item) for item in value)
        return False

    return visit(event.get("tool_calls")) or visit(event.get("content"))


def _step_match_text(step: dict[str, Any]) -> str:
    values = [str(step.get("description") or "")]
    values.extend(str(item) for item in step.get("key_info") or [])
    return " ".join(values).lower()


def _matching_step_index(
    key_steps: list[dict[str, Any]],
    *,
    keywords: tuple[str, ...],
    tools_required: bool | None = None,
    prefer_last: bool = False,
) -> int | None:
    matches: list[int] = []
    for index, key_step in enumerate(key_steps):
        tools = key_step.get("tools") if isinstance(key_step.get("tools"), list) else []
        if tools_required is True and not tools:
            continue
        if tools_required is False and tools:
            continue
        text = _step_match_text(key_step)
        if any(keyword.lower() in text for keyword in keywords):
            matches.append(index)
    if not matches:
        return None
    return matches[-1] if prefer_last else matches[0]


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value.lower()
    try:
        return json.dumps(value, ensure_ascii=False, default=str).lower()
    except (TypeError, ValueError):
        return str(value).lower()


def _preferred_key_step_index(
    event: dict[str, Any],
    *,
    event_index: int,
    event_count: int,
    key_steps: list[dict[str, Any]],
) -> int:
    """Choose a primary-function bucket from description/key_info/tools metadata."""
    key_step_count = len(key_steps)
    if key_step_count <= 1:
        return 0
    role = event.get("role")
    event_type = event.get("event_type")
    content = event.get("content")
    if role == "user" or event_type == "query":
        matched = _matching_step_index(
            key_steps,
            keywords=("分析用户需求", "理解用户需求", "任务意图", "识别意图", "user request", "intent"),
            tools_required=False,
        )
        return matched if matched is not None else 0
    if event_type == "final" or (role == "assistant" and event.get("finish_reason") == "stop"):
        final_text = _content_text(content)
        if any(
            marker in final_text
            for marker in ("澄清", "确认", "授权", "补充信息", "clarif", "confirm", "permission")
        ):
            clarification = _matching_step_index(
                key_steps,
                keywords=("澄清", "确认", "授权", "补充信息", "暂停", "clarif", "confirm"),
                tools_required=False,
            )
            if clarification is not None:
                return clarification
        summary = _matching_step_index(
            key_steps,
            keywords=("总结", "展示结果", "回复", "报告结果", "summary", "report result"),
            tools_required=False,
            prefer_last=True,
        )
        return summary if summary is not None else key_step_count - 1
    if event_type == "tool_call":
        if any(
            _contains_operation(event, operation)
            for operation in ("fetch_catalog", "query", "read", "inspect", "search", "list", "get")
        ):
            matched = _matching_step_index(
                key_steps,
                keywords=("查询", "读取", "检查", "分析", "状态", "属性", "能力", "约束", "inspect", "read", "query"),
                tools_required=True,
            )
            return matched if matched is not None else min(1, key_step_count - 1)
        if any(
            _contains_operation(event, operation)
            for operation in ("execute", "write", "update", "create", "delete", "apply", "submit")
        ):
            matched = _matching_step_index(
                key_steps,
                keywords=("执行", "操作", "创建", "修改", "写入", "提交", "apply", "execute", "create", "update"),
                tools_required=True,
                prefer_last=True,
            )
            return matched if matched is not None else min(3, key_step_count - 1)
    if event_type == "observation" or role == "tool":
        result_type = _content_text(content.get("result_type") if isinstance(content, dict) else content)
        if any(marker in result_type for marker in ("catalog", "query", "read", "inspect", "fetch", "search", "lookup")):
            matched = _matching_step_index(
                key_steps,
                keywords=("查询", "读取", "检查", "分析", "状态", "属性", "能力", "约束", "inspect", "read", "query"),
                tools_required=True,
            )
            return matched if matched is not None else min(1, key_step_count - 1)
        if any(marker in result_type for marker in ("execution", "write", "update", "create", "delete", "apply", "submit")):
            matched = _matching_step_index(
                key_steps,
                keywords=("执行", "操作", "创建", "修改", "写入", "提交", "apply", "execute", "create", "update"),
                tools_required=True,
                prefer_last=True,
            )
            return matched if matched is not None else min(3, key_step_count - 1)
    if role == "assistant":
        matched = _matching_step_index(
            key_steps,
            keywords=("规划", "方案", "步骤", "参数", "plan", "approach"),
            tools_required=False,
        )
        return matched if matched is not None else min(2, key_step_count - 1)
    return min(key_step_count - 1, event_index * key_step_count // max(1, event_count))


def repair_key_step_mapping(
    key_steps: list[dict[str, Any]],
    trajectory: list[dict[str, Any]],
    mappings: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Repair missing coverage without discarding valid multi-key-step mappings."""
    if not isinstance(mappings, list):
        raise ValueError("key_step_mappings must be an array")
    expected_ids = [item["step_id"] for item in key_steps]
    returned_ids = [item.get("step_id") for item in mappings if isinstance(item, dict)]
    if returned_ids != expected_ids or len(mappings) != len(expected_ids):
        raise ValueError("key step IDs or order do not match case config")

    shape_only = []
    for item in mappings:
        cloned = dict(item) if isinstance(item, dict) else item
        if isinstance(cloned, dict):
            cloned["actual_trajectory_steps"] = []
        shape_only.append(cloned)
    cleaned = validate_key_step_mapping(key_steps, [], shape_only)

    expected_steps = [event["step"] for event in trajectory]
    events_by_step = {event["step"]: event for event in trajectory}
    claims: dict[int, list[int]] = {step: [] for step in expected_steps}
    for owner, item in enumerate(mappings):
        actual = item.get("actual_trajectory_steps")
        if not isinstance(actual, list) or any(isinstance(step, bool) or not isinstance(step, int) for step in actual):
            raise ValueError(f"invalid actual_trajectory_steps for {item.get('step_id')}")
        if actual != sorted(actual):
            raise ValueError("actual trajectory steps must remain ordered")
        if len(actual) != len(set(actual)):
            raise ValueError("a trajectory step may appear at most once within one key step mapping")
        if any(step not in events_by_step for step in actual):
            raise ValueError("mapping references an unknown trajectory step")
        for step in actual:
            claims[step].append(owner)

    repaired_lists = [list(item["actual_trajectory_steps"]) for item in mappings]
    repairs: list[dict[str, Any]] = []
    for event_index, step in enumerate(expected_steps):
        owners = claims[step]
        if not owners:
            owner = _preferred_key_step_index(
                events_by_step[step],
                event_index=event_index,
                event_count=len(expected_steps),
                key_steps=key_steps,
            )
            repairs.append(
                {
                    "trajectory_step": step,
                    "previous_owners": [],
                    "assigned_to": expected_ids[owner],
                    "reason": "deterministic missing-event mapping repair",
                }
            )
            repaired_lists[owner].append(step)

    for item, actual in zip(cleaned, repaired_lists):
        item["actual_trajectory_steps"] = sorted(actual)
    return validate_key_step_mapping(key_steps, trajectory, cleaned), repairs


def map_trajectory_to_key_steps(
    *,
    case: dict[str, Any],
    key_steps: list[dict[str, Any]],
    trajectory: list[dict[str, Any]],
    client: Any | None = None,
) -> dict[str, Any]:
    if client is None:
        raise RuntimeError("key steps mapper is not configured")
    prompt = (
        "# 轨迹步骤映射\n\n"
        "## Case 信息\n\n"
        f"- `case_id`：{case.get('case_id') or case.get('id')}\n"
        f"- 场景：{case.get('scenario') or (case.get('metadata') or {}).get('scenario') or ''}\n"
        f"- 用户请求：{_case_user_request(case)}\n\n"
        "## 当前 Case 配置的 Key Steps\n\n"
        f"{json.dumps(key_steps, ensure_ascii=False, indent=2)}\n\n"
        "## 强制映射覆盖要求\n\n"
        f"- 必须覆盖的 `actual_step_ids`：{[event['step'] for event in trajectory]}\n"
        "- 上方每个 `actual_step_id` 至少必须在下列数组中出现一次：\n"
        f"{json.dumps({item['step_id']: [] for item in key_steps}, ensure_ascii=False)}\n\n"
        "- 同一个 `actual_step_id` 只有在其实际行为同时满足多个 Key Step 的 `description` "
        "及 `key_info` 约束时，才可以映射到多个 Key Step；不得仅因步骤相邻、存在依赖或"
        "后续会使用该结果而重复映射。\n\n"
        "## 实际轨迹\n\n"
        f"{json.dumps(trajectory, ensure_ascii=False, indent=2)}"
    )
    last_error = ""
    last_mappings: Any = None
    max_tokens = int(os.environ.get("KEY_STEPS_MAPPER_MAX_TOKENS", "16384"))
    for attempt in range(2):
        current = prompt
        if attempt:
            expected_steps = [event["step"] for event in trajectory]
            current += (
                "\n\n# 映射校验失败后的重试\n\n"
                "## 校验信息\n\n"
                f"- 上次输出校验失败：{last_error}\n"
                f"- 必须至少一次覆盖这些实际 step：{expected_steps}\n"
                f"- 上次映射：{json.dumps(_mapping_snapshot(last_mappings), ensure_ascii=False)}\n\n"
                "## 修正要求\n\n"
                "- 逐项核对 `missing`；保留有充分 description 和 key_info 证据的多重映射。\n"
                "- 修正后重新输出完整 JSON。"
            )
        try:
            result = client.complete_json(_system_prompt(), current, max_tokens=max_tokens)
        except Exception as exc:
            last_error = str(exc)
            continue
        last_mappings = result.get("key_step_mappings")
        try:
            mappings = validate_key_step_mapping(key_steps, trajectory, last_mappings)
        except ValueError as exc:
            last_error = str(exc)
            continue
        return {
            "case_id": case.get("case_id") or case.get("id"),
            "key_step_mappings": mappings,
            "summary": str(result.get("summary") or ""),
            "mapping_repairs": [],
        }
    try:
        repaired, repairs = repair_key_step_mapping(key_steps, trajectory, last_mappings)
    except ValueError as exc:
        raise ValueError(f"key step mapper output invalid after retry: {last_error}; repair failed: {exc}") from exc
    return {
        "case_id": case.get("case_id") or case.get("id"),
        "key_step_mappings": repaired,
        "summary": "Mapper output required deterministic coverage repair",
        "mapping_repairs": repairs,
    }


def align_trajectory_to_key_steps(
    *,
    case: dict[str, Any],
    key_steps: list[dict[str, Any]] | None = None,
    evaluation_trajectory: list[dict[str, Any]],
    client: Any | None = None,
    model_role: str | None = None,
) -> dict[str, Any]:
    """Map one model trajectory to case key steps without affecting task success."""
    configured = validate_key_steps(key_steps if key_steps is not None else case.get("key_steps"))
    result = map_trajectory_to_key_steps(
        case=case,
        key_steps=configured,
        trajectory=evaluation_trajectory,
        client=client,
    )
    return {
        **result,
        "model_role": model_role,
        "configured_key_steps": configured,
    }


__all__ = [
    "align_trajectory_to_key_steps",
    "map_trajectory_to_key_steps",
    "repair_key_step_mapping",
    "validate_key_step_mapping",
    "validate_key_steps",
]
