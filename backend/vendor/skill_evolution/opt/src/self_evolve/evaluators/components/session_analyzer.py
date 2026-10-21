"""Multi-dimensional session analysis (三维度：过程 / 精简 / 结果)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ....model_client import ModelClient
from ...schemas import SessionAnalysisResult, TrajectoryStep, to_plain_dict


# 结果子类型（数据集 output 的 type 允许这些；onlyllm 为专用 rubric 二进制判定；
# word 为黑/白名单关键词硬匹配校验；format 为 JSON 结构格式校验，均不依赖模型）
RESULT_TYPES = ("text", "tool", "file", "onlyllm", "word", "format")


class SessionAnalyzer:
    """三维度独立评估：过程 / 精简 / 结果（结果再按 output.type 分发）。"""

    def __init__(self, model_client: Optional[ModelClient] = None, verbose: bool = False):
        self.model_client = model_client
        self.verbose = verbose
        self._debug_dir: Optional[Path] = None

    def analyze(
        self,
        *,
        case_id: str,
        input_text: str,
        trajectory: List[TrajectoryStep],
        key_steps: Optional[List[Dict[str, Any]]] = None,
        expected_outputs: Optional[Dict[str, Any]] = None,
        judgment_weights: Optional[Dict[str, float]] = None,
        output_weights: Optional[Dict[str, float]] = None,
        output_path: Optional[Path] = None,
        verbose: Optional[bool] = None,
        input_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> SessionAnalysisResult:
        """
        Analyze session and generate three-dimension scores.

        expected_outputs: 按 type 聚合的期望输出，如
            {"text": "...", "tool": "...", "file": {"expect_path": "...", "actual_path": "..."},
             "onlyllm": "rubric prompt 文件路径"}
        judgment_weights: {process, conciseness, result}，权重为 0 的维度跳过评估。
        output_weights: {text, tool, file, onlyllm}，权重为 0 或类型缺失的子类型跳过评估。
        input_messages: direct 模式下的用户输入 messages 数组（供 onlyllm 构建 conversation）。
        """
        _verbose = verbose if verbose is not None else self.verbose
        self._debug_dir = None
        if _verbose and output_path:
            self._debug_dir = output_path.parent / "_debug"
            self._debug_dir.mkdir(parents=True, exist_ok=True)

        key_steps = key_steps or []
        expected_outputs = expected_outputs or {}
        judgment_weights = judgment_weights or {}
        output_weights = output_weights or {}

        process = 0.0
        process_details: Dict[str, Any] = {}
        conciseness = 0.0
        conciseness_details: Dict[str, Any] = {}
        output_scores: Dict[str, float] = {}
        output_analyses: Dict[str, Dict[str, Any]] = {}

        # 维度 1: 过程（权重非零才评估）
        if self.model_client and judgment_weights.get("process", 0.0) > 0:
            process, process_details = self._analyze_process(
                trajectory=trajectory, key_steps=key_steps,
            )

        # 维度 2: 精简（权重非零才评估）
        if self.model_client and judgment_weights.get("conciseness", 0.0) > 0:
            conciseness, conciseness_details = self._analyze_conciseness(
                trajectory=trajectory, input_text=input_text,
            )

        # 维度 3: 结果（按 output.type 分发；权重非零才评估）
        for otype in RESULT_TYPES:
            if otype not in expected_outputs:
                continue
            if output_weights.get(otype, 0.0) <= 0:
                continue
            expected = expected_outputs[otype]
            if otype == "text":
                score, details = self._analyze_text(
                    trajectory=trajectory, input_text=input_text, expected_text=expected,
                )
            elif otype == "tool":
                score, details = self._analyze_tool(
                    trajectory=trajectory, expected_tool=expected,
                )
            elif otype == "onlyllm":
                score, details = self._analyze_onlyllm(
                    trajectory=trajectory, input_text=input_text,
                    input_messages=input_messages, rubric_path=expected,
                )
            elif otype == "word":
                score, details = self._analyze_word(
                    trajectory=trajectory, expected_word=expected,
                )
            elif otype == "format":
                score, details = self._analyze_format(
                    trajectory=trajectory, expected_format=expected,
                )
            else:  # file
                score, details = self._analyze_file(expected_file=expected)
            output_scores[otype] = score
            output_analyses[otype] = details

        overall_improvement_suggestions = self._build_overall_suggestions(
            process_details, conciseness_details, output_analyses,
        )

        result = SessionAnalysisResult(
            case_id=case_id,
            key_steps=key_steps,
            process=process,
            process_details=process_details,
            conciseness=conciseness,
            conciseness_details=conciseness_details,
            output_scores=output_scores,
            output_analyses=output_analyses,
            trajectory_quality=(process + conciseness) / 2 if (process or conciseness) else 0.0,
            overall_improvement_suggestions=overall_improvement_suggestions,
        )

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(to_plain_dict(result), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        return result

    # ------------------------------------------------------------------
    # 维度 1: 过程（关键步骤覆盖 / 精准度）
    # ------------------------------------------------------------------

    def _analyze_process(
        self,
        *,
        trajectory: List[TrajectoryStep],
        key_steps: List[Dict[str, Any]],
    ) -> Tuple[float, Dict[str, Any]]:
        """判断期望关键步骤是否被实际轨迹覆盖（含 tool_input 以便核对参数）。"""
        if not self.model_client:
            return 0.0, {}

        compact_trajectory = []
        for step in trajectory:
            c = step.content or ""
            if len(c) > 500:
                c = c[:500] + f"...[截断，原{len(step.content)}字符]"
            compact_trajectory.append({
                "index": step.index,
                "role": step.role,
                "action_type": step.action_type,
                "tool_name": step.tool_name,
                "content": c,
                "tool_input": step.tool_input,
                "success": step.success,
            })

        if key_steps:
            prompt = f"""请根据以下期望关键步骤，判断实际执行轨迹是否覆盖了这些步骤，并总结每个步骤的实现方式。

