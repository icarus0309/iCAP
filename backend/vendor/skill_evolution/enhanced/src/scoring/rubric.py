"""
LLM基于评分标准的评估规则
1. 根据skill.md生成评分标准
2. 根据评分标准对测试结果进行打分
3. 生成表格报告和子类型总结
"""
import re
from dataclasses import dataclass, field
from typing import Tuple, List, Dict, Any, Optional
from .base import EvaluationRule, RuleResult


@dataclass
class RubricItem:
    """评分标准项"""
    category: str           # 类别
    weight: float           # 权重
    description: str        # 描述
    level: str = "一般"      # 重要程度: 必要/重要/一般


@dataclass
class Rubric:
    """评分标准"""
    skill_name: str
    description: str
    items: List[RubricItem]
    total_score: int = 100
    grading_levels: Dict[str, str] = field(default_factory=dict)  # 分数段对应描述


@dataclass
class ItemScore:
    """单项评分结果"""
    category: str
    score: int
    weight: int = 0
    level: str = ""
    reason: str = ""


@dataclass
class EvaluationDetail:
    """单次评估详情"""
    item_id: str
    sub_type: str
    input_text: str
    expected: str
    actual: str
    total_score: float
    item_scores: List[ItemScore]
    summary: str


@dataclass
class SubTypeSummary:
    """子类型汇总"""
    sub_type: str
    avg_score: float
    count: int
    reason: str


