"""内置样例库：两把尺子各 25 条（5 维度 × 5 条）。

用例结构约定（全部含唯一 id）：
{
  "id": "r1-kn-001",        # 唯一；同时是 demo 确定性回答的哈希种子
  "dimension": "knowledge", # 必须属于所属尺子的 5 个维度 key 之一
  "type": "choice|open|math|code|format",
  "prompt": "...",          # 作为 user 消息发给被测模型
  "reference": "...",       # 参考答案（展示/判分依据）
  "answer_keys": ["..."],   # 判分关键词（小写规范化后做包含匹配）
  "max_points": 2,
  "max_len": None,          # 仅 type=format 使用：回答规范化长度上限
}
"""
from __future__ import annotations

import copy

from .specs import RULER1_SPEC, RULER2_SPEC

R1_CASES: dict[str, list[dict]] = {
    "knowledge": [
        {"id": "r1-kn-001", "dimension": "knowledge", "type": "choice",
         "prompt": "光在真空中的传播速度约为多少？请直接给出数值。",
         "reference": "约 3×10^8 m/s（299792458 m/s）", "answer_keys": ["299792458", "3×10^8"], "max_points": 2},
        {"id": "r1-kn-002", "dimension": "knowledge", "type": "choice",
         "prompt": "中国第一颗人造地球卫星的名称是什么？",
         "reference": "东方红一号", "answer_keys": ["东方红"], "max_points": 2},
        {"id": "r1-kn-003", "dimension": "knowledge", "type": "choice",
         "prompt": "水的化学分子式是什么？请直接给出分子式。",
         "reference": "H2O", "answer_keys": ["h2o"], "max_points": 2},
        {"id": "r1-kn-004", "dimension": "knowledge", "type": "math",
         "prompt": "圆周率 π 约等于多少（保留两位小数）？请直接给出数值。",
         "reference": "3.14", "answer_keys": ["3.14"], "max_points": 2},
        {"id": "r1-kn-005", "dimension": "knowledge", "type": "choice",
         "prompt": "正常成人腋下体温大约是多少摄氏度？",
         "reference": "约 36.5℃ ~ 37.2℃", "answer_keys": ["36", "37"], "max_points": 2},
    ],
    "reasoning": [
        {"id": "r1-rs-001", "dimension": "reasoning", "type": "open",
         "prompt": "所有哺乳动物都是恒温动物；鲸鱼是哺乳动物。据此可以推出什么结论？",
         "reference": "鲸鱼是恒温（温血）动物", "answer_keys": ["恒温", "温血"], "max_points": 2},
        {"id": "r1-rs-002", "dimension": "reasoning", "type": "open",
         "prompt": "甲比乙高，乙比丙高。三人中谁最高？请只回答人名。",
         "reference": "甲最高", "answer_keys": ["甲"], "max_points": 2},
        {"id": "r1-rs-003", "dimension": "reasoning", "type": "math",
         "prompt": "小明有 5 个苹果，吃掉 2 个后又买了 3 个，现在有几个苹果？请只回答数字。",
         "reference": "6 个", "answer_keys": ["6"], "max_points": 2},
        {"id": "r1-rs-004", "dimension": "reasoning", "type": "open",
         "prompt": "今天是星期三，3 天后是星期几？请直接回答星期几。",
         "reference": "星期六", "answer_keys": ["六", "星期六"], "max_points": 2},
        {"id": "r1-rs-005", "dimension": "reasoning", "type": "open",
         "prompt": "所有金属都能导电，水银是金属。水银可以导电吗？请用一句话回答。",
         "reference": "可以，水银能导电", "answer_keys": ["能", "导电"], "max_points": 2},
    ],
    "math": [
        {"id": "r1-mt-001", "dimension": "math", "type": "math",
         "prompt": "17 + 25 等于多少？请只回答数字。", "reference": "42", "answer_keys": ["42"], "max_points": 2},
        {"id": "r1-mt-002", "dimension": "math", "type": "math",
         "prompt": "120 的 25% 是多少？请只回答数字。", "reference": "30", "answer_keys": ["30"], "max_points": 2},
        {"id": "r1-mt-003", "dimension": "math", "type": "math",
         "prompt": "长为 8、宽为 5 的长方形，面积是多少？请只回答数字。", "reference": "40", "answer_keys": ["40"], "max_points": 2},
        {"id": "r1-mt-004", "dimension": "math", "type": "math",
         "prompt": "3 的 4 次方等于多少？请只回答数字。", "reference": "81", "answer_keys": ["81"], "max_points": 2},
        {"id": "r1-mt-005", "dimension": "math", "type": "math",
         "prompt": "100 除以 7，整数商是多少（不取余数）？请只回答数字。", "reference": "14", "answer_keys": ["14"], "max_points": 2},
    ],
    "code": [
        {"id": "r1-cd-001", "dimension": "code", "type": "code",
         "prompt": "用 Python 写一个函数 add(a, b)，返回两数之和。",
         "reference": "def add(a, b):\n    return a + b", "answer_keys": ["def", "return"], "max_points": 2},
        {"id": "r1-cd-002", "dimension": "code", "type": "code",
         "prompt": "用 Python 打印 1 到 10 之间的所有偶数。",
         "reference": "for i in range(1, 11):\n    if i % 2 == 0:\n        print(i)", "answer_keys": ["for", "print", "range"], "max_points": 2},
        {"id": "r1-cd-003", "dimension": "code", "type": "code",
         "prompt": "用 Python 写一个函数反转字符串并返回结果。",
         "reference": "def reverse(s):\n    return s[::-1]", "answer_keys": ["def", "return"], "max_points": 2},
        {"id": "r1-cd-004", "dimension": "code", "type": "code",
         "prompt": "用 Python 写一个函数 is_palindrome(s)，判断字符串是否为回文。",
         "reference": "def is_palindrome(s):\n    return s == s[::-1]", "answer_keys": ["def", "return"], "max_points": 2},
        {"id": "r1-cd-005", "dimension": "code", "type": "code",
         "prompt": "用 Python 统计一个字符串中每个字符出现的次数，返回字典。",
         "reference": "def count_chars(s):\n    d = {}\n    for c in s:\n        d[c] = d.get(c, 0) + 1\n    return d", "answer_keys": ["def", "for", "int"], "max_points": 2},
    ],
    "conformance": [
        {"id": "r1-cf-001", "dimension": "conformance", "type": "format",
         "prompt": "请只回复两个字：收到", "reference": "收到", "answer_keys": ["收到"], "max_points": 2},
        {"id": "r1-cf-002", "dimension": "conformance", "type": "choice",
         "prompt": "请输出一个 JSON 对象，内容为 {\"ok\": true}。", "reference": "{\"ok\": true}", "answer_keys": ["ok"], "max_points": 2},
        {"id": "r1-cf-003", "dimension": "conformance", "type": "format",
         "prompt": "请用不超过 20 个字介绍你自己。", "reference": "（非空且不超过 20 字）", "answer_keys": [], "max_points": 2, "max_len": 20},
        {"id": "r1-cf-004", "dimension": "conformance", "type": "choice",
         "prompt": "请输出三行，每行依次是数字 1、2、3。", "reference": "1\n2\n3", "answer_keys": ["1", "2", "3"], "max_points": 2},
        {"id": "r1-cf-005", "dimension": "conformance", "type": "choice",
         "prompt": "请只回答两个字：是 或 否。", "reference": "是", "answer_keys": ["是"], "max_points": 2},
    ],
}