注意：轨迹中可能包含试错步骤（失败的尝试），请识别出真正实现关键步骤的步骤，忽略试错过程。tool_input 字段用于核对调用参数是否准确。

## 期望关键步骤
{json.dumps(key_steps, ensure_ascii=False, indent=2)}

## 实际执行轨迹
{json.dumps(compact_trajectory, ensure_ascii=False, indent=2)}

请输出 JSON 格式的结果，其中 step_id 对应期望步骤中的 id/step_id/name 字段：
{{
    "key_step_coverage": {{"<step_id>": true/false, ...}},
    "step_scores": {{"<step_id>": 0-100, ...}},
    "step_implementations": {{"<step_id>": "该步骤在轨迹中的实现方式（排除试错步骤）", ...}},
    "missing_steps": ["未被覆盖的关键步骤列表及原因"],
    "trial_and_error_steps": ["轨迹中的试错步骤索引列表"],
    "improvement_suggestions": "具体的改进建议：如何确保所有关键步骤都被正确覆盖",
    "analysis": "整体分析说明：哪些步骤缺失、为什么缺失、如何改进"
}}

只输出 JSON，不要其他内容。"""
        else:
            prompt = f"""请根据实际执行轨迹，判断每个步骤是否被正确执行。

## 实际执行轨迹
{json.dumps(compact_trajectory, ensure_ascii=False, indent=2)}

## 说明
无参考步骤定义，请基于轨迹本身判断执行是否正确。

请输出 JSON 格式的结果：
{{
    "key_step_coverage": {{"step_<index>": true/false, ...}},
    "step_scores": {{"step_<index>": 0-100, ...}},
    "analysis": "分析说明"
}}

