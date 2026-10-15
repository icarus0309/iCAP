"""尺子测评规格：维度顺序、权重与展示元数据。权重之和必须为 1。"""
from __future__ import annotations


class RulerSpec:
    """一把测评尺子的规格：固定 5 个维度，权重之和为 1。"""

    def __init__(self, id: str, label: str, scope: str, description: str,
                 dimensions: list[dict], default_sample_limit: int):
        total = sum(d["weight"] for d in dimensions)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"尺子 {id} 维度权重之和必须为 1，当前 {total}")
        if len(dimensions) != 5:
            raise ValueError(f"尺子 {id} 必须定义 5 个维度")
        self.id = id
        self.label = label
        self.scope = scope
        self.description = description
        self.dimensions = dimensions
        self.default_sample_limit = default_sample_limit

    def weights(self) -> list[float]:
        return [d["weight"] for d in self.dimensions]

    def weight(self, key: str) -> float:
        for d in self.dimensions:
            if d["key"] == key:
                return d["weight"]
        raise KeyError(key)

    def dimension_labels(self) -> dict[str, str]:
        return {d["key"]: d["label"] for d in self.dimensions}


RULER1_SPEC = RulerSpec(
    id="Ruler1", label="尺子一测评", scope="通用能力",
    description="面向大模型的通用能力测评：知识、推理、数学、代码与指令遵循。",
    dimensions=[
        {"key": "knowledge",   "label": "知识",     "weight": 0.30, "benchmark_focus": "MMLU/C-Eval"},
        {"key": "reasoning",   "label": "推理",     "weight": 0.30, "benchmark_focus": "AIME/逻辑"},
        {"key": "math",        "label": "数学",     "weight": 0.25, "benchmark_focus": "GSM8K/数学"},
        {"key": "code",        "label": "代码",     "weight": 0.05, "benchmark_focus": "HumanEval/代码"},
        {"key": "conformance", "label": "指令遵循", "weight": 0.10, "benchmark_focus": "指令遵循"},
    ],
    default_sample_limit=5,
)

RULER2_SPEC = RulerSpec(
    id="Ruler2", label="尺子二测评", scope="通信领域能力",
    description="面向通信领域智能体的领域能力测评：意图、工具、记忆、澄清与规划。",
    dimensions=[
        {"key": "intent_cls", "label": "意图分类", "weight": 0.20, "benchmark_focus": "AISF 意图"},
        {"key": "tool_call",  "label": "功能调用", "weight": 0.15, "benchmark_focus": "AISF 工具"},
        {"key": "memory",     "label": "记忆保持", "weight": 0.15, "benchmark_focus": "多轮记忆"},
        {"key": "clarify",    "label": "澄清对话", "weight": 0.10, "benchmark_focus": "澄清/澄清提问"},
        {"key": "planning",   "label": "感知规划", "weight": 0.40, "benchmark_focus": "任务规划"},
    ],
    default_sample_limit=5,
)

#: 顺序即接口与页面的展示顺序
RULER_SPECS = (RULER1_SPEC, RULER2_SPEC)

#: 已接入真实引擎的尺子 ID 集合；其余 benchmark 仍走原模拟执行器
RULER_IDS = frozenset(spec.id for spec in RULER_SPECS)


def get_ruler_spec(ruler_id: str) -> RulerSpec:
    if ruler_id == "Ruler1":
        return RULER1_SPEC
    if ruler_id == "Ruler2":
        return RULER2_SPEC
    raise ValueError(f"不支持的测评尺子: {ruler_id}")
