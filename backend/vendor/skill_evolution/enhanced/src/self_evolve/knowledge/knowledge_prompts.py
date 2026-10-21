"""Prompts for dual-model divergence and natural-text knowledge extraction.

Migrated from ``skill_distill_multi_dim/src/trace2skill/knowledge_prompts.py``. The
divergence / verification prompts are unchanged; the knowledge-extraction prompt is
rewritten to emit a **natural-text** ``case_analysis`` (``root_cause`` + ``suggestion``)
instead of a patch with ``edits``.
"""

from __future__ import annotations

import json
from typing import Any


FIRST_DIVERGENCE_SYSTEM = """# 角色

你是双模型轨迹的首次关键不一致定位器。

## 任务

- 输入包含同一个任务、同一个 Key Step 定义、小模型和大模型在该 Key Step 下各自对应的实际轨迹，以及只用于检查恢复情况的有限 lookahead。
- 如果更早的候选差异已被复核否决，输入还会包含此前所有 Key Steps 的证据；这些证据只用于理解决策如何延续、分支为何不同以及当前步骤单边缺失是否有行为意义。
- 判断当前 Key Step 是否出现了能够解释“小模型 Output 失败而大模型 Output 成功”的首次关键差异。

## 归因规则

- 只把当前 Key Step 中发生的差异归因到当前步骤。
- lookahead 只能用于否证当前错误仍然有效；禁止把 lookahead 中新出现的错误提前归因到当前 Key Step。
- 忽略不影响结果的措辞、格式、冗余查询和等价工具差异。
- 关键差异必须同时具备：小模型的明确错误、大模型的对应正确行为，以及与 Output 失败之间的因果关系。对应正确行为可以表现为执行了不同动作，也可以表现为依据前序判断正确地停止、等待、跳过或未进入当前步骤。
- 所有结论必须引用实际 trajectory step；证据不足时返回未发现。
- reasoning 缺失、为 null 或为空，只代表没有可用推理文本，不能据此判定小模型错误。
- 本阶段只定位当前 Case 的差异证据，不直接产出 Skill 知识。
- 不得照抄完整用户 Query 或固定答案；哪些场景、参数和命令细节应进入 Skill，由后续知识提取阶段判断。

## 恢复检查

- 判断前必须按时间顺序完整检查当前 Key Step 的全部轨迹。
- 特别检查小模型在报错、失败调用或错误尝试后，是否通过重试或自我修正，成功取得完成本步骤所需的等价信息或状态。
- 禁止看到第一个 error 就停止分析，也禁止只引用失败事件而忽略同一 Key Step 中更晚的恢复事件。
- 关键差异以“Key Step 结束时仍然存在，并继续影响任务的信息、状态或决策差异”为准，不以中间过程是否出现过错误为准。
- 如果小模型在进入后续规划或不可逆操作前已经恢复，并最终取得与大模型等价的必要信息或状态，则该错误只是已恢复的瞬时差异；必须返回 `critical_difference_found=false`、`causal_to_output_failure=false`。
- 额外耗时、额外查询、路径探索或重试次数本身不构成 Output 失败原因，除非轨迹明确显示它耗尽硬性预算、造成不可逆副作用，或导致必要信息在后续仍然缺失。

## 因果证据要求

- 不得使用“可能导致”“也许”“为后续奠定基础”等推测建立因果关系。
- 必须指出可观察的数据流：当前步骤遗留了什么错误或缺失信息，该信息如何进入错误决策或动作，并如何对应 Output 失败。
- expected output 和总分标签只能说明结果，不能作为当前步骤的因果证据。
- 当前 Key Step 只有一侧存在轨迹时，不得仅因另一侧为空就跳过，也不得仅凭为空就认定错误。必须结合 Key Step 的职责与依赖、此前 Key Steps 的实际证据、当前存在侧的动作，以及 ground truth 所要求的结果分支，说明缺失侧为何是正确停止/等待或错误遗漏。
- 单边步骤差异若被判为关键，必须在 `absence_significance` 中说明：缺失的是哪一侧、前序哪个判断导致双方走向不同分支，以及该分支如何对应最终成败。

## 输出要求

- 只返回 JSON。
"""