class LLMRubricBasedRule(EvaluationRule):
    """基于LLM评分标准的评估规则"""
    
    def __init__(self, 
                 weight: float = 1.0,
                 model_client=None,
                 skill_content: str = ""):
        super().__init__("llm_rubric_based", weight)
        self._model_client = model_client
        self._skill_content = skill_content
        self._rubric: Optional[Rubric] = None
    
    def set_model_client(self, model_client):
        """设置模型客户端"""
        self._model_client = model_client
    
    @property
    def model_client(self):
        return self._model_client
    
    def set_skill_content(self, content: str):
        """设置skill内容"""
        self._skill_content = content
        self._rubric = None  # 重置评分标准
    
    def load_rubric_from_file(self, file_path: str) -> Rubric:
        """从文件加载评分标准（支持 .json 和 .md 格式）"""
        if file_path.endswith('.json'):
            return self._load_rubric_from_json(file_path)
        else:
            return self._load_rubric_from_md(file_path)
    
    def _load_rubric_from_json(self, file_path: str) -> Rubric:
        """从JSON文件加载评分标准"""
        import json
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        raw_weights = []
        items = []
        for item in data.get("items", []):
            weight = item["weight"]
            raw_weights.append(weight)
            items.append(RubricItem(
                category=item["category"],
                weight=weight,
                description=item.get("description", ""),
                level=item.get("level", "一般"),
            ))
        
        # 检测权重格式并标准化为百分制（0-100，总和为100）
        total_weight = sum(raw_weights)
        if 0.9 <= total_weight <= 1.1:
            # 小数格式 -> 转换为百分制
            for i, item in enumerate(items):
                item.weight = raw_weights[i] * 100
        
        rubric = Rubric(
            skill_name=data.get("skill_name", "Unknown"),
            description=data.get("description", ""),
            items=items,
            total_score=data.get("total_score", 100),
            grading_levels=data.get("grading_levels", {}),
        )
        self._rubric = rubric
        return rubric
    
    def _load_rubric_from_md(self, file_path: str) -> Rubric:
        """从Markdown文件加载评分标准（支持任意格式，由LLM解析）
        
        如果文件包含标准表格格式则直接解析，否则交给LLM理解并提取。
        """
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 先尝试表格格式解析
        items = self._parse_md_table(content)
        
        if items:
            # 检测并标准化权重格式
            raw_weights = [item.weight for item in items]
            total_weight = sum(raw_weights)
            if 0.9 <= total_weight <= 1.1:
                # 小数格式 -> 转换为百分制
                for i, item in enumerate(items):
                    item.weight = raw_weights[i] * 100
            
            # 表格解析成功，直接用
            title_match = re.search(r'^#\s+(.+?)$', content, re.MULTILINE)
            skill_name = title_match.group(1).strip().rstrip(' - 评分标准').strip() if title_match else "Unknown"
            
            desc_match = re.search(r'\*\*描述\*\*[:\s]*(.+?)$', content, re.MULTILINE)
            description = desc_match.group(1).strip() if desc_match else ""
            
            grading_levels = self._parse_grading_levels(content)
        elif self.model_client:
            # 表格解析失败，用 LLM 解析
            print("MD格式非标准表格，使用LLM解析...")
            return self._parse_rubric_with_llm(content)
        else:
            print("警告: MD格式非标准且未配置模型客户端，无法解析")
            return None
        
        rubric = Rubric(
            skill_name=skill_name,
            description=description,
            items=items,
            total_score=100,
            grading_levels=grading_levels,
        )
        self._rubric = rubric
        print(f"从MD文件加载评分标准: {skill_name}, 共 {len(items)} 个评分项")
        return rubric
    
    def _parse_md_table(self, content: str) -> List[RubricItem]:
        """尝试解析MD中的表格格式评分项"""
        items = []
        table_match = re.search(r'##\s*评分项\s*\n([\s\S]*?)(?=\n##|\Z)', content)
        if table_match:
            table_content = table_match.group(1)
            for line in table_content.strip().split('\n'):
                line = line.strip()
                # 跳过表头和分隔行
                if not line or line.startswith('|--') or line.startswith('| ---') or '类别' in line:
                    continue
                # 解析表格行：按 | 分割，取中间部分
                parts = [p.strip() for p in line.split('|')]
                # 去掉首尾的空字符串（因为 | 在行首行尾）
                parts = [p for p in parts if p]
                
                if len(parts) >= 4:
                    category, weight, level, desc = parts[0], parts[1], parts[2], '|'.join(parts[3:])  # description 可能包含 |
                    try:
                        items.append(RubricItem(
                            category=category.strip(),
                            weight=float(weight.strip()),
                            level=level.strip(),
                            description=desc.strip(),
                        ))
                    except ValueError:
                        continue
        return items
    
    def _parse_grading_levels(self, content: str) -> Dict[str, str]:
        """解析评分等级"""
        grading_levels = {}
        level_match = re.search(r'##\s*评分等级\s*\n([\s\S]*?)(?=\n##|\Z)', content)
        if level_match:
            for line in level_match.group(1).strip().split('\n'):
                line_match = re.match(r'-\s*(\d+-\d+)[:\s]+(.+)', line.strip())
                if line_match:
                    grading_levels[line_match.group(1)] = line_match.group(2).strip()
        return grading_levels
    
    def _parse_rubric_with_llm(self, content: str) -> Rubric:
        """用LLM解析任意格式的MD评分标准"""
        prompt = f"""请分析以下评分标准文档，提取出结构化的评分信息。

## 文档内容
{content}

## 要求
1. 提取评分标准名称
2. 提取描述（如果有）
3. 将每个评分项提取为：类别、权重（0-100的数字，所有权重之和为100）、重要程度（必要/重要/一般）、要求描述
4. 提取评分等级（如果有）

请按以下JSON格式输出：
```json
{{
    "skill_name": "评分标准名称",
    "description": "描述",
    "items": [
        {{"category": "类别", "weight": 权重值, "level": "必要/重要/一般", "description": "要求描述"}}
    ],
    "grading_levels": {{
        "90-100": "等级描述",
        "70-89": "等级描述"
    }}
}}
```

请直接输出JSON："""

        try:
            response = self.model_client.generate([
                {"role": "system", "content": "你是文档解析专家，擅长从非结构化文本中提取结构化信息。只输出JSON格式。"},
                {"role": "user", "content": prompt}
            ], temperature=0.1, max_tokens=4000)
            
            # 解析JSON
            json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                brace_match = re.search(r'\{.*\}', response, re.DOTALL)
                json_str = brace_match.group(0) if brace_match else response
            
            import json
            data = json.loads(json_str)
            
            items = [
                RubricItem(
                    category=item["category"],
                    weight=float(item["weight"]),
                    level=item.get("level", "一般"),
                    description=item.get("description", ""),
                )
                for item in data.get("items", [])
            ]
            
            rubric = Rubric(
                skill_name=data.get("skill_name", "Unknown"),
                description=data.get("description", ""),
                items=items,
                total_score=100,
                grading_levels=data.get("grading_levels", {}),
            )
            self._rubric = rubric
            print(f"LLM解析评分标准完成: {rubric.skill_name}, 共 {len(items)} 个评分项")
            return rubric
            
        except Exception as e:
            print(f"LLM解析评分标准失败: {e}")
            # 返回一个空的rubric作为fallback
            return Rubric(skill_name="Unknown", description="", items=[], total_score=100)
    
    @property
    def rubric(self) -> Optional[Rubric]:
        """获取评分标准"""
        if self._rubric is None and self._skill_content:
            self._rubric = self.generate_rubric()
        return self._rubric
    
    def generate_rubric(self) -> Optional[Rubric]:
        """根据skill.md生成评分标准"""
        if not self.model_client:
            print("警告: 未配置模型客户端，无法生成评分标准")
            return None
        
        if not self._skill_content:
            print("警告: 未设置skill内容，无法生成评分标准")
            return None
        
        try:
            return self._generate_rubric_with_llm()
        except Exception as e:
            print(f"生成评分标准失败: {e}")
            return None
    
    def _generate_rubric_with_llm(self) -> Rubric:
        """使用LLM生成评分标准"""
        rubric_prompt = f"""请分析以下Skill的内容，提炼出输出涉及的关键信息，并生成一套评分标准。

## Skill内容
{self._skill_content}

## 任务要求
1. 分析Skill输出应包含的关键信息
2. 将每个关键信息按重要程度分类：必要/重要/一般
3. 根据重要程度分配权重（必要>重要>一般）
4. 生成0-100分的评分标准

请按以下JSON格式输出：
```json
{{
    "skill_name": "Skill名称",
    "description": "Skill简要描述",
    "items": [
        {{
            "category": "信息类别",
            "weight": 权重值(总和为100),
            "description": "该信息的具体要求",
            "level": "必要/重要/一般"
        }}
    ],
    "grading_levels": {{
        "90-100": "优秀：完全满足所有必要信息，且大部分重要信息覆盖",
        "70-89": "良好：满足大部分必要信息，部分重要信息覆盖",
        "50-69": "及格：基本满足必要信息，但信息不够完整",
        "30-49": "较差：部分必要信息缺失",
        "0-29": "不合格：大量关键信息缺失"
    }}
}}
```

请直接输出JSON，不要添加其他内容："""

        response = self.model_client.generate([
            {"role": "system", "content": "你是Skill分析专家，擅长分析Skill输出内容并生成评分标准。只输出指定JSON格式。"},
            {"role": "user", "content": rubric_prompt}
        ], temperature=0.3, max_tokens=1500)
        
        # 解析JSON
        rubric = self._parse_rubric_response(response)
        print(f"已生成评分标准，共{len(rubric.items)}个评分项")
        return rubric
    
    def _parse_rubric_response(self, response: str) -> Rubric:
        """解析LLM返回的评分标准"""
        # 提取JSON
        json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            # 尝试直接查找大括号内容
            brace_match = re.search(r'\{.*\}', response, re.DOTALL)
            json_str = brace_match.group(0) if brace_match else response
        
        import json
        data = json.loads(json_str)
        
        items = []
        raw_weights = []
        for item_data in data.get("items", []):
            weight = item_data["weight"]
            raw_weights.append(weight)
            items.append(RubricItem(
                category=item_data["category"],
                weight=weight,
                description=item_data["description"],
                level=item_data.get("level", "一般")
            ))
        
        # 检测权重格式并标准化为百分制（0-100，总和为100）
        total_weight = sum(raw_weights)
        if 0.9 <= total_weight <= 1.1:
            # 小数格式 (0.3, 0.3, 0.4) -> 转换为百分制 (30, 30, 40)
            for i, item in enumerate(items):
                item.weight = raw_weights[i] * 100
        
        return Rubric(
            skill_name=data.get("skill_name", "Unknown"),
            description=data.get("description", ""),
            items=items,
            grading_levels=data.get("grading_levels", {})
        )
    
    def evaluate(self, output: str, expected: str, input_text: str = "", sub_type: str = "") -> RuleResult:
        """根据评分标准评估输出
        
        Args:
            output: 实际输出
            expected: 期望输出
            input_text: 输入文本（可选，用于提供更多上下文）
            sub_type: 数据子类型（可选，用于提供更多上下文）
        """
        if self.model_client is None:
            return RuleResult(
                rule_name=self.name,
                score=0.5,
                feedback="未配置模型客户端",
                diagnostics={"error": "no_model_client"}
            )
        
        if self.rubric is None:
            return RuleResult(
                rule_name=self.name,
                score=0.5,
                feedback="评分标准未生成",
                diagnostics={"error": "no_rubric"}
            )
        
        try:
            return self._evaluate_with_rubric(output, expected, input_text, sub_type)
        except Exception as e:
            return RuleResult(
                rule_name=self.name,
                score=0.5,
                feedback=f"评估失败: {e}",
                diagnostics={"error": str(e)}
            )
    
    def _evaluate_with_rubric(self, output: str, expected: str, input_text: str = "", sub_type: str = "") -> RuleResult:
        """使用评分标准评估"""
        rubric_text = self._rubric_to_text()

        context_info = ""
        if input_text:
            context_info += f"\n## 用户输入\n{input_text[:500]}"
        if sub_type:
            context_info += f"\n## 数据类型\n{sub_type}"

        eval_prompt = f"""请根据以下评分标准评估回答的质量。
{context_info}

## 评分标准
{rubric_text}"""

        if expected:
            eval_prompt += f"\n\n## 期望答案\n{expected}"
        else:
            eval_prompt += "\n\n## 说明\n无参考答案，请根据评分标准和用户输入评估实际回答"

        eval_prompt += f"""

## 实际回答
{output}

## 评估要求
请按照评分标准中的每个评估项，对实际回答进行评分。
总分为100分，每个评估项根据权重计分。

请按以下JSON格式输出：
```json
{{
    "item_scores": [
        {{
            "category": "评估项类别",
            "score": 实际得分(0-权重值),
            "reason": "评分理由"
        }}
    ],
    "summary": "总体评价"
}}
```

请直接输出JSON，不要添加其他内容："""

        response = self.model_client.generate([
            {"role": "system", "content": "你是Skill输出评估专家。根据评分标准对回答进行严格评分。只输出指定JSON格式。"},
            {"role": "user", "content": eval_prompt}
        ], temperature=0.3, max_tokens=1000)
        
        # 解析评估结果
        item_scores, total_score, summary = self._parse_eval_response(response)
        
        # 生成反馈
        feedback = summary
        if total_score < 60:
            feedback = f"得分较低({total_score}分)：{summary}"
        
        return RuleResult(
            rule_name=self.name,
            score=total_score / 100.0,
            feedback=feedback,
            diagnostics={
                "total_score": total_score,
                "item_scores": [item_score.__dict__ for item_score in item_scores],
                "summary": summary
            }
        )
    
    def _rubric_to_text(self) -> str:
        """将评分标准转换为文本"""
        if not self.rubric:
            return ""
        
        text = f"Skill: {self.rubric.skill_name}\n"
        text += f"描述: {self.rubric.description}\n\n"
        text += "评分项:\n"
        
        for i, item in enumerate(self.rubric.items, 1):
            text += f"{i}. {item.category} (权重: {item.weight}, 重要程度: {item.level})\n"
            text += f"   要求: {item.description}\n"
        
        text += "\n评分等级:\n"
        for level, desc in self.rubric.grading_levels.items():
            text += f"- {level}: {desc}\n"
        
        return text
    
    def _parse_eval_response(self, response: str) -> Tuple[List[ItemScore], int, str]:
        """解析评估结果"""
        import json
        
        # 提取JSON
        json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            brace_match = re.search(r'\{.*\}', response, re.DOTALL)
            json_str = brace_match.group(0) if brace_match else response
        
        data = json.loads(json_str)
        
        item_scores = []
        total_score = 0
        
        for item_data in data.get("item_scores", []):
            # 查找对应的权重
            weight = 0
            level = "一般"
            if self.rubric:
                for rubric_item in self.rubric.items:
                    if rubric_item.category == item_data["category"]:
                        weight = rubric_item.weight
                        level = rubric_item.level
                        break
            
            item_score = ItemScore(
                category=item_data["category"],
                score=item_data["score"],
                weight=weight,
                level=level,
                reason=item_data.get("reason", "")
            )
            item_scores.append(item_score)
            total_score += item_data["score"]
        
        summary = data.get("summary", "")
        
        return item_scores, total_score, summary
    
    def evaluate_detail(self, 
                       item_id: str,
                       sub_type: str,
                       input_text: str,
                       output: str, 
                       expected: str) -> EvaluationDetail:
        """详细评估，返回EvaluationDetail"""
        if self.model_client is None or self.rubric is None:
            return EvaluationDetail(
                item_id=item_id,
                sub_type=sub_type,
                input_text=input_text,
                expected=expected,
                actual=output,
                total_score=0.0,
                item_scores=[],
                summary="评估失败：未配置模型或评分标准"
            )
        
        result = self.evaluate(output, expected, input_text=input_text, sub_type=sub_type)
        
        item_scores_data = result.diagnostics.get("item_scores", [])
        item_scores = [
            ItemScore(
                category=score_data["category"],
                score=score_data["score"],
                level=score_data.get("level", "一般"),
                reason=score_data.get("reason", "")
            )
            for score_data in item_scores_data
        ]
        
        return EvaluationDetail(
            item_id=item_id,
            sub_type=sub_type,
            input_text=input_text,
            expected=expected,
            actual=output,
            total_score=result.diagnostics.get("total_score", 0),
            item_scores=item_scores,
            summary=result.diagnostics.get("summary", "")
        )