只输出 JSON，不要其他内容。"""

        self._write_debug("process", prompt)

        try:
            result, parse_error = self._generate_and_parse(prompt, "process")
            if parse_error:
                return 0.0, {"error": parse_error}

            step_weights: Dict[str, float] = {}
            for step in key_steps:
                step_id = step.get("id") or step.get("step_id") or step.get("name")
                weight = step.get("weight", 1.0)
                if step_id:
                    step_weights[str(step_id)] = weight

            step_scores = result.get("step_scores", {})
            weighted_sum = 0.0
            total_weight = 0.0
            for step_id, weight in step_weights.items():
                score = step_scores.get(step_id, 0.0)
                weighted_sum += score * weight
                total_weight += weight

            process = weighted_sum / total_weight if total_weight > 0 else 0.0

            return process, {
                "key_step_coverage": result.get("key_step_coverage", {}),
                "step_scores": step_scores,
                "step_weights": step_weights,
                "step_implementations": result.get("step_implementations", {}),
                "missing_steps": result.get("missing_steps", []),
                "trial_and_error_steps": result.get("trial_and_error_steps", []),
                "improvement_suggestions": result.get("improvement_suggestions", ""),
                "weighted_score": process,
                "analysis": result.get("analysis", ""),
                "source": "model",
            }

        except Exception as exc:
            return 0.0, {"error": str(exc)}

    # ------------------------------------------------------------------
    # 维度 2: 精简（过程简洁度）
    # ------------------------------------------------------------------

    def _analyze_conciseness(
        self,
        *,
        trajectory: List[TrajectoryStep],
        input_text: str,
    ) -> Tuple[float, Dict[str, Any]]:
        """评估执行过程是否简洁高效（只看结构，不含 content）。"""
        if not self.model_client:
            return 0.0, {}

        compact_trajectory = [
            {
                "index": step.index,
                "role": step.role,
                "action_type": step.action_type,
                "tool_name": step.tool_name,
            }
            for step in trajectory
        ]

        prompt = f"""请评估以下执行过程的简洁度和效率。

## 用户输入
{input_text}

## 执行轨迹（共 {len(trajectory)} 步）
{json.dumps(compact_trajectory, ensure_ascii=False, indent=2)}

请从以下几个方面评估：
1. 步骤数是否合理（相对于用户需求）
2. 是否有重复的步骤（具体指出哪些步骤重复）
3. 是否有不必要的步骤（具体指出哪些步骤不必要，为什么）
4. 步骤之间的逻辑是否清晰（是否有冗余的中间步骤）
5. 是否可以通过合并或优化步骤来提高效率（给出具体的优化方案）

输出 JSON 格式：
{{
    "step_count": {len(trajectory)},
    "repeated_steps": 数字,
    "repeated_step_indices": [具体的重复步骤索引],
    "unnecessary_steps": 数字,
    "unnecessary_step_indices": [具体的不必要步骤索引],
    "logic_clarity": "清晰/一般/混乱",
    "efficiency_score": 0-100,
    "optimization_plan": "具体的优化方案（如何减少步骤、如何合并操作）",
    "analysis": "综合分析：当前效率问题的根本原因和改进方向"
}}

只输出 JSON，不要其他内容。"""

        self._write_debug("conciseness", prompt)

        try:
            result, parse_error = self._generate_and_parse(prompt, "conciseness")
            if parse_error:
                return 0.0, {"error": parse_error}

            conciseness = result.get("efficiency_score", 0.0)

            return conciseness, {
                "step_count": result.get("step_count", len(trajectory)),
                "repeated_steps": result.get("repeated_steps", 0),
                "repeated_step_indices": result.get("repeated_step_indices", []),
                "unnecessary_steps": result.get("unnecessary_steps", 0),
                "unnecessary_step_indices": result.get("unnecessary_step_indices", []),
                "logic_clarity": result.get("logic_clarity", ""),
                "efficiency_score": conciseness,
                "optimization_plan": result.get("optimization_plan", ""),
                "analysis": result.get("analysis", ""),
                "source": "model",
            }

        except Exception as exc:
            return 0.0, {"error": str(exc)}

    # ------------------------------------------------------------------
    # 维度 3: 结果（text / tool / file）
    # ------------------------------------------------------------------

    def _analyze_text(
        self,
        *,
        trajectory: List[TrajectoryStep],
        input_text: str,
        expected_text: str,
    ) -> Tuple[float, Dict[str, Any]]:
        """评估助手最终文本输出 vs 期望文本。"""
        if not self.model_client:
            return 0.0, {}

        final_output = ""
        for step in reversed(trajectory):
            if step.role == "assistant" and step.content:
                final_output = step.content
                break

        prompt = f"""请评估以下助手的最终输出质量。

## 用户输入
{input_text}

## 助手最终输出
{final_output}"""

        if expected_text:
            prompt += f"\n\n## 期望输出\n{expected_text}"
        else:
            prompt += "\n\n## 说明\n无参考答案，请根据用户输入和输出内容评估质量"

        prompt += """

请评估：
1. 输出是否与用户需求相关
2. 输出是否完整（是否回答了用户的所有问题）
3. 输出的清晰度和专业性
4. 整体质量评分（0-100）
5. 具体的改进建议（如何提高相关性、完整性、清晰度）

