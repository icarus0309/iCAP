"""
评估报告生成器
"""
import os
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from .rubric import (
    LLMRubricBasedRule,
    EvaluationDetail,
    SubTypeSummary,
    Rubric,
    ItemScore,
    generate_sub_type_summaries
)


class ReportGenerator:
    """报告生成器"""
    
    def __init__(self, output_dir: str = "output"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def create_run_dir(self, prefix: str = "eval") -> Path:
        """创建本次执行的输出目录"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = self.output_dir / f"{prefix}_{timestamp}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir
    
    def save_rubric(self, rubric: Rubric, run_dir: Path):
        """保存评分标准"""
        if not rubric:
            return
        
        rubric_file = run_dir / "rubric.md"
        with open(rubric_file, 'w', encoding='utf-8') as f:
            f.write(f"# {rubric.skill_name} - 评分标准\n\n")
            f.write(f"**描述**: {rubric.description}\n\n")
            
            f.write("## 评分项\n\n")
            f.write("| 序号 | 类别 | 权重 | 重要程度 | 要求 |\n")
            f.write("|------|------|------|----------|------|\n")
            for i, item in enumerate(rubric.items, 1):
                f.write(f"| {i} | {item.category} | {item.weight} | {item.level} | {item.description} |\n")
            f.write("\n")
            
            f.write("## 评分等级\n\n")
            for level, desc in rubric.grading_levels.items():
                f.write(f"- **{level}**: {desc}\n")
        
        print(f"评分标准已保存到: {rubric_file}")
    
    def save_score_table(self, details: List[EvaluationDetail], run_dir: Path):
        """保存评分明细表格"""
        if not details:
            return
        
        # Markdown表格
        table_file = run_dir / "score_table.md"
        with open(table_file, 'w', encoding='utf-8') as f:
            f.write("# 评分明细表\n\n")
            f.write("| ID | Sub-Type | 得分 | 输入 | 期望输出 | 实际输出 | 评价 |\n")
            f.write("|----|----------|------|------|----------|----------|------|\n")
            
            for detail in details:
                # 截断长文本
                input_text = detail.input_text[:30] + '...' if len(detail.input_text) > 30 else detail.input_text
                expected_text = detail.expected[:30] + '...' if len(detail.expected) > 30 else detail.expected
                actual_text = detail.actual[:30] + '...' if len(detail.actual) > 30 else detail.actual
                summary_text = detail.summary[:20] + '...' if len(detail.summary) > 20 else detail.summary
                
                # 转义表格特殊字符
                input_text = input_text.replace('|', '\\|').replace('\n', ' ')
                expected_text = expected_text.replace('|', '\\|').replace('\n', ' ')
                actual_text = actual_text.replace('|', '\\|').replace('\n', ' ')
                summary_text = summary_text.replace('|', '\\|').replace('\n', ' ')
                
                f.write(f"| {detail.item_id} | {detail.sub_type} | {detail.total_score:.1f} | {input_text} | {expected_text} | {actual_text} | {summary_text} |\n")
            
            f.write(f"\n*总用例数: {len(details)}*\n")
        
        print(f"评分明细表已保存到: {table_file}")
        
        # JSON格式
        json_file = run_dir / "score_details.json"
        data = []
        for detail in details:
            data.append({
                "item_id": detail.item_id,
                "sub_type": detail.sub_type,
                "input": detail.input_text,
                "expected": detail.expected,
                "actual": detail.actual,
                "total_score": detail.total_score,
                "item_scores": [
                    {"category": item.category, "score": item.score, "level": item.level, "reason": item.reason}
                    for item in detail.item_scores
                ],
                "summary": detail.summary
            })
        
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def save_sub_type_summary(self, details: List[EvaluationDetail], run_dir: Path):
        """保存子类型汇总"""
        if not details:
            return
        
        summaries = generate_sub_type_summaries(details)
        
        # Markdown格式
        summary_file = run_dir / "sub_type_summary.md"
        with open(summary_file, 'w', encoding='utf-8') as f:
            f.write("# 子类型评估汇总\n\n")
            
            for summary in sorted(summaries, key=lambda x: x.avg_score, reverse=True):
                f.write(f"**类别**: {summary.sub_type}  ")
                f.write(f"**评分**: {summary.avg_score:.1f}  ")
                f.write(f"**用例数**: {summary.count}  ")
                f.write(f"**理由**: {summary.reason}\n\n")
            
            # 总体统计
            all_scores = [d.total_score for d in details]
            overall_avg = sum(all_scores) / len(all_scores) if all_scores else 0
            f.write(f"---\n\n")
            f.write(f"**总体评分**: {overall_avg:.1f}  ")
            f.write(f"**总用例数**: {len(details)}\n")
        
        print(f"子类型汇总已保存到: {summary_file}")
        
        # 控制台输出
        print("\n" + "=" * 50)
        print("子类型评估汇总")
        print("=" * 50)
        for summary in sorted(summaries, key=lambda x: x.avg_score, reverse=True):
            print(f"类别: {summary.sub_type:<15} 评分: {summary.avg_score:>6.1f} 理由: {summary.reason}")
        print("=" * 50)
        
        return summaries
    
    def generate_full_report(self,
                            details: List[EvaluationDetail],
                            rubric: Optional[Rubric] = None,
                            prefix: str = "eval"):
        """生成完整报告"""
        run_dir = self.create_run_dir(prefix)
        
        if rubric:
            self.save_rubric(rubric, run_dir)
        
        self.save_score_table(details, run_dir)
        summaries = self.save_sub_type_summary(details, run_dir)
        
        return run_dir, summaries
