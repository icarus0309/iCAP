"""
内置评估规则
"""
import re
from typing import Tuple, Optional, Set, List
from .base import EvaluationRule, RuleResult


class KeywordCoverageRule(EvaluationRule):
    """关键词覆盖率规则"""
    
    def __init__(self, weight: float = 0.3):
        super().__init__("keyword_coverage", weight)
    
    def extract_keywords(self, text: str) -> Set[str]:
        """提取2-6字的中文关键词"""
        return set(re.findall(r'[\u4e00-\u9fa5]{2,6}', text))
    
    def evaluate(self, output: str, expected: str) -> RuleResult:
        expected_keywords = self.extract_keywords(expected)
        output_keywords = self.extract_keywords(output)
        
        if not expected_keywords:
            return RuleResult(
                rule_name=self.name,
                score=0.0,
                feedback="期望输出为空",
                diagnostics={"expected_keywords": 0}
            )
        
        overlap = len(expected_keywords & output_keywords)
        score = overlap / len(expected_keywords)
        
        feedback = ""
        if score < 0.3:
            feedback = "缺少关键概念"
        elif score < 0.6:
            feedback = "部分关键概念未覆盖"
        
        return RuleResult(
            rule_name=self.name,
            score=score,
            feedback=feedback,
            diagnostics={
                "overlap": overlap,
                "expected": len(expected_keywords),
                "coverage": f"{score:.1%}"
            }
        )


class CharacterOverlapRule(EvaluationRule):
    """字符重叠率规则"""
    
    def __init__(self, weight: float = 0.2):
        super().__init__("character_overlap", weight)
    
    def evaluate(self, output: str, expected: str) -> RuleResult:
        output_chars = set(output)
        expected_chars = set(expected)
        
        if not expected_chars:
            return RuleResult(
                rule_name=self.name,
                score=0.0,
                feedback="期望输出为空"
            )
        
        intersection = len(output_chars & expected_chars)
        union = len(output_chars | expected_chars)
        
        if union == 0:
            return RuleResult(
                rule_name=self.name,
                score=0.0,
                feedback="无法计算重叠率"
            )
        
        score = intersection / union
        
        return RuleResult(
            rule_name=self.name,
            score=score,
            feedback="",
            diagnostics={
                "intersection": intersection,
                "union": union,
                "overlap": f"{score:.1%}"
            }
        )


class ExactMatchRule(EvaluationRule):
    """完全匹配规则"""
    
    def __init__(self, weight: float = 1.0):
        super().__init__("exact_match", weight)
    
    def evaluate(self, output: str, expected: str) -> RuleResult:
        output_clean = self._clean_text(output)
        expected_clean = self._clean_text(expected)
        
        if output_clean == expected_clean:
            return RuleResult(
                rule_name=self.name,
                score=1.0,
                feedback="完全匹配",
                diagnostics={"matched": True}
            )
        
        return RuleResult(
            rule_name=self.name,
            score=0.0,
            feedback="输出与期望不完全匹配",
            diagnostics={"matched": False}
        )
    
    def _clean_text(self, text: str) -> str:
        text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
        text = re.sub(r'\*(.*?)\*', r'\1', text)
        text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text


class LengthPenaltyRule(EvaluationRule):
    """长度惩罚规则 - 惩罚过长或过短的输出"""
    
    def __init__(self, 
                 weight: float = 0.1,
                 min_length: int = 10,
                 max_length: int = 500,
                 ideal_ratio: float = 0.8):
        super().__init__("length_penalty", weight)
        self.min_length = min_length
        self.max_length = max_length
        self.ideal_ratio = ideal_ratio
    
    def evaluate(self, output: str, expected: str) -> RuleResult:
        output_len = len(output)
        expected_len = len(expected)
        
        if expected_len == 0:
            return RuleResult(
                rule_name=self.name,
                score=0.0,
                feedback="期望输出为空"
            )
        
        ratio = output_len / expected_len
        
        if output_len < self.min_length:
            score = 0.0
            feedback = f"输出过短 (< {self.min_length}字符)"
        elif output_len > self.max_length:
            score = 0.0
            feedback = f"输出过长 (> {self.max_length}字符)"
        elif abs(ratio - self.ideal_ratio) < 0.2:
            score = 1.0
            feedback = ""
        else:
            score = max(0.0, 1.0 - abs(ratio - self.ideal_ratio))
            feedback = "长度偏离期望"
        
        return RuleResult(
            rule_name=self.name,
            score=score,
            feedback=feedback,
            diagnostics={
                "output_length": output_len,
                "expected_length": expected_len,
                "ratio": f"{ratio:.2f}"
            }
        )