输出 JSON 格式：
{
    "score": 0-100,
    "relevance_score": 0-100,
    "completeness_score": 0-100,
    "clarity_score": 0-100,
    "improvement_suggestions": "具体的改进建议：如何提高输出质量",
    "analysis": "分析说明：当前输出的主要问题和改进方向"
}

只输出 JSON，不要其他内容。"""

        self._write_debug("text", prompt)

        try:
            result, parse_error = self._generate_and_parse(prompt, "text")
            if parse_error:
                return 0.0, {"error": parse_error}

            score = result.get("score", 0.0)

            return score, {
                "score": score,
                "relevance_score": result.get("relevance_score", 0),
                "completeness_score": result.get("completeness_score", 0),
                "clarity_score": result.get("clarity_score", 0),
                "improvement_suggestions": result.get("improvement_suggestions", ""),
                "analysis": result.get("analysis", ""),
                "source": "model",
            }

        except Exception as exc:
            return 0.0, {"error": str(exc)}

    def _analyze_tool(
        self,
        *,
        trajectory: List[TrajectoryStep],
        expected_tool: str,
    ) -> Tuple[float, Dict[str, Any]]:
        """评估最后一次工具返回结果 vs 期望结果（不截断）。"""
        if not self.model_client or not expected_tool:
            return 0.0, {}

        actual = self._last_tool_result(trajectory)
        if not actual:
            return 0.0, {"analysis": "未找到工具返回结果", "source": "model"}

        prompt = f"""评估设备操作任务的最终结果准确性（对比实际设备最终状态与期望最终状态）。

## 实际设备最终状态
{actual}

## 期望设备最终状态
{expected_tool}

## 评分标准（严格：最终结果评估，逐设备判定）

设备操作任务的最终状态是确定性的。expected 可能包含一个或多个设备，请逐设备判定对错，再按正确设备占比给分。

逐设备判定：
- 某设备的 entity_id、state 与所有关键 attributes 全部精确匹配 → 该设备"正确"。
- 任一核心字段不匹配（entity_id 选错、state 错、关键 attributes 错或缺失）、设备未执行、或工具调用失败 → 该设备"错误"。

score 计算（对错误设备重罚）：
- 先算正确设备占比分：正确设备数 / 总设备数 × 100（四舍五入取整数）。
- 每个错误设备再额外扣 10 分。
- 最终 score = 占比分 - 10 × 错误设备数，最低 0 分。

示例：
- 6 个设备错 1 个：83 - 10 = 73 分
- 6 个设备错 2 个：67 - 20 = 47 分
- 1 个设备且该设备错：0 - 10 → 0 分
- 全部正确：100 分

关键原则：
- 单个设备内部不做"部分给分"：state 或任一关键 attributes 错，该设备即判错误。
- 不要把 entity_id + state 匹配当作"该设备部分正确"。attributes 错一个，该设备就是错的。
- 错误的具体位置与原因通过 mismatched 的 item / cause / detail 表达，不要用 score 折中。

逐项对比 entity_id、state 与 attributes 中的每个关键字段。对每个不匹配项，指出归因类型：
- "参数生成错误"：调用参数不合理，但工具返回了 success
- "执行失败"：调用参数合理，但工具返回失败或设备未响应

输出 JSON 格式：
{{
    "score": 0-100,
    "matched": ["匹配项列表"],
    "mismatched": [{{"item": "entity_id 或字段名", "expected": "...", "actual": "...", "cause": "参数生成错误或执行失败", "detail": "具体原因"}}],
    "analysis": "整体对比分析和归因"
}}

