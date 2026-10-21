"""Structured mutator — bounded small-step edits with experience binding.

Each edit action (ADD/DELETE/REPLACE) binds to a selected failure experience,
so修改效果可以追踪。成功经验作为保留约束，拒绝经验作为禁止操作。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .base import BaseMutator
from .result import MutationResult
from ...model_client import QuotaExceededError
from ..evaluators.result import EvaluationResult


class StructuredMutator(BaseMutator):
    """Bounded ADD/DELETE/REPLACE edits, each bound to a selected failure experience.

    The orchestrator passes a unified failure list (failure + knowledge, ordered
    by ``trajectory_count``) via ``selected_failures``; there is no separate
    knowledge priority.
    """

    def __init__(self, model_client: Any, max_steps: int = 5):
        self.model_client = model_client
        self.max_steps = max_steps

    def mutate(
        self,
        *,
        skill: str,
        eval_result: EvaluationResult,
        data: List[Any],
        state: Dict[str, Any],
        selected_failures: Optional[List[Dict[str, str]]] = None,
        all_success: Optional[List[str]] = None,
    ) -> MutationResult:
        if not self.model_client:
            return MutationResult(next_skill=skill, changed=False)

        # Build context
        n_fail = len(selected_failures) if selected_failures else 0
        n_succ = len(all_success) if all_success else 0
        n_rej = len(eval_result.rejected_experiences)
        print(f"    编辑上下文: 成功约束={n_succ}条, 失败目标={n_fail}条, 拒绝={n_rej}条")

        context = _build_context(
            skill=skill,
            avg_score=eval_result.average_score,
            selected_failures=selected_failures,
            all_success=all_success,
            rejected=eval_result.rejected_experiences,
        )

        # Plan → retry once with stricter prompt
        print(f"    模型规划编辑方案 (最多 {self.max_steps} 步)...")
        plan_text = self._call_planner(context, selected_failures)
        if not plan_text:
            return MutationResult(next_skill=skill, changed=False)

        actions = _parse_actions(plan_text, self.max_steps)
        if not actions:
            print(f"    首次未解析出有效 JSON，重试...")
            plan_text = self._call_planner(context, selected_failures, force_json=True)
            if not plan_text:
                return MutationResult(next_skill=skill, changed=False)
            actions = _parse_actions(plan_text, self.max_steps)

        if not actions:
            print(f"    未解析出有效编辑操作")
            return MutationResult(next_skill=skill, changed=False)

        # Report planned actions with their bound experiences
        action_summary = " -> ".join(
            f"{a['action']}[{a.get('bound_experience_id', '?')}]"
            for a in actions
        )
        print(f"    规划了 {len(actions)} 步: {action_summary}")

        # Apply
        next_skill = _apply_actions(skill, actions)
        if next_skill == skill:
            return MutationResult(
                next_skill=skill,
                changed=False,
                mutation_plan=plan_text,
                raw={"actions": actions, "action_count": len(actions)},
            )

        return MutationResult(
            next_skill=next_skill,
            changed=True,
            mutation_plan=plan_text,
            raw={"actions": actions, "action_count": len(actions)},
        )

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    def _call_planner(
        self,
        context: str,
        selected_failures: Optional[List[Dict[str, str]]] = None,
        force_json: bool = False,
    ) -> str:
        """Call LLM to generate edit plan. Each action must bind to a selected failure experience."""

        # Build failure experience reference list (unified failure + knowledge)
        failure_ref = ""
        if selected_failures:
            items = []
            for f in selected_failures:
                fid = f.get("id", "?")
                fcontent = f.get("content", "")[:120]
                items.append(f"- `{fid}`: {fcontent}")
            failure_ref = "\n".join(items)

        system = f"""你是 SKILL 文档编辑专家。根据选中的失败经验，规划具体的编辑操作。

## 可用操作

1. ADD — 在指定位置之后插入新内容
2. DELETE — 删除指定内容
3. REPLACE — 替换指定内容

## 输出格式

每个操作必须绑定到一个失败经验 ID。JSON 格式：

[
  {{
    "action": "REPLACE",
    "target": "文档中存在的精确文本",
    "content": "替换后的新内容",
    "bound_experience_id": "失败经验ID",
    "reason": "为什么这个修改能落实该经验"
  }},
  ...
]

## 约束规则

