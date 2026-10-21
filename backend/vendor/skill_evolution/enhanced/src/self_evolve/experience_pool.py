"""Experience pool — persistent, deduplicated storage for success/failure/knowledge/rejected experiences.

Pools:
- success_pool: 经验证有效的关键机制，修改时必须保留（单文本，跨轮保留）
- failure_pool: 尚未解决的问题及其修改建议，驱动本轮修改目标（统一池，知识与失败一视同仁）
- invalid_pool: 无效用例（轨迹为空 / 评估解析失败），仅记录用于诊断，不参与变异
- rejected: 当前根版本下已造成退化的修改操作（简单列表，跟随根版本生命周期）

失败池统一语义（每轮重置，不跨轮合并）：
- 失败经验与知识经验都进入同一个 ``failure_pool``；一条经验 = 一个 ``root_cause``
  + 一个 ``suggestions`` 列表 + ``trajectory_ids`` / ``trajectory_count`` + ``origin``。
- 同根因经验在本轮内用代码合并（suggestions 追加、trajectory_ids 取并集、计数相加）。
- ``origin`` 仅记录来源（failure / knowledge / both），不携带任何优先级含义。
- ``trajectory_count`` 记录本轮命中该经验的失败轨迹数，``select_failure`` 用它降序排序。
"""

from __future__ import annotations

import json
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from ..model_client import QuotaExceededError


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ExperienceItem:
    """A single experience entry in the pool."""

    id: str                                            # unique id
    type: Literal["success", "failure", "invalid"]     # pool type
    content: str = ""                                   # display text (success: raw; failure: root_cause + suggestions)
    source_rounds: List[int] = field(default_factory=list)  # rounds that regenerated this
    last_seen_round: int = 0                            # most recent round where it appeared
    status: Literal["active", "resolved"] = "active"    # lifecycle status
    origin: Literal["failure", "knowledge", "both"] = "failure"  # provenance only, no priority
    trajectory_count: int = 0                           # failure trajectories hitting this experience
    # structured failure/knowledge fields (Part ②)
    root_cause: str = ""                                # 根因
    suggestions: List[str] = field(default_factory=list)  # 建议列表
    trajectory_ids: List[str] = field(default_factory=list)  # 命中的 case_id

    @staticmethod
    def generate_id(content: str, prefix: str = "exp") -> str:
        """Generate a stable id from content hash + timestamp."""
        h = hashlib.md5(content.encode("utf-8")).hexdigest()[:8]
        return f"{prefix}_{h}"


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