只输出 JSON，不要其他内容。"""

        self._write_debug("tool", prompt)

        try:
            result, parse_error = self._generate_and_parse(prompt, "tool")
            if parse_error:
                return 0.0, {"error": parse_error}

            score = result.get("score", 0.0)
            return score, {
                "score": score,
                "matched": result.get("matched", []),
                "mismatched": result.get("mismatched", []),
                "analysis": result.get("analysis", ""),
                "source": "model",
            }

        except Exception as exc:
            return 0.0, {"error": str(exc)}

    def _analyze_file(
        self,
        *,
        expected_file: Any,
    ) -> Tuple[float, Dict[str, Any]]:
        """对比期望文件与实际文件（expect_path / actual_path）。"""
        if not self.model_client or not expected_file:
            return 0.0, {}

        if not isinstance(expected_file, dict):
            return 0.0, {"analysis": "file 期望需提供 expect_path/actual_path", "source": "model"}

        expect_path = expected_file.get("expect_path", "")
        actual_path = expected_file.get("actual_path", "")
        if not expect_path or not actual_path:
            return 0.0, {"analysis": "file 期望缺少 expect_path 或 actual_path", "source": "model"}

        expect_content = self._read_file_content(expect_path)
        actual_content = self._read_file_content(actual_path)

        prompt = f"""评估文件结果的准确性（对比实际文件与期望文件）。

## 期望文件（{expect_path}）
{expect_content}

## 实际文件（{actual_path}）
{actual_content}

对比两个文件内容，评估其匹配度和质量。

输出 JSON 格式：
{{
    "score": 0-100,
    "analysis": "对比分析说明：实际与期望的匹配情况、存在的问题和改进建议"
}}