R2_CASES: dict[str, list[dict]] = {
    "intent_cls": [
        {"id": "r2-ic-001", "dimension": "intent_cls", "type": "open",
         "prompt": "用户说：‘帮我查一下上个月的流量账单’。请判断用户意图是什么。",
         "reference": "查询账单（费用/流量查询）", "answer_keys": ["查询", "账单"], "max_points": 2},
        {"id": "r2-ic-002", "dimension": "intent_cls", "type": "open",
         "prompt": "用户说：‘我要投诉，你们信号太不稳定了’。请判断用户意图。",
         "reference": "投诉（网络质量投诉）", "answer_keys": ["投诉"], "max_points": 2},
        {"id": "r2-ic-003", "dimension": "intent_cls", "type": "open",
         "prompt": "用户说：‘麻烦帮我办理停机保号’。请判断用户意图。",
         "reference": "业务办理（停机保号）", "answer_keys": ["停机", "办理"], "max_points": 2},
        {"id": "r2-ic-004", "dimension": "intent_cls", "type": "open",
         "prompt": "用户说：‘请转人工服务’。请判断用户意图。",
         "reference": "转人工（人工客服）", "answer_keys": ["人工"], "max_points": 2},
        {"id": "r2-ic-005", "dimension": "intent_cls", "type": "open",
         "prompt": "用户说：‘我这个月的话费是多少？’。请判断用户意图。",
         "reference": "话费查询（费用查询）", "answer_keys": ["话费", "查询"], "max_points": 2},
    ],
    "tool_call": [
        {"id": "r2-tc-001", "dimension": "tool_call", "type": "open",
         "prompt": "用户要求调用 $10086 查询账户余额。请输出要调用的工具名和动作。",
         "reference": "调用 $10086：查询余额", "answer_keys": ["10086", "余额"], "max_points": 2},
        {"id": "r2-tc-002", "dimension": "tool_call", "type": "open",
         "prompt": "用户说：‘帮我设置语音信箱’。请输出需要调用的工具。",
         "reference": "调用工具：设置语音信箱", "answer_keys": ["语音信箱"], "max_points": 2},
        {"id": "r2-tc-003", "dimension": "tool_call", "type": "open",
         "prompt": "用户说：‘我要开通国际漫游’。请输出需要执行的工单动作。",
         "reference": "工单：开通国际漫游", "answer_keys": ["漫游", "开通"], "max_points": 2},
        {"id": "r2-tc-004", "dimension": "tool_call", "type": "open",
         "prompt": "用户说：‘帮我找一下附近的营业厅’。请输出要调用的工具。",
         "reference": "调用工具：查询附近营业厅", "answer_keys": ["营业厅", "附近"], "max_points": 2},
        {"id": "r2-tc-005", "dimension": "tool_call", "type": "open",
         "prompt": "用户说：‘请给我订购一个 5G 流量包’。请输出订购动作。",
         "reference": "订购 5G 流量包", "answer_keys": ["5g", "流量", "订购"], "max_points": 2},
    ],
    "memory": [
        {"id": "r2-mm-001", "dimension": "memory", "type": "open",
         "prompt": "多轮对话：用户先前说‘我是王工’，现在问‘我姓什么？’请回答。",
         "reference": "王（王工）", "answer_keys": ["王工", "王"], "max_points": 2},
        {"id": "r2-mm-002", "dimension": "memory", "type": "open",
         "prompt": "用户告知手机尾号是 1234，随后问‘我的手机尾号是多少？’请回答。",
         "reference": "1234", "answer_keys": ["1234"], "max_points": 2},
        {"id": "r2-mm-003", "dimension": "memory", "type": "open",
         "prompt": "用户说每月套餐费是 99 元，随后问‘我每月套餐费是多少？’请回答。",
         "reference": "99 元", "answer_keys": ["99"], "max_points": 2},
        {"id": "r2-mm-004", "dimension": "memory", "type": "open",
         "prompt": "用户预约‘明天下午 3 点’上门安装，随后问‘我预约的是几点？’请回答。",
         "reference": "明天下午 3 点", "answer_keys": ["3点", "3 点", "明天"], "max_points": 2},
        {"id": "r2-mm-005", "dimension": "memory", "type": "open",
         "prompt": "用户说办理地点是‘广州天河营业厅’，随后问‘在哪里办理？’请回答。",
         "reference": "广州天河营业厅", "answer_keys": ["天河"], "max_points": 2},
    ],
    "clarify": [
        {"id": "r2-cl-001", "dimension": "clarify", "type": "open",
         "prompt": "用户说‘我要办理业务’但没有说明是哪类业务。合适的下一步是？",
         "reference": "追问用户想办理什么业务", "answer_keys": ["哪种", "什么业务", "办理什么"], "max_points": 2},
        {"id": "r2-cl-002", "dimension": "clarify", "type": "open",
         "prompt": "用户说‘帮我改个套餐’，但没有说明目标套餐。合适的下一步是？",
         "reference": "询问用户想要什么套餐或哪个套餐", "answer_keys": ["什么套餐", "哪个套餐", "目标套餐"], "max_points": 2},
        {"id": "r2-cl-003", "dimension": "clarify", "type": "open",
         "prompt": "用户说‘我想找个地方’，但没说找什么地方。合适的下一步是？",
         "reference": "询问用户要找什么地方，或具体在哪里", "answer_keys": ["哪里", "什么地方"], "max_points": 2},
        {"id": "r2-cl-004", "dimension": "clarify", "type": "open",
         "prompt": "用户说‘把我的宽带延迟一下’，‘延迟’含义有歧义。合适的下一步是？",
         "reference": "询问清楚是暂停还是续费，避免歧义", "answer_keys": ["暂停", "续费", "什么"], "max_points": 2},
        {"id": "r2-cl-005", "dimension": "clarify", "type": "open",
         "prompt": "用户说‘我要退费’，但没说退什么费用。合适的下一步是？",
         "reference": "询问用户要退什么费用", "answer_keys": ["什么", "退"], "max_points": 2},
    ],
    "planning": [
        {"id": "r2-pl-001", "dimension": "planning", "type": "open",
         "prompt": "用户下周一出差，需要：订高铁票、订酒店、提交差旅申请。请输出完整步骤序列。",
         "reference": "1 订高铁 2 订酒店 3 提交差旅申请", "answer_keys": ["高铁", "酒店", "申请"], "max_points": 2},
        {"id": "r2-pl-002", "dimension": "planning", "type": "open",
         "prompt": "为一场客户答谢会输出筹备步骤。",
         "reference": "明确时间地点 → 预算审批 → 嘉宾邀请 → 现场执行", "answer_keys": ["时间", "地点", "预算", "邀请"], "max_points": 2},
        {"id": "r2-pl-003", "dimension": "planning", "type": "open",
         "prompt": "为企业客户规划 5G 专网部署的实施步骤。",
         "reference": "需求分析 → 方案设计 → 实施部署 → 测试验收", "answer_keys": ["需求", "方案", "实施", "验收"], "max_points": 2},
        {"id": "r2-pl-004", "dimension": "planning", "type": "open",
         "prompt": "用户出差回来需要报销，请列出报销步骤。",
         "reference": "整理单据 → 填写申请 → 提交发票 → 审批打款", "answer_keys": ["发票", "审批", "单据"], "max_points": 2},
        {"id": "r2-pl-005", "dimension": "planning", "type": "open",
         "prompt": "办公区 WiFi 升级改造，请列出实施步骤。",
         "reference": "现场勘查 → 设备选型 → 布线施工 → 测试优化", "answer_keys": ["勘查", "选型", "布线", "测试"], "max_points": 2},
    ],
}

_CASES_BY_RULER = {"Ruler1": R1_CASES, "Ruler2": R2_CASES}


def get_cases_for_ruler(ruler_id: str) -> dict[str, list[dict]]:
    """返回 {维度 key: [用例...]} 的深拷贝，避免调用方就地修改内置样例。"""
    if ruler_id == RULER1_SPEC.id:
        return copy.deepcopy(R1_CASES)
    if ruler_id == RULER2_SPEC.id:
        return copy.deepcopy(R2_CASES)
    raise ValueError(f"不支持的测评尺子: {ruler_id}")


def all_cases() -> dict[str, dict[str, list[dict]]]:
    """两把尺子的原始样例（只读用途，如按 prompt 反查参考答案）。"""
    return _CASES_BY_RULER