DIVERGENCE_VERIFICATION_SYSTEM = """# 角色

你是首次关键不一致的恢复与因果复核器。

## 任务

- 候选定位器已提出当前 Key Step 可能包含首个关键差异。
- 使用当前步骤的完整事件、此前 Key Steps 的可用证据、有限 recovery lookahead、Key Step 定义、ground truth 和候选差异进行独立复核。
- 重点判断：差异是否在规划或不可逆动作前恢复、步骤末态是否等价，以及是否仍有可观察的未解决后果进入最终失败。

## 复核规则

- lookahead 只能用于否证当前候选，不能把其中新发生的错误归因给当前步骤。
- 如果错误已恢复、末态等价、未解决后果为空，或因果说明依赖“可能”“也许”“奠定基础”等推测，必须否决候选。
- 只有轨迹明确显示未恢复的数据或状态差异进入后续错误决策，或耗尽硬性预算时，才能确认候选。
- 当前步骤一侧为空时，检查这是否是前序决策造成的合理分支，例如正确停止、等待外部确认或跳过不应执行的动作；另一侧实际进入该步骤并产生了与 ground truth 不符的结果时，可以确认单边关键差异。
- 单边缺失本身不是充分证据。必须同时核对当前存在侧的实际步骤、此前双方证据、Key Step 的职责/依赖和 ground truth，并在 `absence_significance` 中给出可观察的因果说明。
- 所有证据 step 必须来自提供的当前轨迹、此前 Key Step 证据或 recovery lookahead；lookahead 中的新错误仍不得提前归因。

## 输出要求

- 只返回 JSON。
"""


KNOWLEDGE_EXTRACTION_SYSTEM = """# 角色

你是面向 Skill 蒸馏的关键知识提取器。

## 任务

- 当前 Case 已满足：小模型 Output 失败、大模型 Output 成功，且系统已按 Key Steps 从前到后定位首次关键不一致。
- 基于该差异、小模型错误轨迹，以及大模型在该 Key Step 下全部实际步骤的 reasoning、content 和 tool_calls，提取一条最小、有证据且可执行的自然文本知识。

## 证据与边界

- 以首次关键不一致为边界，不得使用后续结果倒推。
- 当前 Case 是知识证据来源；不得补写轨迹中不存在的信息。
- reasoning 缺失时，只使用其他实际证据。
- 原始 reasoning 只能作为证据，不能原样写入最终 Skill，但可参考大模型 reasoning 中的思考步骤提炼出核心逻辑。
- 区分可跨案例比较的决策模式与偶然样本事实；不得把单个现象夸大为领域规律或直接写成规则。

## 知识内容

- 必须提取触发条件、正确判断/动作背后的模式、应避免的错误和适用边界。
- 结论必须能跨案例比较：描述“哪类前提下应做何种判断/校验/顺序控制”，而非复述一次调用。
- 可以写出场景名或场景类型以限定适用范围；不得写出设备/对象名、工具/API/CLI、命令、flags、参数名或值、公式、固定动作序列。

## 泄漏与有效性规则

- 不得照抄或近似复述完整用户 Query、Case ID 或固定测试答案。
- 知识不得包含 Case ID、完整或近似完整的测试 Query、固定测试答案，或特定接口/参数。
- 没有可比较的、由证据支持的模式时，返回 `valid=false`；不能仅因知识来自单个 Case 或适用范围较窄而判定无效。
- 网络、超时、工具异常或证据不足时，返回 `valid=false`。

## 输出形态

- 输出**自然文本知识经验**，不输出任何编辑操作（无 `edits` / `file` / `op` / `target`）。
- `case_analysis.root_cause` + `case_analysis.suggestion` 是最终要注入 SKILL 变异 prompt 的自然文本规则；`root_cause` 描述验证过的根因，`suggestion` 描述触发条件、正确行为/不变量、应避免的错误和适用边界（可包含实例或取值）。

## 输出要求

- 只返回 JSON。
"""


