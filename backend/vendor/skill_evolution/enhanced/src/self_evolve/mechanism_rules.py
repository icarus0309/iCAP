"""Shared abstraction boundary for case analysis and experience aggregation.

Copied from ``skill_distill_multi_dim/src/trace2skill/analysis_prompt_rules.py``
so both the failure-summarization and knowledge-extraction prompts enforce the
same granularity rules when emitting ``root_cause`` / ``suggestion``.
"""

CASE_MECHANISM_RULES = """## 抽象粒度：保留失败机制，去除个案取值

- 提炼同类场景可复用的机制，不要求跨领域成立。保留能区分本次问题的对象类别、属性语义、触发条件和依赖关系；删除实例名、位置名、接口/API、命令、参数字段名、具体取值、枚举值、公式和逐例操作清单。用“运行模式”“目标温度”“位置反馈”“百分比与绝对量”等自然语言语义表达关系，不列参数表。
- `suggestion` 本身应交代“何种场景/条件下，哪项判断或依赖被遗漏/误解，造成什么偏差或应如何处理”。不能仅靠 root_cause 或 evidence_summary 保留关键机制，而把 suggestion 写成“理解意图、补齐参数、验证结果、避免猜测”等通用于多数失败的建议。
- `root_cause`、`suggestion`、`improvement_direction` 必须对应同一条有证据的因果链；改进应直接针对错误判断或遗漏的依赖，不能用下游通用检查替代已观察到的上游根因。只能观察到状态不一致时，说明不一致，不推断未经验证的设备或接口内部原因。
- 粒度示例仅用于说明表达方式，不是待输出的规则：温控场景中“只调目标温度却沿用与供暖目标不符的旧运行模式”，比“补齐设备参数”更能定位问题；窗帘场景中“关闭状态与位置反馈不一致，不能仅凭关闭回执断言到位”，比“验证关键属性”更明确；亮度场景中“混用百分比和绝对量时误判数值尺度并擅自换算”，比“所有数值都不得转换”更准确。
- 上述示例仅在当前证据确实支持时采用，不得照搬或为其它场景补造相同根因。保留必要的尺度转换、模式保持等合理例外；不能把某个错误实例扩大为无条件禁止，也不能从固定验收答案倒推出用户未表达的唯一运行模式。
- 输出前自检：删去实例名与取值后，读者是否仍能辨认原问题的机制，并区别于同场景另一种失败？如果只剩泛泛建议，回到证据补回条件和语义关系；证据不足则明确保留不确定性，不补写细节。审计 ID 只放在 schema 指定的来源字段，分析正文不出现 Case ID。
"""

MERGE_MECHANISM_RULES = CASE_MECHANISM_RULES + """
## 合并时保持机制分辨率

- 按触发条件和因果机制合并，不按“参数问题”“状态校验”“意图理解”等宽泛主题合并。只有机制等价或互补且条件兼容时才综合；不同机制保留独立 suggestion 或清晰条件分支。
- 聚合、再次合并及最终编辑不得逐层抽象成更宽泛的口号。保留来源中有证据的属性语义、错误关系和适用边界；去掉实例与取值不等于删除这些信息。编辑条数受限时可在同一章节容纳不同机制，不得把它们压成一句通用建议。
- 若旧输入的 suggestion 过宽，可依据同一来源的 root_cause、evidence_summary 和 applicability_boundary 收窄；不得虚构缺失机制，也不得用提高抽象程度来凑支持度。单案例的置信度和证据门槛保持不变，不因措辞更通用而获得更多支持。
"""
