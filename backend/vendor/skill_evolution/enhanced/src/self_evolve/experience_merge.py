"""Hierarchical (map-reduce) experience merging with random grouping.

Splits a large number of per-case detail units into random batches (default 10),
summarizes each batch, then repeatedly merges the intermediate results until a
single consolidated output remains. This keeps prompt context bounded while still
seeing every case.

Failure/knowledge experiences use the ``root_cause`` + ``suggestions`` schema
(one root cause carries a list of suggestions). ``trajectory_count`` (number of
failure trajectories hitting the experience) accumulates as batches merge; the
suggestions stay a list — concatenation into a display string is done in code,
not by the model. Success summaries only need the bounded-context reduction, so
they use the text variant.
"""

from __future__ import annotations

import json
import re
import random
from typing import Any, Callable, Dict, List

from ..model_client import QuotaExceededError
from .mechanism_rules import MERGE_MECHANISM_RULES


def chunk(items: List[Any], size: int) -> List[List[Any]]:
    """Split ``items`` into consecutive chunks of ``size``."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def parse_json_array(text: str) -> Any:
    """Parse the first JSON array found in ``text`` (lenient)."""
    text = (text or "").strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def sanitize_experiences(raw: Any) -> List[Dict[str, Any]]:
    """Normalize a model-produced experience list into root_cause + suggestions.

    Drops non-dict / empty entries, dedups ``suggestions`` and ``trajectory_ids``,
    and recomputes ``trajectory_count = len(trajectory_ids)`` so counts are always
    consistent.
    """
    if not isinstance(raw, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        root_cause = str(item.get("root_cause") or "").strip()
        if not root_cause:
            continue
        raw_suggestions = item.get("suggestions") or item.get("suggestion") or []
        if isinstance(raw_suggestions, str):
            raw_suggestions = [raw_suggestions]
        suggestions: List[str] = []
        for s in raw_suggestions:
            if isinstance(s, str) and s.strip() and s.strip() not in suggestions:
                suggestions.append(s.strip())
        ids: List[str] = []
        for tid in (item.get("trajectory_ids") or []):
            if isinstance(tid, str) and tid and tid not in ids:
                ids.append(tid)
        result.append({
            "root_cause": root_cause,
            "suggestions": suggestions,
            "trajectory_ids": ids,
            "trajectory_count": len(ids),
        })
    return result


def experience_to_text(exp: Dict[str, Any]) -> str:
    """Join ``root_cause`` + ``suggestions`` into a display string (code-level)."""
    parts = [str(exp.get("root_cause") or "").strip()]
    parts.extend(str(s).strip() for s in (exp.get("suggestions") or []) if str(s).strip())
    return "\n".join(p for p in parts if p)


def merge_experience_batch(
    generate: Callable[[List[Dict[str, str]]], str],
    batch: List[Dict[str, Any]],
    *,
    kind_label: str = "失败",
) -> List[Dict[str, Any]]:
    """Merge one batch of experience dicts into a consolidated experience list.

    ``generate`` is a ``generate(messages) -> str`` callable (e.g. ModelClient.generate).
    Same-root-cause experiences are merged (suggestions union, trajectory_ids union,
    counts add); distinct root causes stay independent. One retry on unparseable
    output.
    """
    prompt = f"""你是SKILL{kind_label}经验合并专家。以下是若干条经验（每条带根因 root_cause、建议列表 suggestions、命中轨迹列表 trajectory_ids 和计数 trajectory_count），请合并语义相同/相近的经验。

## 要求
- 同一根因的经验合并为一条：root_cause 取更完整表述，suggestions 取并集（去重、不丢失任何一条），trajectory_ids 取并集，trajectory_count 相加。
- 不同根因的经验保持独立，不要强行合并；一条经验一个根因。
- 建议保持为列表（suggestions），不要把多条建议改写合并成一句；最终拼接由代码完成。

{MERGE_MECHANISM_RULES}

## 待合并经验
{json.dumps(batch, ensure_ascii=False, indent=2)}

输出 JSON 数组（只输出 JSON，不要其他内容）：
[{{"root_cause": "...", "suggestions": ["...", ...], "trajectory_ids": [...], "trajectory_count": N}}]"""
    for attempt in range(2):
        try:
            text = generate([{"role": "user", "content": prompt}])
        except QuotaExceededError:
            raise
        except Exception as exc:
            print(f"[警告] 经验合并模型调用失败: {exc}")
            return []
        exps = sanitize_experiences(parse_json_array(text))
        if exps:
            return exps
        prompt += "\n\n【重要】只输出 JSON 数组，以 [ 开头、以 ] 结尾，不要输出任何解释。"
    return []


def hierarchical_merge_experiences(
    leaves: List[Dict[str, Any]],
    *,
    to_experiences: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
    merge_experiences: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
    batch_size: int = 10,
    rng: Any = None,
) -> List[Dict[str, Any]]:
    """Merge many leaf units into a consolidated experience list.

    ``leaves`` are level-0 units (e.g. ``{"case_id": ..., "detail": ...}``).
    ``to_experiences`` converts one batch of leaves into experience dicts
    ``{"experience", "trajectory_ids", "trajectory_count"}``. ``merge_experiences``
    merges one batch of experience dicts (union ids / add counts). Returns the
    final experience list.
    """
    rng = rng or random
    shuffled = list(leaves)
    rng.shuffle(shuffled)

    current: List[Dict[str, Any]] = []
    for batch in chunk(shuffled, batch_size):
        current.extend(to_experiences(batch))

    while len(current) > batch_size:
        rng.shuffle(current)
        nxt: List[Dict[str, Any]] = []
        for batch in chunk(current, batch_size):
            merged = merge_experiences(batch)
            nxt.extend(merged if merged else batch)  # 合并失败（空结果）时保留原批，避免丢数据
        if len(nxt) >= len(current):
            break  # no further consolidation possible (experiences are distinct)
        current = nxt

    if len(current) > 1:
        merged = merge_experiences(current)
        if merged:
            current = merged
    return current


def hierarchical_summarize_text(
    leaves: List[str],
    *,
    summarize_batch: Callable[[List[str]], str],
    batch_size: int = 10,
    rng: Any = None,
) -> str:
    """Reduce many text leaves into one summary via random-grouped hierarchical merging."""
    rng = rng or random
    shuffled = list(leaves)
    rng.shuffle(shuffled)

    current: List[str] = []
    for batch in chunk(shuffled, batch_size):
        current.append(summarize_batch(batch))

    while len(current) > 1:
        rng.shuffle(current)
        nxt: List[str] = []
        for batch in chunk(current, batch_size):
            nxt.append(summarize_batch(batch))
        current = nxt

    return current[0] if current else ""


__all__ = [
    "chunk",
    "experience_to_text",
    "hierarchical_merge_experiences",
    "hierarchical_summarize_text",
    "merge_experience_batch",
    "parse_json_array",
    "sanitize_experiences",
]
