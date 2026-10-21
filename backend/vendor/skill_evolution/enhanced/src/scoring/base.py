"""
评估器模块 - 乐高化设计
"""
from dataclasses import dataclass, field
from typing import Dict, Any, List, Callable, Optional, Tuple
from abc import ABC, abstractmethod


@dataclass
class RuleResult:
    """单条规则的评估结果"""
    rule_name: str
    score: float
    feedback: str = ""
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "score": self.score,
            "feedback": self.feedback,
            "diagnostics": self.diagnostics
        }


class EvaluationRule(ABC):
    """评估规则基类"""

    def __init__(self, name: str, weight: float = 1.0):
        self.name = name
        self.weight = weight

    @abstractmethod
    def evaluate(self, output: str, expected: str) -> RuleResult:
        """评估单个输出

        Args:
            output: 实际输出
            expected: 期望输出

        Returns:
            RuleResult: 评估结果
        """
        pass

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name}, weight={self.weight})"


class CompositeEvaluator:
    """组合评估器 - 整合多条规则"""

    def __init__(self, rules: Optional[List[EvaluationRule]] = None):
        self.rules: List[EvaluationRule] = rules or []
        self._rule_cache: Dict[str, EvaluationRule] = {}
        for rule in self.rules:
            self._rule_cache[rule.name] = rule

    def add_rule(self, rule: EvaluationRule) -> "CompositeEvaluator":
        """添加评估规则 (链式调用)"""
        self.rules.append(rule)
        self._rule_cache[rule.name] = rule
        return self

    def remove_rule(self, name: str) -> bool:
        """移除评估规则"""
        for i, rule in enumerate(self.rules):
            if rule.name == name:
                del self.rules[i]
                del self._rule_cache[name]
                return True
        return False

    def get_rule(self, name: str) -> Optional[EvaluationRule]:
        """获取指定规则"""
        return self._rule_cache.get(name)

    def evaluate(self, output: str, expected: str, input_text: str = "", sub_type: str = "") -> Tuple[float, "SideInfo"]:
        """综合评估

        Args:
            output: 实际输出
            expected: 期望输出
            input_text: 输入文本（可选）
            sub_type: 数据子类型（可选）

        Returns:
            (综合得分, SideInfo)
        """
        if not output or not expected:
            return 0.0, SideInfo(0.0, "输出或期望为空")

        total_weight = sum(rule.weight for rule in self.rules)
        if total_weight == 0:
            return 0.0, SideInfo(0.0, "没有有效的评估规则")

        rule_results: List[RuleResult] = []
        weighted_sum = 0.0

        for rule in self.rules:
            # 尝试传递 input_text 和 sub_type 参数（如果规则支持）
            try:
                result = rule.evaluate(output, expected, input_text=input_text, sub_type=sub_type)
            except TypeError:
                # 如果规则不支持这些参数，回退到基本调用
                result = rule.evaluate(output, expected)
            rule_results.append(result)
            weighted_sum += result.score * rule.weight

        final_score = weighted_sum / total_weight

        all_feedback = [r.feedback for r in rule_results if r.feedback]
        feedback = "；".join(all_feedback) if all_feedback else "评估完成"

        diagnostics = {
            "rule_count": len(self.rules),
            "rules": {r.rule_name: r.to_dict() for r in rule_results}
        }

        return final_score, SideInfo(final_score, feedback, diagnostics)

    def __len__(self) -> int:
        return len(self.rules)

    def __repr__(self) -> str:
        return f"CompositeEvaluator(rules={self.rules})"


@dataclass
class SideInfo:
    """ASI (Actionable Side Information) - 可操作的侧信息"""
    score: float
    feedback: str = ""
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def to_text(self) -> str:
        text = f"得分: {self.score:.3f}\n"
        if self.feedback:
            text += f"反馈: {self.feedback}\n"
        if self.diagnostics:
            text += "诊断信息:\n"
            for key, value in self.diagnostics.items():
                if isinstance(value, dict):
                    text += f"  - {key}:\n"
                    for k, v in value.items():
                        text += f"    - {k}: {v}\n"
                else:
                    text += f"  - {key}: {value}\n"
        return text
