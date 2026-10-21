"""
数据集加载器 - 支持skill_dataset格式
"""
import json
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from pathlib import Path

from src.config import load_output_weights


@dataclass
class KeyStep:
    """关键步骤"""
    step_id: int
    description: str
    key_info: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    dependence: List[int] = field(default_factory=list)
    weight: float = 1.0


@dataclass
class ContentItem:
    """内容项（支持text、image等类型）"""
    type: str  # "text", "image", etc.
    # text 为字符串；word 为 {"blacklist": [...], "whitelist": [...]}；
    # list 为消息数组 [{"role": ..., "content": ...}, ...]
    data: Any
    # file 类型专用：期望/实际文件路径
    expect_path: str = ""
    actual_path: str = ""

    def get_text(self) -> str:
        """获取文本内容（目前只支持text类型）"""
        if self.type == "text":
            return self.data
        return f"[{self.type}]: {self.data}"


@dataclass
class DatasetItem:
    """数据集单项"""
    id: int
    type: str  # 数据集类型
    sub_type: str  # 数据子类型
    input_items: List[ContentItem]
    key_steps: List[KeyStep]
    output_items: List[ContentItem]
    pre_condition: Optional[List[Dict[str, Any]]] = None  # 前置条件（实体状态列表）
    output_weights: Dict[str, float] = field(default_factory=load_output_weights)
    
    @property
    def input_text(self) -> str:
        """获取输入文本"""
        texts = [item.get_text() for item in self.input_items if item.type == "text"]
        return "\n".join(texts)

    @property
    def input_messages(self) -> Optional[List[Dict[str, Any]]]:
        """获取消息数组输入（direct 模式的 user prompt）。

        - type=="json"：data 为 JSON 字符串，解析出 list；
        - type=="list"：data 本身即为消息数组；
        - 无上述输入或解析失败返回 None。
        """
        for item in self.input_items:
            if item.type == "json":
                try:
                    parsed = json.loads(item.data)
                except (json.JSONDecodeError, TypeError):
                    return None
                if isinstance(parsed, list):
                    return parsed
            elif item.type == "list":
                if isinstance(item.data, list):
                    return item.data
        return None

    @property
    def expected_output(self) -> str:
        """获取期望输出文本（自然语言部分，type=text）"""
        texts = [item.get_text() for item in self.output_items if item.type == "text"]
        return "\n".join(texts)

    @property
    def expected_device_state(self) -> str:
        """获取期望设备状态（工具返回结果对比用，type=device_state）"""
        for item in self.output_items:
            if item.type == "device_state":
                return item.data
        return ""

    @property
    def expected_outputs(self) -> Dict[str, Any]:
        """获取全部类型的期望输出，返回 {type: 期望值}。

        - text: 所有 type=text 条目 data 合并（换行连接）
        - tool: type=tool 条目 data（兼容旧 type=device_state）
        - file: {"expect_path": ..., "actual_path": ...}
        - onlyllm: type=onlyllm 条目 data（rubric prompt 文件路径）
        - word: type=word 条目 data（{"blacklist": [...], "whitelist": [...]} 格式硬校验）
        - format: type=format 条目 data（JSON 数组格式示例；为空时回退 .env 的 OUTPUT_FORMAT_EXAMPLE）
        """
        result: Dict[str, Any] = {}
        text_parts: List[str] = []
        for item in self.output_items:
            if item.type == "text":
                text_parts.append(item.data)
            elif item.type in ("tool", "device_state"):
                # device_state -> tool 兼容映射
                result["tool"] = item.data
            elif item.type == "file":
                result["file"] = {
                    "expect_path": item.expect_path,
                    "actual_path": item.actual_path,
                }
            elif item.type == "onlyllm":
                result["onlyllm"] = item.data
            elif item.type == "word":
                result["word"] = item.data
            elif item.type == "format":
                result["format"] = item.data
        if text_parts:
            result["text"] = "\n".join(text_parts)
        return result