只输出 JSON，不要其他内容。"""

        self._write_debug("file", prompt)

        try:
            result, parse_error = self._generate_and_parse(prompt, "file")
            if parse_error:
                return 0.0, {"error": parse_error}

            score = result.get("score", 0.0)
            return score, {
                "score": score,
                "expect_path": expect_path,
                "actual_path": actual_path,
                "analysis": result.get("analysis", ""),
                "source": "model",
            }

        except Exception as exc:
            return 0.0, {"error": str(exc), "expect_path": expect_path, "actual_path": actual_path}

    def _analyze_onlyllm(
        self,
        *,
        trajectory: List[TrajectoryStep],
        input_text: str,
        input_messages: Optional[List[Dict[str, Any]]],
        rubric_path: str,
    ) -> Tuple[float, Dict[str, Any]]:
        """用专用 rubric prompt 判定执行模型输出是否合格（二进制 100/0）。

        rubric 文件含占位符 {current_year}/{conversation}/{history_memory_list}/
        {memory_list}/{peer_memory_list}/{fewshots}；按 issues 是否为空判通过/失败。
        """
        if not self.model_client:
            return 0.0, {}

        rubric = self._load_rubric_text(rubric_path)
        if not rubric:
            return 0.0, {"error": f"rubric 文件不存在或为空: {rubric_path}", "source": "model"}

        final_output = ""
        for step in reversed(trajectory):
            if step.role == "assistant" and step.content:
                final_output = step.content
                break

        conversation = self._build_conversation(input_messages, input_text)
        year = datetime.now().year
        prompt = self._render_rubric(
            rubric, year=year, conversation=conversation, memory_list=final_output,
        )

        # online rubric 无占位符时，手动把模型输出作为待评摘要注入
        if "{memory_list}" not in rubric:
            prompt = prompt.rstrip() + f"\n\n摘要输入：{final_output or '（空）'}\n"

        self._write_debug("onlyllm", prompt)
        response = self.model_client.generate([{"role": "user", "content": prompt}])
        self._write_debug("onlyllm", prompt, response)

        result = self._parse_json(response)
        if not result:
            return 0.0, {"error": "onlyllm 评估未返回有效 JSON", "source": "model"}

        # 兼容 online rubric（审核结果 三分类）与 issues 版 rubric
        if "审核结果" in result:
            verdict = str(result.get("审核结果", "")).strip()
            passed = verdict == "合规"
            score = 100.0 if passed else 0.0
            cat = result.get("问题大类", "") or ""
            detail = result.get("问题细节", "") or ""
            if passed:
                analysis = "合规"
            else:
                analysis = f"失败：{verdict}"
                if cat:
                    analysis += f"；{cat}"
                if detail:
                    analysis += f"；{detail}"
            return score, {
                "score": score,
                "passed": passed,
                "verdict": verdict,
                "problem_category": cat,
                "problem_detail": detail,
                "analysis": analysis,
                "rubric_path": rubric_path,
                "source": "model",
            }

        issues = result.get("issues", []) or []
        passed = not issues
        score = 100.0 if passed else 0.0
        comment = result.get("comment", "")
        if passed:
            analysis = comment or "通过"
        else:
            issue_lines = []
            for issue in issues:
                if isinstance(issue, dict):
                    issue_lines.append(f"[{issue.get('type', '?')}] {issue.get('reason', '')}")
            analysis = "失败：" + ("；".join(issue_lines) if issue_lines else f"{len(issues)} 个问题")
            if comment:
                analysis = f"{analysis}；{comment}"

        return score, {
            "score": score,
            "passed": passed,
            "issues": issues,
            "comment": comment,
            "analysis": analysis,
            "rubric_path": rubric_path,
            "source": "model",
        }

    def _analyze_word(
        self,
        *,
        trajectory: List[TrajectoryStep],
        expected_word: Any,
    ) -> Tuple[float, Dict[str, Any]]:
        """关键词校验：黑名单（不能出现）+ 白名单（必须出现）字符串硬匹配（确定性规则，不调模型）。

        expected_word 形如 {"blacklist": ["...", ...], "whitelist": ["...", ...]}。
        命中任一条黑名单字眼、或缺失任一条白名单字眼均计一次失败，
        score 按通过项占比给分（100 为全部通过）。
        """
        if not expected_word:
            return 0.0, {}

        if not isinstance(expected_word, dict):
            return 0.0, {
                "analysis": "word 期望需提供 blacklist/whitelist",
                "source": "rule",
            }

        blacklist = [str(w) for w in (expected_word.get("blacklist") or []) if str(w).strip()]
        whitelist = [str(w) for w in (expected_word.get("whitelist") or []) if str(w).strip()]

        final_output = ""
        for step in reversed(trajectory):
            if step.role == "assistant" and step.content:
                final_output = step.content
                break

        hit_black = [w for w in blacklist if w in final_output]
        miss_white = [w for w in whitelist if w not in final_output]

        total = len(blacklist) + len(whitelist)
        failed = len(hit_black) + len(miss_white)

        if total == 0:
            score = 100.0
            passed = True
            analysis = "无黑/白名单校验项"
        else:
            score = max(0.0, round((1.0 - failed / total) * 100.0, 1))
            passed = failed == 0
            if passed:
                analysis = "关键词校验通过"
            else:
                parts = []
                if hit_black:
                    parts.append("命中黑名单: " + "、".join(hit_black))
                if miss_white:
                    parts.append("缺失白名单: " + "、".join(miss_white))
                analysis = "关键词校验未通过：" + "；".join(parts)

        return score, {
            "score": score,
            "passed": passed,
            "blacklist": blacklist,
            "whitelist": whitelist,
            "blacklist_hits": hit_black,
            "whitelist_missing": miss_white,
            "final_output": final_output,
            "analysis": analysis,
            "source": "rule",
        }

    def _analyze_format(
        self,
        *,
        trajectory: List[TrajectoryStep],
        expected_format: Any,
    ) -> Tuple[float, Dict[str, Any]]:
        """格式校验：模型最终输出须为符合格式示例的 JSON 数组（确定性规则，不调模型）。

        - 空数组 []（如记忆萃取无结果）视为通过；
        - 非空时逐元素检查：必须是对象且字段集合与格式示例一致；
        - 解析失败 / 非数组 / 字段不匹配时返回实际输出 + 校验判断。
        expected_format 可为 JSON 字符串、list/dict；为空时回退 .env 的 OUTPUT_FORMAT_EXAMPLE。
        """
        format_example = self._resolve_format_example(expected_format)
        if format_example is None:
            return 0.0, {
                "analysis": "未配置格式示例（OUTPUT_FORMAT_EXAMPLE）或格式示例解析失败",
                "source": "rule",
            }

        # 由格式示例推导必填字段集合（多对象取并集）
        if isinstance(format_example, dict):
            example_objects = [format_example]
        else:
            example_objects = [o for o in format_example if isinstance(o, dict)]
        required_keys = sorted({k for o in example_objects for k in o.keys()})

        final_output = ""
        for step in reversed(trajectory):
            if step.role == "assistant" and step.content:
                final_output = step.content
                break

        parsed, parse_error = self._parse_json_array(final_output)

        if parse_error:
            score = 0.0
            passed = False
            parsed_list = None
            issues = [parse_error]
            analysis = f"格式校验未通过：{parse_error}"
        elif parsed == []:
            score = 100.0
            passed = True
            parsed_list = parsed
            issues = []
            analysis = "格式校验通过：空记忆列表（[]）"
        else:
            issues = []
            valid_count = 0
            total = len(parsed)
            for i, obj in enumerate(parsed):
                if not isinstance(obj, dict):
                    issues.append(f"第 {i} 项不是对象")
                    continue
                keys = set(obj.keys())
                if not required_keys:
                    valid_count += 1
                    continue
                missing = [k for k in required_keys if k not in keys]
                extra = [k for k in keys if k not in required_keys]
                if missing or extra:
                    msg = f"第 {i} 项字段不匹配"
                    if missing:
                        msg += f"（缺少 {', '.join(missing)}）"
                    if extra:
                        msg += f"（多余 {', '.join(extra)}）"
                    issues.append(msg)
                else:
                    valid_count += 1
            parsed_list = parsed
            if issues:
                score = max(0.0, round(valid_count / total * 100.0, 1))
                passed = valid_count == total
                analysis = "格式校验未通过：" + "；".join(issues)
            else:
                score = 100.0
                passed = True
                analysis = "格式校验通过"

        return score, {
            "score": score,
            "passed": passed,
            "actual_output": final_output,
            "format_example": format_example,
            "required_keys": required_keys,
            "parsed": parsed_list,
            "issues": issues,
            "analysis": analysis,
            "source": "rule",
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_format_example(expected_format: Any) -> Optional[Any]:
        """解析格式示例：优先数据集传入值，为空时回退 .env 的 OUTPUT_FORMAT_EXAMPLE。"""
        example = expected_format
        if isinstance(example, str):
            example = example.strip()
            if not example:
                example = None
            else:
                try:
                    example = json.loads(example)
                except (json.JSONDecodeError, TypeError):
                    example = None
        if example is None:
            from ....config import load_output_format_example

            example = load_output_format_example()
        if isinstance(example, str):
            try:
                example = json.loads(example)
            except (json.JSONDecodeError, TypeError):
                return None
        if isinstance(example, (list, dict)):
            return example
        return None

    @staticmethod
    def _parse_json_array(text: str) -> Tuple[Optional[List[Any]], str]:
        """从模型输出中稳健解析 JSON 数组，返回 (list_or_None, error_str)。

        - 支持裸 JSON、markdown 围栏包裹；
        - 空数组 [] 返回 ([], "")。
        """
        if not text:
            return None, "模型输出为空"
        t = text.strip()
        if not t:
            return None, "模型输出为空"

        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", t, re.DOTALL | re.IGNORECASE)
        if fenced:
            t = fenced.group(1).strip()

        try:
            parsed = json.loads(t)
        except json.JSONDecodeError:
            parsed = None
        else:
            if isinstance(parsed, list):
                return parsed, ""
            return None, "JSON 解析结果不是数组"

        start = t.find("[")
        if start == -1:
            return None, "JSON 解析失败（未找到数组）"

        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = json.loads(t[start:i + 1])
                    except json.JSONDecodeError:
                        return None, "JSON 解析失败"
                    if isinstance(candidate, list):
                        return candidate, ""
                    return None, "JSON 解析结果不是数组"
        return None, "JSON 解析失败（数组括号不匹配）"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _last_tool_result(trajectory: List[TrajectoryStep]) -> str:
        """取轨迹中最后一条 tool_result 的返回内容（不截断）。"""
        for step in reversed(trajectory):
            if step.action_type == "tool_result" and step.content:
                return step.content
        return ""

    @staticmethod
    def _read_file_content(path: str, limit: int = 20000) -> str:
        """读取文件内容：文本类直接读；二进制/图片标记为需多模态评估。"""
        p = Path(path)
        if not p.exists():
            return f"[文件不存在: {path}]"
        try:
            data = p.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            suffix = p.suffix.lower()
            return f"[二进制文件，类型 {suffix or '未知'}，路径 {path}，需多模态模型评估]"
        if len(data) > limit:
            return data[:limit] + f"...[截断，原{len(data)}字符]"
        return data

    @staticmethod
    def _load_rubric_text(rubric_path: str) -> str:
        """读取 rubric prompt 文件；相对路径按运行目录解析。"""
        if not rubric_path:
            return ""
        p = Path(rubric_path)
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.exists():
            return ""
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return ""

    @staticmethod
    def _build_conversation(
        input_messages: Optional[List[Dict[str, Any]]],
        input_text: str,
    ) -> str:
        """由用户输入 messages 构建 conversation 文本（仅人类轮次）。"""
        if input_messages:
            lines = []
            for msg in input_messages:
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get("role", "")).lower()
                if role not in ("user", "human"):
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(
                        str(item.get("text") or item.get("content") or "")
                        for item in content if isinstance(item, dict)
                    )
                lines.append(str(content))
            if lines:
                return "\n".join(lines)
        return input_text or ""

    @staticmethod
    def _render_rubric(rubric: str, *, year: int, conversation: str, memory_list: str) -> str:
        """填充 rubric 模板占位符，并将 {{ }} 转义为 { }。

        采用显式 replace 而非 str.format，避免 rubric 中 JSON 示例花括号
        与占位符混用带来的解析歧义；替换占位符前先把 {{ }} 还原为单层花括号，
        从而不影响 conversation / memory_list 内容中的花括号。
        """
        out = rubric.replace("{{", "{").replace("}}", "}")
        replacements = {
            "{current_year}": str(year),
            "{conversation}": conversation,
            "{history_memory_list}": "无",
            "{memory_list}": memory_list,
            "{peer_memory_list}": "无",
            "{fewshots}": "无",
        }
        for key, value in replacements.items():
            out = out.replace(key, value)
        return out

    @staticmethod
    def _build_overall_suggestions(
        process_details: Dict[str, Any],
        conciseness_details: Dict[str, Any],
        output_analyses: Dict[str, Dict[str, Any]],
    ) -> str:
        parts = []
        if process_details.get("analysis"):
            parts.append(process_details["analysis"])
        if conciseness_details.get("analysis"):
            parts.append(conciseness_details["analysis"])
        for otype, details in output_analyses.items():
            if details.get("analysis"):
                parts.append(f"[{otype}] {details['analysis']}")
        return "\n".join(parts)

    def _write_debug(self, tag: str, prompt: str, response: str = "", *, extra: Optional[Dict[str, Any]] = None):
        """Write debug files when verbose mode is on."""
        if not self._debug_dir:
            return
        (self._debug_dir / f"{tag}_prompt.txt").write_text(prompt, encoding="utf-8")
        if response:
            (self._debug_dir / f"{tag}_response.txt").write_text(response, encoding="utf-8")
        if extra:
            (self._debug_dir / f"{tag}_extra.json").write_text(
                json.dumps(extra, ensure_ascii=False, indent=2), encoding="utf-8")

    def _parse_json(self, text: str) -> Dict[str, Any]:
        """Parse JSON from text."""
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

    def _generate_and_parse(
        self,
        prompt: str,
        tag: str,
    ) -> Tuple[Dict[str, Any], str]:
        """Generate a model response and parse it as JSON.

        On a parse failure (non-empty response yielding no JSON object) the
        call is retried once. Returns ``(parsed_dict, error_str)`` where
        ``error_str`` is non-empty when evaluation produced no usable result
        (empty response, or parse failure that survived the retry).
        """
        response = self.model_client.generate([
            {"role": "user", "content": prompt}
        ])
        self._write_debug(tag, prompt, response)
        result = self._parse_json(response)

        if not result and response.strip():
            # 解析失败：保留首次响应，重试一次。
            if self._debug_dir:
                (self._debug_dir / f"{tag}_response_attempt1.txt").write_text(
                    response, encoding="utf-8")
            response = self.model_client.generate([
                {"role": "user", "content": prompt}
            ])
            self._write_debug(tag, prompt, response, extra={"parse_retried": True})
            result = self._parse_json(response)

        if result:
            return result, ""
        if not response.strip():
            return {}, "模型返回空响应"
        return {}, "JSON 解析失败（已重试一次）"

    def _heuristic_analyze(
        self,
        trajectory: List[TrajectoryStep],
        key_steps: List[Dict[str, Any]],
    ) -> SessionAnalysisResult:
        """Heuristic analysis (when no model is available)."""
        return SessionAnalysisResult(
            case_id="",
            process=0.0,
            conciseness=0.0,
            trajectory_quality=0.0,
            failure_modes=["未配置分析模型"],
        )
