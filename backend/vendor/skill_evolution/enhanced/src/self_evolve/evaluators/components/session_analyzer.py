"""Multi-dimensional session analysis (三维度：过程 / 精简 / 结果)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ....model_client import ModelClient, QuotaExceededError
from ...schemas import SessionAnalysisResult, TrajectoryStep, to_plain_dict


# 结果子类型（数据集 output 的 type 允许这三种）
RESULT_TYPES = ("text", "tool", "file")


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
        execution_mode: str = "hermes",
        input_messages=None, final_output="", judge_template="", judge_fields=None, skill_text="",
    ) -> SessionAnalysisResult:
        """
        Analyze session and generate three-dimension scores.

        expected_outputs: 按 type 聚合的期望输出，如
            {"text": "...", "tool": "...", "file": {"expect_path": "...", "actual_path": "..."}}
        judgment_weights: {process, conciseness, result}，权重为 0 的维度跳过评估。
        output_weights: {text, tool, file}，权重为 0 或类型缺失的子类型跳过评估。
        """
        if execution_mode == "direct":
            return self._analyze_direct(case_id=case_id, input_messages=input_messages or [],
                final_output=final_output, expected_outputs=expected_outputs or {},
                output_weights=output_weights or {}, judge_template=judge_template,
                judge_fields=judge_fields, skill_text=skill_text, output_path=output_path)
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
        evaluation_errors: List[str] = []

        # 维度 1: 过程（权重非零才评估；解析失败重试一次）
        if self.model_client and judgment_weights.get("process", 0.0) > 0:
            process, process_details = self._analyze_dimension(
                self._analyze_process, trajectory=trajectory, key_steps=key_steps,
            )
            if process_details.get("parse_failed"):
                evaluation_errors.append("process")

        # 维度 2: 精简（权重非零才评估；解析失败重试一次）
        if self.model_client and judgment_weights.get("conciseness", 0.0) > 0:
            conciseness, conciseness_details = self._analyze_dimension(
                self._analyze_conciseness, trajectory=trajectory, input_text=input_text,
            )
            if conciseness_details.get("parse_failed"):
                evaluation_errors.append("conciseness")

        # 维度 3: 结果（按 output.type 分发；权重非零才评估；解析失败重试一次）
        for otype in RESULT_TYPES:
            if otype not in expected_outputs:
                continue
            if output_weights.get(otype, 0.0) <= 0:
                continue
            expected = expected_outputs[otype]
            if otype == "text":
                score, details = self._analyze_dimension(
                    self._analyze_text,
                    trajectory=trajectory, input_text=input_text, expected_text=expected,
                )
            elif otype == "tool":
                score, details = self._analyze_dimension(
                    self._analyze_tool,
                    trajectory=trajectory, expected_tool=expected,
                )
            else:  # file
                score, details = self._analyze_dimension(
                    self._analyze_file, expected_file=expected,
                )
            if details.get("parse_failed"):
                evaluation_errors.append(f"result:{otype}")
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
            evaluation_error="; ".join(evaluation_errors),
        )

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(to_plain_dict(result), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        return result

    def _analyze_direct(self, *, case_id, input_messages, final_output, expected_outputs,
                        output_weights, judge_template, judge_fields, skill_text, output_path):
        from .direct_judge import parse_judge_result, render_judge_prompt
        from ...evaluation_policy import fingerprint, write_json
        result = SessionAnalysisResult(case_id=case_id, execution_mode="direct",
            evaluation_mode="result_only", evaluated_dimensions=["result"],
            input_messages=input_messages, final_output=final_output)
        errors = []
        for kind in ("word", "format"):
            if kind not in expected_outputs or output_weights.get(kind, 0) <= 0:
                continue
            if kind == "word":
                score, details = self._analyze_word(final_output=final_output, expected_word=expected_outputs[kind])
            else:
                score, details = self._analyze_format(final_output=final_output, expected_format=expected_outputs[kind])
            result.output_scores[kind] = score
            result.output_analyses[kind] = details
        for kind in ("onlyllm", "text", "tool", "file"):
            if kind not in expected_outputs or output_weights.get(kind, 0) <= 0:
                continue
            def judge():
                try:
                    if not self.model_client or not final_output.strip():
                        raise ValueError("missing judge client or current final output")
                    if kind == "onlyllm":
                        template = judge_template or Path(expected_outputs[kind]).read_text(encoding="utf-8")
                        if not template.strip():
                            raise ValueError("empty judge template")
                        prompt = render_judge_prompt(template, messages=input_messages, output=final_output,
                                                     fields=judge_fields, skill_text=skill_text)
                        template_id = fingerprint(template)
                    else:
                        expected = expected_outputs[kind]
                        if kind == "file":
                            expected = (judge_fields or {}).get("expected_file_content")
                            if expected is None:
                                expected = Path(expected_outputs[kind]["expect_path"]).read_text(encoding="utf-8")
                        prompt = ("只评估本次最终输出是否满足输入和参考结果，不评过程或简洁度。"
                                  "返回 JSON object，包含 score（0–100）和 analysis（证据及问题）。\n" +
                                  json.dumps({"input_messages": input_messages, "candidate_output": final_output,
                                              "expected_output": expected}, ensure_ascii=False))
                        template_id = "direct_result_v1"
                    response = self.model_client.generate([{"role": "user", "content": prompt}])
                    parsed = self._parse_json_strict(response)
                    score = parse_judge_result(parsed)
                    return score, {"judge_result": parsed, "raw_response": response,
                        "analysis": json.dumps(parsed, ensure_ascii=False), "score": score,
                        "template_id": template_id, "source": "model"}
                except QuotaExceededError:
                    raise
                except Exception as exc:
                    return 0.0, {"parse_failed": True, "error": str(exc)}
            score, details = self._analyze_dimension(judge)
            result.output_scores[kind] = score
            result.output_analyses[kind] = details
            if details.get("parse_failed"):
                errors.append(f"{kind}: {details.get('error')}")
        if not result.output_scores:
            errors.append("no effective result judge")
        result.evaluation_error = "; ".join(errors)
        if output_path:
            write_json(output_path, to_plain_dict(result))
        return result

    def _analyze_dimension(self, fn, **kwargs) -> Tuple[float, Dict[str, Any]]:
        """Call a dimension analyzer, retrying once on parse failure.

        Each ``_analyze_*`` returns ``details["parse_failed"]=True`` when the
        model response could not be parsed (or the call raised). We retry the
        exact same dimension once; if it still fails the flag remains set so
        the caller can mark the case invalid.
        """
        score, details = fn(**kwargs)
        if details.get("parse_failed"):
            score, details = fn(**kwargs)
        return score, details

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
            response = self.model_client.generate([
                {"role": "user", "content": prompt}
            ])
            self._write_debug("process", prompt, response)
            result = self._parse_json_strict(response)

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

        except QuotaExceededError:
            raise
        except Exception as exc:
            return 0.0, {"error": str(exc), "parse_failed": True}

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
            response = self.model_client.generate([
                {"role": "user", "content": prompt}
            ])
            self._write_debug("conciseness", prompt, response)
            result = self._parse_json_strict(response)

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

        except QuotaExceededError:
            raise
        except Exception as exc:
            return 0.0, {"error": str(exc), "parse_failed": True}

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
            response = self.model_client.generate([
                {"role": "user", "content": prompt}
            ])
            self._write_debug("text", prompt, response)
            result = self._parse_json_strict(response)

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

        except QuotaExceededError:
            raise
        except Exception as exc:
            return 0.0, {"error": str(exc), "parse_failed": True}

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
            response = self.model_client.generate([
                {"role": "user", "content": prompt}
            ])
            self._write_debug("tool", prompt, response)
            result = self._parse_json_strict(response)

            score = result.get("score", 0.0)
            return score, {
                "score": score,
                "matched": result.get("matched", []),
                "mismatched": result.get("mismatched", []),
                "analysis": result.get("analysis", ""),
                "source": "model",
            }

        except QuotaExceededError:
            raise
        except Exception as exc:
            return 0.0, {"error": str(exc), "parse_failed": True}

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
            response = self.model_client.generate([
                {"role": "user", "content": prompt}
            ])
            self._write_debug("file", prompt, response)
            result = self._parse_json_strict(response)

            score = result.get("score", 0.0)
            return score, {
                "score": score,
                "expect_path": expect_path,
                "actual_path": actual_path,
                "analysis": result.get("analysis", ""),
                "source": "model",
            }

        except QuotaExceededError:
            raise
        except Exception as exc:
            return 0.0, {"error": str(exc), "parse_failed": True, "expect_path": expect_path, "actual_path": actual_path}

    # ------------------------------------------------------------------
    # 结果子类型：word（关键词）与 format（格式）确定性校验（不调模型）
    # ------------------------------------------------------------------

    def _analyze_word(self, *, final_output: str, expected_word: Any) -> Tuple[float, Dict[str, Any]]:
        """关键词校验：黑名单（不能出现）+ 白名单（必须出现）字符串硬匹配（确定性规则）。

        expected_word 形如 {"blacklist": ["...", ...], "whitelist": ["...", ...]}。
        命中任一条黑名单字眼、或缺失任一条白名单字眼均计一次失败，score 按通过项占比给分。
        黑/白名单均为空时，视为该用例期望"不提取内容"：输出须为空数组 []，输出非空判 0 分。
        """
        if not isinstance(expected_word, dict):
            return 0.0, {"analysis": "word 期望需提供 blacklist/whitelist", "source": "rule"}

        blacklist = [str(w) for w in (expected_word.get("blacklist") or []) if str(w).strip()]
        whitelist = [str(w) for w in (expected_word.get("whitelist") or []) if str(w).strip()]

        hit_black = [w for w in blacklist if w in final_output]
        miss_white = [w for w in whitelist if w not in final_output]

        total = len(blacklist) + len(whitelist)
        failed = len(hit_black) + len(miss_white)

        if total == 0:
            parsed, parse_err = self._parse_json_array(final_output)
            if parse_err:
                score = 0.0
                passed = False
                analysis = "关键词校验未通过：期望输出为空，但输出无法解析（" + parse_err + "）"
            elif parsed == []:
                score = 100.0
                passed = True
                analysis = "关键词校验通过：输出为空（未提取内容）"
            else:
                score = 0.0
                passed = False
                analysis = "关键词校验未通过：期望输出为空，但模型输出了内容"
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

    def _analyze_format(self, *, final_output: str, expected_format: Any) -> Tuple[float, Dict[str, Any]]:
        """格式校验：模型最终输出须为符合格式示例的 JSON 数组或单个对象（确定性规则）。

        - 空数组 []（如记忆萃取无结果）视为通过；
        - 单个对象 {"意图": "..."} 视为单元素数组参与校验；
        - 非空时逐元素检查：必须是对象且字段集合与格式示例一致。
        expected_format 可为 JSON 字符串、list/dict；为空时回退 .env 的 OUTPUT_FORMAT_EXAMPLE。
        """
        format_example = self._resolve_format_example(expected_format)
        if format_example is None:
            return 0.0, {
                "analysis": "未配置格式示例（OUTPUT_FORMAT_EXAMPLE）或格式示例解析失败",
                "source": "rule",
            }

        if isinstance(format_example, dict):
            example_objects = [format_example]
        else:
            example_objects = [o for o in format_example if isinstance(o, dict)]
        required_keys = sorted({k for o in example_objects for k in o.keys()})

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
        """从模型输出中稳健解析 JSON（数组或单个对象），返回 (list_or_None, error_str)。

        - 支持裸 JSON、markdown 围栏包裹；
        - 单个对象 {"意图": "..."} 会被包装为 [{"意图": "..."}]；
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
            if isinstance(parsed, dict):
                return [parsed], ""
            return None, "JSON 解析结果不是数组或对象"

        for open_ch, close_ch in (("[", "]"), ("{", "}")):
            start = t.find(open_ch)
            if start == -1:
                continue
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
                elif ch == open_ch:
                    depth += 1
                elif ch == close_ch:
                    depth -= 1
                    if depth == 0:
                        try:
                            candidate = json.loads(t[start:i + 1])
                        except json.JSONDecodeError:
                            return None, "JSON 解析失败"
                        if isinstance(candidate, list):
                            return candidate, ""
                        if isinstance(candidate, dict):
                            return [candidate], ""
                        return None, "JSON 解析结果不是数组或对象"
            return None, f"JSON 解析失败（{open_ch} 括号不匹配）"
        return None, "JSON 解析失败（未找到数组或对象）"

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

    def _parse_json_strict(self, text: str) -> Dict[str, Any]:
        """Parse JSON; raise ValueError on failure (drives dimension-level retry)."""
        result = self._parse_json(text)
        if not result:
            raise ValueError("模型返回无法解析的 JSON")
        return result

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
