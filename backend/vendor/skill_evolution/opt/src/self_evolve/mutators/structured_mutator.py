"""Structured mutator – bounded small-step edits using ADD/DELETE/REPLACE actions.

Unlike the freeform mutator which rewrites the entire document, this mutator
uses the SkillOpt approach: an LLM plans a limited set of edit actions and
applies them mechanically to the skill document.

Score-based acceptance is handled by the orchestrator via round-to-round
score comparison, not inside the mutator itself.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import BaseMutator
from .result import MutationResult
from ..evaluators.result import EvaluationResult


class StructuredMutator(BaseMutator):
    """Mutator that uses structured ADD/DELETE/REPLACE actions.

    The edit budget is capped by ``max_steps``. The LLM outputs a JSON
    plan of edit actions, which is applied to the skill document.
    The result is passed back to the orchestrator for score-based acceptance.
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
    ) -> MutationResult:
        if not self.model_client:
            return MutationResult(next_skill=skill, changed=False)

        # Build context for the planner
        print(f"    📋 构建编辑上下文... (成功经验: {bool(eval_result.success_experience)}, 失败经验: {bool(eval_result.failure_experience)}, 拒绝经验: {len(eval_result.rejected_experiences)}条)")
        context = self._build_plan_context(skill, eval_result)

        # Get edit plan from LLM (retry once with stricter prompt if parsing fails)
        print(f"    🤖 模型规划编辑方案 (最多 {self.max_steps} 步)...")
        plan_text = self._call_planner(context)

        if not plan_text:
            return MutationResult(next_skill=skill, changed=False)

        actions = _parse_actions(plan_text, self.max_steps)
        if not actions:
            # Retry with stricter JSON-only prompt
            print(f"    ⚠️ 首次未解析出有效 JSON，使用强化 prompt 重试...")
            plan_text = self._call_planner(context, force_json=True)
            if not plan_text:
                return MutationResult(next_skill=skill, changed=False)
            actions = _parse_actions(plan_text, self.max_steps)

        if not actions:
            print(f"    ⚠️ 未解析出有效编辑操作，放弃")
            return MutationResult(next_skill=skill, changed=False)

        print(f"    📝 规划了 {len(actions)} 步编辑: {' -> '.join(a['action'] for a in actions)}")

        # Apply actions
        next_skill = _apply_actions(skill, actions)

        if next_skill == skill:
            print(f"    ⚠️ 编辑操作未产生实际变化")
            return MutationResult(
                next_skill=skill,
                changed=False,
                mutation_plan=plan_text,
                raw={"actions": actions, "action_count": len(actions)},
            )

        print(f"    ✅ 应用成功，文档已修改")
        return MutationResult(
            next_skill=next_skill,
            changed=True,
            mutation_plan=plan_text,
            raw={"actions": actions, "action_count": len(actions)},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_plan_context(self, skill: str, eval_result: EvaluationResult) -> str:
        parts: List[str] = []

        parts.append(f"## 当前 SKILL 文档\n```markdown\n{skill}\n```")
        parts.append(f"\n## 评估结果\n平均分: {eval_result.average_score:.2f}")

        if eval_result.success_experience:
            parts.append(f"\n### 成功经验（应保留/强化）\n{eval_result.success_experience}")
        if eval_result.failure_experience:
            parts.append(f"\n### 失败经验（应修复）\n{eval_result.failure_experience}")
        if eval_result.rejected_experiences:
            rejected_text = "\n".join(f"- {r}" for r in eval_result.rejected_experiences[-5:])
            parts.append(f"\n### 之前被拒的改进（应避免重复）\n{rejected_text}")

        return "\n".join(parts)

    def _call_planner(self, context: str, force_json: bool = False) -> str:
        system = f"""你是一个 SKILL 文档编辑专家。你的任务是根据评估反馈，规划对 SKILL 文档的具体编辑。

## 可用操作

1. **ADD** – 在文档特定位置**之后**插入新内容
   ```json
   {{"action": "ADD", "after": "<在此文本之后插入>", "content": "<要插入的内容>"}}
   ```

2. **DELETE** – 删除文档中的一段内容
   ```json
   {{"action": "DELETE", "target": "<要删除的精确文本>"}}
   ```

3. **REPLACE** – 替换文档中的一段内容
   ```json
   {{"action": "REPLACE", "target": "<要替换的精确文本>", "content": "<替换后的内容>"}}
   ```

## 重要规则

- 最多输出 {self.max_steps} 个操作
- target/after 必须是文档中实际存在的精确文本（复制粘贴，不要改写）
- action 和 content 的值不要包含 markdown 或注释——只输出原始内容
- 优先修复失败经验中提到的问题
- 保留成功经验中的有效模式
- 避免重复之前被拒的改进
- 保留 YAML frontmatter 不变
- 直接输出 JSON 数组，不要有任何解释

输出格式：
[
  {{"action": "REPLACE", "target": "旧文本...", "content": "新文本..."}},
  ...
]"""

        user = f"{context}\n\n请根据上述反馈，规划对 SKILL 文档的编辑操作（最多 {self.max_steps} 步）。直接输出 JSON 数组："
        if force_json:
            user += (
                f"\n\n【重要】你必须只输出一个有效的 JSON 数组，不能输出任何其他内容。"
                f"不要输出解释、分析或思考过程。你的回复必须以 [ 开头、以 ] 结尾。"
                f"如果你认为不需要修改，输出空数组 []。"
            )

        try:
            response = self.model_client.generate_with_system(system, user)
            return response
        except Exception as exc:
            print(f"[警告] 编辑计划生成失败: {exc}")
            return ""


# ------------------------------------------------------------------
# Action parsing and application
# ------------------------------------------------------------------

def _parse_actions(text: str, max_steps: int) -> List[Dict[str, str]]:
    """Parse LLM output into a validated list of edit actions."""
    import json
    import re

    # Extract JSON array
    text = text.strip()
    # Remove markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    # Find the JSON array
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

    # Validate actions
    valid: List[Dict[str, str]] = []
    for act in actions[:max_steps]:
        if not isinstance(act, dict):
            continue
        action = act.get("action", "").upper()
        if action not in ("ADD", "DELETE", "REPLACE"):
            continue
        if action == "ADD":
            if "after" not in act or "content" not in act:
                continue
        elif action == "DELETE":
            if "target" not in act:
                continue
        elif action == "REPLACE":
            if "target" not in act or "content" not in act:
                continue
        valid.append({"action": action, **{k: v for k, v in act.items() if k != "action"}})

    return valid


def _apply_actions(skill: str, actions: List[Dict[str, str]]) -> str:
    """Apply edit actions to the skill document. Actions are applied in order."""
    result = skill

    for act in actions:
        action_type = act["action"]
        if action_type == "REPLACE":
            target = act.get("target", "")
            content = act.get("content", "")
            if target and target in result:
                result = result.replace(target, content, 1)
            else:
                print(f"[警告] REPLACE: 未找到目标文本 '{target[:80]}...'")

        elif action_type == "DELETE":
            target = act.get("target", "")
            if target and target in result:
                result = result.replace(target, "", 1)
            else:
                print(f"[警告] DELETE: 未找到目标文本 '{target[:80]}...'")

        elif action_type == "ADD":
            after = act.get("after", "")
            content = act.get("content", "")
            if after and after in result:
                idx = result.index(after) + len(after)
                # Ensure newline separation
                sep = "\n" if not after.endswith("\n") else ""
                result = result[:idx] + sep + content + result[idx:]
            else:
                print(f"[警告] ADD: 未找到锚点文本 '{after[:80]}...'")

    return result