class ExperiencePool:
    """Manages success, failure, and rejected experience pools with persistence.

    Usage::

        pool = ExperiencePool(model_client=eval_client)
        pool.merge_success("有效的查询策略...", round_idx=1)
        pool.merge_failure(
            {"root_cause": "缺少参数校验", "suggestions": ["在调用前校验入参"],
             "trajectory_ids": ["case_1"], "trajectory_count": 1},
            round_idx=1, origin="failure",
        )

        # Before mutation
        selected = pool.select_failure(max_count=5)
        all_success = pool.get_success_texts()

        # Serialize
        pool.save(output_dir / "experience_pool.json")
    """

    def __init__(self, model_client: Any = None):
        self.model_client = model_client
        self.success_pool: List[ExperienceItem] = []
        self.failure_pool: List[ExperienceItem] = []
        self.invalid_pool: List[ExperienceItem] = []
        self.rejected: List[str] = []

    # ------------------------------------------------------------------
    # Success pool
    # ------------------------------------------------------------------

    def merge_success(self, new_text: str, round_idx: int) -> Optional[ExperienceItem]:
        """Merge a new success experience into the pool.

        Uses semantic comparison (model call) to decide: merge / supplement / add / skip.
        Returns the affected ExperienceItem or None if entirely duplicate.
        """
        if not new_text.strip():
            return None
        return self._merge_into_pool(
            new_text=new_text,
            pool=self.success_pool,
            pool_type="success",
            round_idx=round_idx,
        )

    def get_all_success(self) -> List[ExperienceItem]:
        """Return all active success experiences."""
        return [e for e in self.success_pool if e.status == "active"]

    def get_success_texts(self) -> List[str]:
        """Return all active success experience texts (for prompt injection)."""
        return [e.content for e in self.get_all_success()]

    # ------------------------------------------------------------------
    # Failure pool (unified: failure + knowledge, no priority distinction)
    # ------------------------------------------------------------------

    def merge_failure(
        self,
        experience: Dict[str, Any],
        round_idx: int,
        *,
        origin: Literal["failure", "knowledge", "both"] = "failure",
    ) -> Optional[ExperienceItem]:
        """Merge one structured failure/knowledge experience into the failure pool.

        ``experience`` carries ``root_cause``, ``suggestions`` (list), and
        ``trajectory_ids`` (list). A matching entry is found via exact match
        first, then semantic match (model) as a fallback. On a match:
        suggestions appended, trajectory_ids union, count recomputed, and the
        merged root cause (from the model, if any) replaces the existing one.
        ``origin`` records provenance only (failure / knowledge / both); it never
        affects selection.
        """
        root_cause = str(experience.get("root_cause") or "").strip()
        if not root_cause:
            return None
        suggestions: List[str] = []
        for s in (experience.get("suggestions") or []):
            s = str(s).strip()
            if s and s not in suggestions:
                suggestions.append(s)
        trajectory_ids: List[str] = []
        for tid in (experience.get("trajectory_ids") or []):
            if isinstance(tid, str) and tid and tid not in trajectory_ids:
                trajectory_ids.append(tid)

        match = self._find_failure_match(root_cause, suggestions)
        if match is not None:
            item = match["item"]
            merged_root_cause = str(match.get("merged_root_cause") or "").strip()
            # 语义兜底命中时，模型可能给出更完整的根因表述；更新 root_cause 并重算 id
            if merged_root_cause and merged_root_cause != item.root_cause:
                item.root_cause = merged_root_cause
                item.id = ExperienceItem.generate_id(item.root_cause, "fail")
            for s in suggestions:
                if s not in item.suggestions:
                    item.suggestions.append(s)
            for tid in trajectory_ids:
                if tid not in item.trajectory_ids:
                    item.trajectory_ids.append(tid)
            item.trajectory_count = len(item.trajectory_ids)
            item.origin = _merge_origin(item.origin, origin)
            if round_idx not in item.source_rounds:
                item.source_rounds.append(round_idx)
            item.last_seen_round = round_idx
            item.status = "active"
            item.content = self._render_content(item)
            return item

        item = ExperienceItem(
            id=ExperienceItem.generate_id(root_cause, "fail"),
            type="failure",
            root_cause=root_cause,
            suggestions=suggestions,
            trajectory_ids=trajectory_ids,
            trajectory_count=len(trajectory_ids),
            source_rounds=[round_idx],
            last_seen_round=round_idx,
            status="active",
            origin=origin,
        )
        item.content = self._render_content(item)
        self.failure_pool.append(item)
        return item

    @staticmethod
    def _render_content(item: "ExperienceItem") -> str:
        """Render the display text (root_cause + bulleted suggestions)."""
        parts = [item.root_cause] if item.root_cause else []
        parts.extend(f"- {s}" for s in item.suggestions if s)
        return "\n".join(parts)

    def _find_failure_match(
        self,
        root_cause: str,
        suggestions: List[str],
    ) -> Optional[Dict[str, Any]]:
        """Find an existing failure-pool entry matching ``root_cause``.

        Exact match first (fast path, keeps the original behaviour); if no exact
        match and a model client is available, ask the model whether the new root
        cause is semantically the same as one of the existing entries. Returns
        ``{"item": ExperienceItem, "merged_root_cause": str}`` or ``None``.
        """
        active = [e for e in self.failure_pool if e.status == "active"]
        for item in active:
            if item.root_cause == root_cause:
                return {"item": item, "merged_root_cause": root_cause}
        if self.model_client is None:
            return None
        matched = self._model_match_failure_root_cause(root_cause, suggestions)
        if not matched:
            return None
        target_id = matched.get("target_id")
        merged_root_cause = str(matched.get("merged_root_cause") or "").strip()
        for item in active:
            if item.id == target_id:
                return {"item": item, "merged_root_cause": merged_root_cause or item.root_cause}
        return None

    def _model_match_failure_root_cause(
        self,
        root_cause: str,
        suggestions: List[str],
    ) -> Optional[Dict[str, str]]:
        """Ask the model whether ``root_cause`` matches an existing failure entry.

        Returns ``{"target_id", "merged_root_cause"}`` on a semantic match, else
        ``None``. ``QuotaExceededError`` propagates; other errors fall back to
        ``None`` (treated as "no match" so the experience is added, never lost).
        """
        active = [e for e in self.failure_pool if e.status == "active"]
        if not active:
            return None
        pool_lines = []
        for item in active:
            preview = item.root_cause[:200].replace("\n", " ")
            pool_lines.append(f"- [{item.id}] {preview}")
        pool_list = "\n".join(pool_lines)
        suggestion_lines = "\n".join(f"- {s}" for s in suggestions if s) or "(无)"

        prompt = f"""比较以下新的失败/知识根因与已有失败经验池中各条根因，判断新根因是否与某一条"语义相同/描述同一根因"。

## 新根因
{root_cause[:500]}

## 新建议（辅助判断根因）
{suggestion_lines}

## 已有失败经验池（每条含 id 和 root_cause）
{pool_list}

## 要求
- 若新根因与某一条已有根因语义相同/描述同一根因，match 填 true，target_id 填该条 id，merged_root_cause 给出合并后更完整的根因表述。
- 若都不相同，match 填 false。
- 只输出 JSON，不要其他内容。

输出 JSON:
{{"match": true/false, "target_id": "...", "merged_root_cause": "...", "reason": "..."}}"""

        try:
            response = self.model_client.generate([{"role": "user", "content": prompt}])
        except QuotaExceededError:
            raise
        except Exception as exc:
            print(f"[经验池] 根因语义匹配模型调用失败: {exc}")
            return None
        result = _parse_json(response)
        match_val = result.get("match")
        is_match = match_val is True or str(match_val).strip().lower() == "true"
        if not is_match:
            return None
        target_id = str(result.get("target_id") or "").strip()
        if not target_id:
            return None
        return {
            "target_id": target_id,
            "merged_root_cause": str(result.get("merged_root_cause") or "").strip(),
        }

    def clear_failure_pool(self) -> None:
        """Reset the failure pool at the start of a new round."""
        self.failure_pool.clear()

    def select_failure(self, max_count: int) -> List[ExperienceItem]:
        """Select at most *max_count* active failure experiences for this round.

        Priority: ``trajectory_count`` descending (more failure trajectories
        hitting an experience = higher priority). ``last_seen_round`` is a
        stability tiebreaker.
        """
        active = [e for e in self.failure_pool if e.status == "active"]
        active.sort(key=lambda e: (e.trajectory_count, e.last_seen_round), reverse=True)
        return active[:max_count]

    def mark_resolved_failures(self, current_issue_texts: List[str], current_round: int = 0) -> None:
        """No-op retained for compatibility — the failure pool resets each round."""
        return None

    # ------------------------------------------------------------------
    # Invalid pool (diagnostic only — never drives mutations)
    # ------------------------------------------------------------------

    def merge_invalid(self, new_text: str, round_idx: int) -> Optional[ExperienceItem]:
        """Record an invalid case (empty trajectory / evaluation parse failure).

        Invalid entries are diagnostic-only and never participate in mutation.
        Deduplication is a cheap content equality check (no model call).
        """
        if not new_text.strip():
            return None
        for item in self.invalid_pool:
            if item.status == "active" and item.content == new_text:
                item.last_seen_round = round_idx
                if round_idx not in item.source_rounds:
                    item.source_rounds.append(round_idx)
                return item
        item = ExperienceItem(
            id=ExperienceItem.generate_id(new_text, prefix="invalid"),
            type="invalid",
            content=new_text,
            source_rounds=[round_idx],
            last_seen_round=round_idx,
            status="active",
        )
        self.invalid_pool.append(item)
        return item

    def get_all_invalid(self) -> List[ExperienceItem]:
        """Return all active invalid experiences."""
        return [e for e in self.invalid_pool if e.status == "active"]

    def get_invalid_texts(self) -> List[str]:
        """Return all active invalid case texts (for diagnostics / web display)."""
        return [e.content for e in self.get_all_invalid()]

    # ------------------------------------------------------------------
    # Rejected experiences
    # ------------------------------------------------------------------

    def add_rejected(self, text: str) -> None:
        """Add a rejected experience. Simple list, no dedup needed."""
        if text.strip():
            self.rejected.append(text.strip())

    def clear_rejected(self) -> None:
        """Clear all rejected experiences (on exploratory rewrite trigger)."""
        self.rejected.clear()

    def get_rejected(self) -> List[str]:
        """Return the rejected list."""
        return list(self.rejected)

    # ------------------------------------------------------------------
    # Merge logic
    # ------------------------------------------------------------------

    def _merge_into_pool(
        self,
        *,
        new_text: str,
        pool: List[ExperienceItem],
        pool_type: str,
        round_idx: int,
        origin: str = "failure",
        trajectory_count: int = 0,
    ) -> Optional[ExperienceItem]:
        """Generic merge logic for success / failure pools."""
        # If pool is empty, add directly
        if not pool:
            return self._add_new(new_text, pool_type, round_idx, pool,
                                 origin=origin, trajectory_count=trajectory_count)

        # Use model for semantic comparison (or heuristic fallback)
        if self.model_client:
            action, target_id = self._model_compare(new_text, pool, pool_type)
        else:
            action, target_id = self._heuristic_compare(new_text, pool)

        if action == "new":
            return self._add_new(new_text, pool_type, round_idx, pool,
                                 origin=origin, trajectory_count=trajectory_count)

        item: Optional[ExperienceItem] = None
        if action == "merge" and target_id:
            item = self._update_existing(target_id, new_text, round_idx, pool, merge=True,
                                         origin=origin, trajectory_count=trajectory_count)
        elif action == "supplement" and target_id:
            item = self._update_existing(target_id, new_text, round_idx, pool, merge=False,
                                         origin=origin, trajectory_count=trajectory_count)
        elif action == "skip" and target_id:
            # Fully covered by an existing experience, but still represents another
            # trajectory hitting it — update counts/provenance without touching content.
            item = self._touch_existing(target_id, round_idx, pool,
                                        origin=origin, trajectory_count=trajectory_count)

        if item is not None:
            return item
        # target_id 不存在（模型幻觉）或未命中 —— 回退为新增，避免经验丢失
        return self._add_new(new_text, pool_type, round_idx, pool,
                             origin=origin, trajectory_count=trajectory_count)

    def _add_new(
        self,
        text: str,
        pool_type: str,
        round_idx: int,
        pool: List[ExperienceItem],
        *,
        origin: str = "failure",
        trajectory_count: int = 0,
    ) -> ExperienceItem:
        item = ExperienceItem(
            id=ExperienceItem.generate_id(text, pool_type[:4]),
            type=pool_type,  # type: ignore[arg-type]
            content=text,
            source_rounds=[round_idx],
            last_seen_round=round_idx,
            status="active",
            origin=origin,  # type: ignore[arg-type]
            trajectory_count=max(0, trajectory_count),
        )
        pool.append(item)
        return item

    def _update_existing(
        self,
        target_id: str,
        new_text: str,
        round_idx: int,
        pool: List[ExperienceItem],
        *,
        merge: bool,
        origin: str = "failure",
        trajectory_count: int = 0,
    ) -> Optional[ExperienceItem]:
        for item in pool:
            if item.id == target_id:
                if merge:
                    # If the new text is more specific or different, replace
                    if len(new_text) > len(item.content) or new_text != item.content:
                        item.content = new_text
                else:
                    # Supplement: append if not already contained
                    if new_text not in item.content:
                        item.content = item.content.rstrip() + "\n" + new_text
                self._touch_item(item, round_idx, origin=origin, trajectory_count=trajectory_count)
                return item
        return None

    def _touch_existing(
        self,
        target_id: str,
        round_idx: int,
        pool: List[ExperienceItem],
        *,
        origin: str = "failure",
        trajectory_count: int = 0,
    ) -> Optional[ExperienceItem]:
        """Update counts/provenance on an existing item without changing content."""
        for item in pool:
            if item.id == target_id:
                self._touch_item(item, round_idx, origin=origin, trajectory_count=trajectory_count)
                return item
        return None

    @staticmethod
    def _touch_item(
        item: ExperienceItem,
        round_idx: int,
        *,
        origin: str,
        trajectory_count: int,
    ) -> None:
        """Apply a round hit to an existing item: counts + provenance + freshness."""
        item.trajectory_count = item.trajectory_count + max(0, trajectory_count)
        item.origin = _merge_origin(item.origin, origin)
        if round_idx not in item.source_rounds:
            item.source_rounds.append(round_idx)
        item.last_seen_round = round_idx
        item.status = "active"

    # ------------------------------------------------------------------
    # Model-based semantic comparison
    # ------------------------------------------------------------------

    def _model_compare(
        self,
        new_text: str,
        pool: List[ExperienceItem],
        pool_type: str,
    ) -> tuple:
        """Use model to determine merge action. Returns (action, target_id)."""
        if not pool:
            return ("new", None)

        # Build compact pool listing
        pool_lines = []
        for item in pool:
            if item.status != "active":
                continue
            preview = item.content[:200].replace("\n", " ")
            pool_lines.append(f"- [{item.id}] {preview}")

        if not pool_lines:
            return ("new", None)

        pool_list = "\n".join(pool_lines)
        type_label = {"success": "成功", "failure": "失败", "invalid": "无效"}.get(pool_type, "经验")

        prompt = f"""比较以下新的{type_label}经验与经验池中已有条目，判断应采取的操作。

## 新经验
{new_text[:500]}

## 已有经验池
{pool_list}

## 操作选项
- "new": 完全不同的新经验，新增一条
- "merge": 与某已有经验说的是同一个问题/机制，内容更全面/表述更好，应替换原经验
- "supplement": 对某已有经验的补充/细化，应追加到原经验中
- "skip": 新经验完全被已有经验覆盖，无需任何操作

输出 JSON:
{{"action": "new|merge|supplement|skip", "target_id": "<已有经验的id>", "reason": "一句话原因"}}

只输出 JSON。"""

        try:
            response = self.model_client.generate([{"role": "user", "content": prompt}])
            result = _parse_json(response)
            action = result.get("action", "new")
            target_id = result.get("target_id")
            if action not in ("new", "merge", "supplement", "skip"):
                action = "new"
            return (action, target_id)
        except QuotaExceededError:
            raise
        except Exception as exc:
            print(f"[经验池] 模型比较失败: {exc}，回退到启发式比较")
            return self._heuristic_compare(new_text, pool)

    # ------------------------------------------------------------------
    # Heuristic fallback
    # ------------------------------------------------------------------

    def _heuristic_compare(self, new_text: str, pool: List[ExperienceItem]) -> tuple:
        """Keyword-overlap based comparison when model is unavailable."""
        best_overlap = 0.0
        best_id = None

        for item in pool:
            if item.status != "active":
                continue
            overlap = _keyword_overlap(new_text, item.content)
            if overlap > best_overlap:
                best_overlap = overlap
                best_id = item.id

        if best_overlap > 0.7:
            return ("merge", best_id)
        elif best_overlap > 0.4:
            return ("supplement", best_id)
        else:
            return ("new", None)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON storage."""
        def _item_to_dict(item: ExperienceItem) -> Dict[str, Any]:
            return {
                "id": item.id,
                "type": item.type,
                "content": item.content,
                "source_rounds": item.source_rounds,
                "last_seen_round": item.last_seen_round,
                "status": item.status,
                "origin": item.origin,
                "trajectory_count": item.trajectory_count,
                "root_cause": item.root_cause,
                "suggestions": item.suggestions,
                "trajectory_ids": item.trajectory_ids,
            }

        return {
            "success_pool": [_item_to_dict(e) for e in self.success_pool],
            "failure_pool": [_item_to_dict(e) for e in self.failure_pool],
            "invalid_pool": [_item_to_dict(e) for e in self.invalid_pool],
            "rejected": self.rejected,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], model_client: Any = None) -> "ExperiencePool":
        """Deserialize from dict (compatible with the legacy knowledge_pool format)."""
        pool = cls(model_client=model_client)

        def _item_from_dict(d: Dict[str, Any], *, default_origin: str = "failure") -> ExperienceItem:
            item_type = d.get("type", "failure")
            # Legacy knowledge items are migrated into the unified failure pool.
            if item_type == "knowledge":
                item_type = "failure"
                default_origin = "knowledge"
            root_cause = d.get("root_cause", "")
            if not root_cause and item_type == "failure":
                root_cause = d.get("content", "")
            return ExperienceItem(
                id=d["id"],
                type=item_type,
                content=d.get("content", ""),
                source_rounds=d.get("source_rounds", []),
                last_seen_round=d.get("last_seen_round", 0),
                status=d.get("status", "active"),
                origin=d.get("origin", default_origin),
                trajectory_count=d.get("trajectory_count", 0),
                root_cause=root_cause,
                suggestions=d.get("suggestions", []),
                trajectory_ids=d.get("trajectory_ids", []),
            )

        pool.success_pool = [_item_from_dict(e) for e in data.get("success_pool", [])]
        pool.failure_pool = [_item_from_dict(e) for e in data.get("failure_pool", [])]
        # Legacy knowledge_pool -> merged into failure_pool with origin="knowledge".
        pool.failure_pool.extend(
            _item_from_dict(e, default_origin="knowledge")
            for e in data.get("knowledge_pool", [])
        )
        pool.invalid_pool = [_item_from_dict(e) for e in data.get("invalid_pool", [])]
        pool.rejected = data.get("rejected", [])
        return pool

    def save(self, path: Path) -> None:
        """Save pool to a JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path, model_client: Any = None) -> Optional["ExperiencePool"]:
        """Load pool from a JSON file. Returns None if file doesn't exist."""
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(data, model_client=model_client)
        except Exception as exc:
            print(f"[经验池] 加载失败: {exc}，使用空池")
            return cls(model_client=model_client)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """Human-readable pool summary."""
        active_success = len(self.get_all_success())
        active_failure = len([e for e in self.failure_pool if e.status == "active"])
        resolved = len([e for e in self.failure_pool if e.status == "resolved"])
        active_invalid = len(self.get_all_invalid())
        # Origin distribution of the unified failure pool (diagnostic only).
        origin_counts: Dict[str, int] = {"failure": 0, "knowledge": 0, "both": 0}
        for e in self.failure_pool:
            if e.status == "active" and e.origin in origin_counts:
                origin_counts[e.origin] += 1
        return (
            f"经验池: 成功={active_success}条, "
            f"失败={active_failure}条(活跃) {resolved}条(已解决), "
            f"[来源: 失败={origin_counts['failure']} 知识={origin_counts['knowledge']} "
            f"两者={origin_counts['both']}], "
            f"无效={active_invalid}条, "
            f"拒绝={len(self.rejected)}条"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _merge_origin(current: str, incoming: str) -> str:
    """Combine two provenance tags. Same source stays; different -> both."""
    if current == incoming:
        return current
    return "both"


def _keyword_overlap(text_a: str, text_b: str) -> float:
    """Compute keyword overlap ratio between two texts."""
    def _tokens(s: str) -> set:
        # Simple Chinese/English tokenizer: split by common delimiters, keep 2-char+ segments
        import re
        words = set()
        # Extract 2+ char Chinese segments
        for m in re.findall(r'[\u4e00-\u9fff]{2,}', s):
            words.add(m)
        # Extract English words
        for m in re.findall(r'[a-zA-Z]{2,}', s.lower()):
            words.add(m)
        return words

    a_tokens = _tokens(text_a)
    b_tokens = _tokens(text_b)
    if not a_tokens or not b_tokens:
        return 0.0
    intersection = a_tokens & b_tokens
    return len(intersection) / min(len(a_tokens), len(b_tokens))


def _parse_json(text: str) -> Dict[str, Any]:
    """Parse JSON from text, extracting the first JSON object found."""
    import re
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}