@dataclass
class SkillDataset:
    """技能数据集"""
    items: List[DatasetItem]
    dataset_type: str = ""
    
    @property
    def sub_types(self) -> List[str]:
        """获取所有子类型"""
        return list(set(item.sub_type for item in self.items))
    
    def get_items_by_sub_type(self, sub_type: str) -> List[DatasetItem]:
        """按子类型获取数据项"""
        return [item for item in self.items if item.sub_type == sub_type]
    
    def to_gepa_data(self) -> List[Any]:
        """转换为GEPA优化器使用的DataInstance格式"""
        from src.self_evolve.schemas import DataInstance

        data = []
        for item in self.items:
            data.append(DataInstance(
                id=str(item.id),
                input=item.input_text,
                expected_output=item.expected_output,
                metadata={
                    "type": item.type,
                    "sub_type": item.sub_type,
                    "key_steps": [
                        {
                            "step_id": s.step_id,
                            "description": s.description,
                            "weight": s.weight
                        }
                        for s in item.key_steps
                    ],
                    "expected_outputs": item.expected_outputs,
                    "input_messages": item.input_messages,
                },
                pre_condition=item.pre_condition
            ))
        return data


def load_dataset(file_path: str) -> SkillDataset:
    """加载数据集"""
    path = Path(file_path)
    
    if not path.exists():
        raise FileNotFoundError(f"数据集文件不存在: {file_path}")
    
    with open(path, 'r', encoding='utf-8') as f:
        raw_data = json.load(f)
    
    if not raw_data:
        return SkillDataset(items=[])
    
    items = []
    dataset_type = ""
    
    for item_data in raw_data:
        # 解析输入
        input_items = []
        for inp in item_data.get("input", []):
            if isinstance(inp, dict):
                input_items.append(ContentItem(
                    type=inp.get("type", "text"),
                    data=inp.get("data", "")
                ))
            elif isinstance(inp, str):
                # 如果是字符串，直接作为 data
                input_items.append(ContentItem(
                    type="text",
                    data=inp
                ))
        
        # 解析关键步骤
        key_steps = []
        for step in item_data.get("key_steps", []):
            key_steps.append(KeyStep(
                step_id=step.get("step_id", 0),
                description=step.get("description", ""),
                key_info=step.get("key_info", []),
                tools=step.get("tools", []),
                dependence=step.get("dependence", []),
                weight=step.get("weight", 1.0)
            ))
        
        # 解析输出
        output_items = []
        for out in item_data.get("output", []):
            if isinstance(out, dict):
                output_items.append(ContentItem(
                    type=out.get("type", "text"),
                    data=out.get("data", ""),
                    expect_path=out.get("expect_path", ""),
                    actual_path=out.get("actual_path", ""),
                ))
            elif isinstance(out, str):
                # 如果是字符串，直接作为 data
                output_items.append(ContentItem(
                    type="text",
                    data=out
                ))
        
        # 获取数据集类型（使用第一个非空的type）
        item_type = item_data.get("type", "")
        if item_type and not dataset_type:
            dataset_type = item_type
        
        # 解析前置条件
        pre_condition = item_data.get("pre_condition", None)

        # 解析输出权重（key 统一为 text/tool/file；兼容旧 key）
        output_weights_raw = item_data.get("output_weights", {})
        if output_weights_raw:
            output_weights = {}
            for k, v in output_weights_raw.items():
                key = k
                if key in ("text_output", "text"):
                    key = "text"
                elif key == "device_state":
                    key = "tool"
                output_weights[key] = float(v)
        else:
            output_weights = load_output_weights()

        items.append(DatasetItem(
            id=item_data.get("id", 0),
            type=item_type,
            sub_type=item_data.get("sub_type", "default"),
            input_items=input_items,
            key_steps=key_steps,
            output_items=output_items,
            pre_condition=pre_condition,
            output_weights=output_weights,
        ))
    
    return SkillDataset(items=items, dataset_type=dataset_type)


def save_dataset(dataset: SkillDataset, file_path: str):
    """保存数据集"""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    raw_data = []
    for item in dataset.items:
        raw_data.append({
            "id": item.id,
            "type": item.type,
            "sub_type": item.sub_type,
            "input": [
                {"type": i.type, "data": i.data}
                for i in item.input_items
            ],
            "key_steps": [
                {
                    "step_id": s.step_id,
                    "description": s.description,
                    "key_info": s.key_info,
                    "tools": s.tools,
                    "dependence": s.dependence,
                    "weight": s.weight
                }
                for s in item.key_steps
            ],
            "output": [
                {
                    "type": o.type,
                    "data": o.data,
                    **({"expect_path": o.expect_path, "actual_path": o.actual_path}
                       if o.type == "file" else {}),
                }
                for o in item.output_items
            ],
            "pre_condition": item.pre_condition,
            "output_weights": item.output_weights,
        })
    
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(raw_data, f, ensure_ascii=False, indent=2)