class CoreTermAccuracyRule(EvaluationRule):
    """专业术语准确性规则"""
    
    def __init__(self, 
                 weight: float = 0.2,
                 core_terms: Optional[Set[str]] = None):
        super().__init__("core_term_accuracy", weight)
        self.core_terms = core_terms or {
            "Skill", "Agent", "创建", "优化", "组合", "管道", "生命周期",
            "注册", "激活", "执行", "监控", "版本", "权限", "测试",
            "单元测试", "集成测试", "性能", "缓存", "超时", "重试"
        }
    
    def evaluate(self, output: str, expected: str) -> RuleResult:
        output_terms = set(re.findall(r'[\u4e00-\u9fa5]{2,6}|[A-Za-z]+', output))
        expected_terms = set(re.findall(r'[\u4e00-\u9fa5]{2,6}|[A-Za-z]+', expected))
        
        relevant_core_terms = expected_terms & self.core_terms
        if not relevant_core_terms:
            return RuleResult(
                rule_name=self.name,
                score=0.5,
                feedback="无相关核心术语"
            )
        
        overlap = len(output_terms & relevant_core_terms)
        score = overlap / len(relevant_core_terms)
        
        feedback = ""
        if score < 0.4:
            feedback = "专业术语使用不足"
        
        return RuleResult(
            rule_name=self.name,
            score=score,
            feedback=feedback,
            diagnostics={
                "overlap": overlap,
                "expected_terms": len(relevant_core_terms),
                "accuracy": f"{score:.1%}"
            }
        )


class LLMEvaluatorRule(EvaluationRule):
    """LLM评估规则 - 使用LLM判断语义准确性"""
    
    def __init__(self, 
                 weight: float = 0.5,
                 model_client=None):
        super().__init__("llm_semantic", weight)
        self._model_client = model_client
    
    def set_model_client(self, model_client):
        """设置模型客户端"""
        self._model_client = model_client
    
    @property
    def model_client(self):
        return self._model_client
    
    def evaluate(self, output: str, expected: str) -> RuleResult:
        if self.model_client is None:
            return RuleResult(
                rule_name=self.name,
                score=0.5,
                feedback="未配置模型客户端",
                diagnostics={"error": "no_model_client"}
            )
        
        try:
            score, feedback = self._evaluate_with_llm(output, expected)
            return RuleResult(
                rule_name=self.name,
                score=score,
                feedback=feedback,
                diagnostics={"llm_score": score}
            )
        except Exception as e:
            return RuleResult(
                rule_name=self.name,
                score=0.5,
                feedback=f"LLM评估失败: {e}",
                diagnostics={"error": str(e)}
            )
    
    def _evaluate_with_llm(self, output: str, expected: str) -> Tuple[float, str]:
        eval_prompt = f"""请评估以下回答的质量。

## 期望答案
{expected}

## 实际回答
{output}

请给出0-100的分数和一句话评语。
格式: 分数: <0-100>
评语: <一句话>"""

        response = self.model_client.generate([
            {"role": "system", "content": "你是评估专家。只输出指定格式。"},
            {"role": "user", "content": eval_prompt}
        ], temperature=0.3, max_tokens=200)
        
        score_match = re.search(r'分数:\s*(\d+)', response)
        feedback_match = re.search(r'评语:\s*(.+)', response)
        
        score = int(score_match.group(1)) / 100.0 if score_match else 0.5
        feedback = feedback_match.group(1).strip() if feedback_match else ""
        
        return score, feedback


def create_default_rules() -> List[EvaluationRule]:
    """创建默认评估规则"""
    return [
        KeywordCoverageRule(weight=0.3),
        CharacterOverlapRule(weight=0.4),
        LengthPenaltyRule(weight=0.1),
        CoreTermAccuracyRule(weight=0.2)
    ]


def create_skill_rules() -> List[EvaluationRule]:
    """创建Skill领域评估规则"""
    return [
        KeywordCoverageRule(weight=0.3),
        LLMEvaluatorRule(weight=0.5),
        CoreTermAccuracyRule(weight=0.2)
    ]


def create_simple_rules() -> List[EvaluationRule]:
    """创建简单评估规则 (无LLM)"""
    return [
        KeywordCoverageRule(weight=0.6),
        CharacterOverlapRule(weight=0.4)
    ]