def generate_sub_type_summaries(details: List[EvaluationDetail]) -> List[SubTypeSummary]:
    """生成子类型汇总"""
    from collections import defaultdict
    
    type_scores = defaultdict(list)
    type_details = defaultdict(list)
    
    for detail in details:
        type_scores[detail.sub_type].append(detail.total_score)
        type_details[detail.sub_type].append(detail)
    
    summaries = []
    for sub_type, scores in type_scores.items():
        avg_score = sum(scores) / len(scores)
        
        # 生成理由
        reasons = []
        for detail in type_details[sub_type]:
            for item_score in detail.item_scores:
                if item_score.level == "必要" and item_score.score < item_score.score * 0.7:
                    reasons.append(f"类别'{item_score.category}'得分不足")
        
        if avg_score >= 80:
            reason = "表现优秀，关键信息覆盖完整"
        elif avg_score >= 60:
            reason = "表现良好，但部分重要信息可改进"
        elif avg_score >= 40:
            reason = "表现一般，需要加强关键信息覆盖"
        else:
            reason = "表现不佳，大量关键信息缺失"
        
        if reasons:
            reason += "；" + "、".join(set(reasons))
        
        summaries.append(SubTypeSummary(
            sub_type=sub_type,
            avg_score=avg_score,
            count=len(scores),
            reason=reason
        ))
    
    return summaries