- 最多 {self.max_steps} 个操作
- 每个操作必须绑定一个失败经验 ID（bound_experience_id）
- 一个经验可以被多个操作绑定
- target/after 必须是文档中实际存在的精确文本（直接复制）
- 不要破坏"成功约束"中列出的能力
- 不要重复"之前被拒的修改"中列出的操作
- 保留 YAML frontmatter 不变
- 直接输出 JSON 数组"""

        target_block = (
            f"## 当前失败经验\n{failure_ref}" if failure_ref else "（无选中经验）"
        )

        user = (
            f"{context}\n\n"
            f"{target_block}\n\n"
            f"请规划编辑操作（最多 {self.max_steps} 步），直接输出 JSON 数组："
        )

        if force_json:
            user += (
                "\n\n【重要】只输出 JSON 数组，以 [ 开头、以 ] 结尾。"
                "不要输出任何解释。如果无法修改，输出 []。"
            )

        try:
            return self.model_client.generate_with_system(system, user)
        except QuotaExceededError:
            raise
        except Exception as exc:
            print(f"[警告] 编辑计划生成失败: {exc}")
            return ""


# ------------------------------------------------------------------
# Context builder (module-level for clarity)
# ------------------------------------------------------------------

def _build_context(
    *,
    skill: str,
    avg_score: float,
    selected_failures: Optional[List[Dict[str, str]]],
    all_success: Optional[List[str]],
    rejected: List[str],
) -> str:
    """Build the edit planning context from experience pool data."""
    parts: List[str] = []

    parts.append(f"## 当前 SKILL 文档\n```markdown\n{skill}\n```")
    parts.append(f"\n## 本轮均分\n{avg_score:.2f}")

    if all_success:
        items = "\n".join(f"- {s}" for s in all_success)
        parts.append(f"\n## 成功约束（修改时必须保留的能力）\n{items}")

    if selected_failures:
        items = "\n".join(
            f"- [{f.get('id', '?')}] {f.get('content', '')}"
            for f in selected_failures
        )
        parts.append(f"\n## 当前选中失败经验（修改目标）\n{items}")

    if rejected:
        items = "\n".join(f"- {r}" for r in rejected[-5:])
        parts.append(f"\n## 之前被拒的修改（禁止重复）\n{items}")

    return "\n".join(parts)


# ------------------------------------------------------------------
# Action parsing and application
# ------------------------------------------------------------------

def _parse_actions(text: str, max_steps: int) -> List[Dict[str, str]]:
    """Parse LLM output into a validated list of edit actions."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        print("[警告] 未找到 JSON 数组")
        return []

    try:
        actions = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        print(f"[警告] JSON 解析失败: {exc}")
        return []

    if not isinstance(actions, list):
        return []

    valid: List[Dict[str, str]] = []
    for act in actions[:max_steps]:
        if not isinstance(act, dict):
            continue
        action = act.get("action", "").upper()
        if action not in ("ADD", "DELETE", "REPLACE"):
            continue

        required = {
            "ADD": ["after", "content"],
            "DELETE": ["target"],
            "REPLACE": ["target", "content"],
        }
        if any(field not in act for field in required.get(action, [])):
            continue

        entry = {
            "action": action,
            **{k: v for k, v in act.items() if k != "action"},
        }
        valid.append(entry)

    return valid


def _apply_actions(skill: str, actions: List[Dict[str, str]]) -> str:
    """Apply edit actions in order. Anchored edits, skip on missing target."""
    result = skill

    for act in actions:
        action_type = act["action"]

        if action_type == "REPLACE":
            target = act.get("target", "")
            content = act.get("content", "")
            if target and target in result:
                result = result.replace(target, content, 1)
            else:
                print(f"[警告] REPLACE: 未找到目标 '{target[:80]}...'")

        elif action_type == "DELETE":
            target = act.get("target", "")
            if target and target in result:
                result = result.replace(target, "", 1)
            else:
                print(f"[警告] DELETE: 未找到目标 '{target[:80]}...'")

        elif action_type == "ADD":
            after = act.get("after", "")
            content = act.get("content", "")
            if after and after in result:
                idx = result.index(after) + len(after)
                sep = "" if after.endswith("\n") else "\n"
                result = result[:idx] + sep + content + result[idx:]
            else:
                print(f"[警告] ADD: 未找到锚点 '{after[:80]}...'")

    return result
