"""
评估器模块
"""
from .base import (
    EvaluationRule,
    CompositeEvaluator,
    RuleResult,
    SideInfo
)
from .rules import (
    KeywordCoverageRule,
    CharacterOverlapRule,
    ExactMatchRule,
    LengthPenaltyRule,
    CoreTermAccuracyRule,
    LLMEvaluatorRule,
    create_default_rules,
    create_skill_rules,
    create_simple_rules
)
from .rubric import (
    LLMRubricBasedRule,
    EvaluationDetail,
    SubTypeSummary,
    Rubric,
    ItemScore,
    RubricItem,
    generate_sub_type_summaries
)
from .report import ReportGenerator


def create_evaluator(rules: list = None, use_llm: bool = False, model_client=None) -> CompositeEvaluator:
    """创建评估器
    
    Args:
        rules: 规则列表，默认使用默认规则
        use_llm: 是否使用LLM评估
        model_client: 模型客户端
        
    Returns:
        CompositeEvaluator: 组合评估器
    """
    if rules is None:
        if use_llm and model_client:
            evaluator = CompositeEvaluator(create_skill_rules())
            llm_rule = evaluator.get_rule("llm_semantic")
            if llm_rule:
                llm_rule.set_model_client(model_client)
        else:
            evaluator = CompositeEvaluator(create_simple_rules())
    else:
        evaluator = CompositeEvaluator(rules)
        
        if model_client:
            llm_rule = evaluator.get_rule("llm_semantic")
            if llm_rule:
                llm_rule.set_model_client(model_client)
    
    return evaluator