def _divergence_output_schema() -> dict[str, Any]:
    return {
        "step_id": "step2",
        "critical_difference_found": True,
        "causal_to_output_failure": True,
        "small_error": "小模型在该步骤做错了什么",
        "large_correct_behavior": "大模型在该步骤做对了什么",
        "small_evidence_steps": [3],
        "large_evidence_steps": [4, 5],
        "transient_error_found": False,
        "recovered_before_decision": False,
        "recovery_evidence_steps": [],
        "end_of_step_equivalent": False,
        "one_sided_step_difference": False,
        "missing_trajectory_side": "none|small|large",
        "absence_significance": "若当前 Key Step 仅一侧有轨迹，说明该缺失为何代表正确分支或错误遗漏；否则留空",
        "unresolved_consequence": "仍缺失的信息如何进入后续错误动作并对应 Output 失败",
        "reason": "为何是当前最早且影响结果的关键差异",
    }


def first_divergence_prompt(payload: dict[str, Any]) -> str:
    return (
        "# 首次关键不一致定位\n\n"
        "## 输出结构\n\n"
        f"{json.dumps(_divergence_output_schema(), ensure_ascii=False, indent=2)}\n\n"
        "## 当前 Key Step 比较输入\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}"
    )


def divergence_verification_prompt(payload: dict[str, Any]) -> str:
    return (
        "# 首次关键不一致复核\n\n"
        "## 输出结构\n\n"
        f"{json.dumps(_divergence_output_schema(), ensure_ascii=False, indent=2)}\n\n"
        "## 强制约束\n\n"
        "- `recovered_before_decision` 或 `end_of_step_equivalent` 为 `true` 时，必须同时将 "
        "`critical_difference_found` 和 `causal_to_output_failure` 设为 `false`。\n"
        "- 当前 Key Step 只有一侧有轨迹且判定为关键差异时，必须填写 `absence_significance`；"
        "不得因为另一侧为空而自动否决或自动确认。\n\n"
        "## 候选与复核证据输入\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}"
    )


def knowledge_extraction_prompt(payload: dict[str, Any]) -> str:
    schema = {
        "schema": "skillimprove.knowledge.v1",
        "valid": True,
        "artifact_type": "knowledge",
        "first_divergent_step_id": "step2",
        "knowledge_type": "procedure|decision_pattern|constraint|verification|recovery",
        "trigger": "可包含场景限定、但不包含对象和参数的触发条件",
        "knowledge": "可跨案例比较的正确决策模式，不含接口、命令或参数",
        "anti_pattern": "应避免的通用错误模式",
        "applicability_boundary": "适用范围、前提和不适用边界",
        "confidence": 0.9,
        "case_analysis": {
            "root_cause": "首次关键不一致的已验证根因",
            "suggestion": "同类场景下的触发条件、关键语义依赖与正确处理机制（可包含实例或取值）",
            "improvement_direction": "后续 Skill 应增加或修复的通用方向",
            "applicability_boundary": "适用前提和边界",
            "evidence_summary": "不含个案参数的证据摘要",
        },
    }
    return (
        "# 关键知识提取\n\n"
        "## 输出结构\n\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        "## 知识证据输入\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}"
    )


__all__ = [
    "DIVERGENCE_VERIFICATION_SYSTEM",
    "FIRST_DIVERGENCE_SYSTEM",
    "KNOWLEDGE_EXTRACTION_SYSTEM",
    "divergence_verification_prompt",
    "first_divergence_prompt",
    "knowledge_extraction_prompt",
]
