# AgentCompass 前端集成设计文档（UI + 前端接口）

> 分支：yyl-dev（沿用后端集成设计同名分支约定）
> 状态：设计评审稿，未实施
> 衔接文档：`docs/AGENTCOMPASS_INTEGRATION_DESIGN.md`（后端进程内 vendor 集成，已评审）
> 本文档范围：iCAP 前端如何暴露 AgentCompass 评测能力（发起 → 运行 → 展示结果），
>             UI 与 iCAP 现有 Vue3 + Element Plus 风格保持一致。

---

## 1. 定位与目标

后端已把 agent-eval-agentCompass 以「进程内 vendor」方式接进 iCAP，`AgentCompassRunner`
能真实执行评测并回写报告。本文档补齐**前端阶段**：

- 前端「发起评测」支持选择 benchmark / harness / environment / 模型 API 协议，提交真实 Agent 评测。
- 评测列表、详情页正确展示 Agent 任务的进度与结果（不再当作「模拟执行」）。
- 结果面板适配 Agent 结果结构（单维解决率 + 统计指标），不复用尺子多维雷达/用例明细。

闭环：前端提交 → 后端 `AgentCompassRunner` 运行 → SSE 进度推送 → 完成后结果面板展示。

## 2. 范围（本轮已确认的决策）

### 2.1 暴露的 harness（3 个，纯 Python + LLM 直连，本地可跑）

| id | 说明 | 备注 |
|----|------|------|
| openai_chat | 直接调模型，无环境 | 问答类 benchmark（gaia/hle/skillsbench） |
| mini_swe_agent | mini-SWE-agent 非交互编码 agent | iCAP 现有 Dev 已在用，已验证 |
| naive_search_agent | function-calling 搜索 agent | 浏览/搜索类（browsecomp/gaia） |

### 2.2 暴露的 environment（2 个）

| id | 说明 |
|----|------|
| docker | 容器隔离执行 |
| host_process | 宿主进程执行（依赖 aioshutil，接入时需确认已装） |

### 2.3 暴露的 benchmark（~8 个，本地有 data 且配好做的 harness）

| id | 建议 harness |
|----|-------------|
| swebench_verified | mini_swe_agent |
| swebench_lite | mini_swe_agent |
| swebench_pro | mini_swe_agent |
| gaia | naive_search_agent / openai_chat |
| browsecomp | naive_search_agent |
| browsecomp_zh | naive_search_agent |
| hle | openai_chat |
| skillsbench | openai_chat / mini_swe_agent |

### 2.4 排除清单（本轮确认不暴露）

- harness：claude_code（用户要求）、codex、pi、hermes、openclaw、openhands、openevolve、
  terminus2、aisf_agent、qwen3vl_gui、scicode_tool_use、researchharness。
- benchmark：aisf_iot、terminalbench2_1（依赖难做的 harness：aisf_agent / terminus2）。
- 所有 AISF 相关（benchmark aisf_iot + harness aisf_agent）不暴露。

### 2.5 模型 API 协议暴露给用户

`ApiProtocol` 是封闭枚举（runtime/llm/protocol.py），前端做成下拉：

| value | 含义 |
|-------|------|
| auto（默认） | 用 harness 默认协议 |
| openai-chat | OpenAI Chat Completions /v1/chat/completions |
| openai-responses | OpenAI Responses API |
| anthropic | Anthropic /v1/messages |

- 协议本质是「用户在 iCAP 里注册的那个端点走的什么协议」，必须与端点 URL 匹配。
- 选中 harness 后前端按 harness 的 preferred 协议给出提示（见 §4.4）。

## 3. UI 设计总则

与 iCAP 现有页面完全一致的视觉与交互约定（依据 `frontend/src/styles.css` 与
`views/Evaluations.vue`、`components/evaluation/*`）：

- 设计 token：品牌红 `--red: #c91e32`、弱化灰 `--muted: #748095`、描边 `--line: #e9ecf1`、
  深墨 `--ink: #18243b`。
- Element Plus 已全局定制（styles.css:90-93）：`el-button--primary` 红色、`el-tabs__active-bar`
  红色、`el-progress-bar__inner` 红色。新 UI 一律沿用 Element Plus 组件 + 现有自定义 class，
  不新造颜色/字号体系。
- 复用 class：`.card / .card-pad / .card-header / .grid.two / .stat* / .notice / .chip / .muted /
  .small / .toolbar / .flex-between / .log-box / .mono / .section-gap / .ruler-card`。
- 页面骨架：`.page-heading`（`.eyebrow` + `h1` + `.subtitle`）+ `.card` 卡片分区，与
  `Evaluations.vue` / `EvaluationDetail.vue` 一致。
- 弹窗：`el-dialog` + `el-form(label-position="top")`，与 `CreateEvaluationDialog.vue` 一致。
- 文案语气：中文，短句；真实执行用 `notice` 强调，模拟/未接入用「模拟执行」标注。

## 4. 发起评测弹窗（CreateEvaluationDialog.vue 改造）

### 4.1 「测评类型」下拉重构（el-option-group 分组）

现有下拉是「尺子 + 模拟」平铺。改为三组：

```
【真实评测 · 尺子】  Ruler1（通用能力） / Ruler2（通信领域）
【真实评测 · Agent】 Agent 评测（需 docker）
【模拟执行】         VLM / AISF / ASR / MT / TTS
```

- `Agent 评测` 是一个新选项（value 用一个固定枚举，如 `"Agent"`，与后端 `AGENTCOMPASS_IDS`
  的判定方式对齐，见 §7）。
- 选中后 `.notice` 文案：`该评测由 AgentCompass 真实执行，需要 docker 环境；提交后进入队列，
  单任务串行运行。`

### 4.2 选中「Agent 评测」后的表单（隐藏数据集，展开 agent 专属字段）

现有「数据集」字段对 Agent 评测隐藏（benchmark 自带数据），改为一条 muted 说明：

```
被测模型（可多选，最多 4 个；多选时并行对比）   ← 保留
数据集                                              ← 隐藏，替换为说明
  说明：Agent 评测使用 benchmark 内置数据，无需选择数据集
Benchmark（卡片选择）
Harness（下拉）
Environment（下拉）
模型 API 协议（下拉）
可见范围                                            ← 保留 VisibilityField
```

### 4.3 Benchmark 选择（卡片 grid，复用尺子卡片交互）

新增 `components/evaluation/BenchmarkPicker.vue`，交互对齐 `RulerPicker.vue`：

- `.grid.two` 两列卡片，每张卡片（复用 `.ruler-card` 样式，选中 `.active` 红框高亮）展示：
  - 标题：benchmark id（如 `swebench_verified`）
  - 描述：registry 的 `description` 原文（英文，论文引用，不翻译）
  - 底部：`建议 harness：mini_swe_agent`
- 卡片数据来自 `GET /api/agent-capabilities` 的 `benchmarks[]`（§7.1）。
- 弹窗宽度由 `min(680px,95vw)` 加宽到 `min(760px,95vw)`，容纳两列卡片。

### 4.4 Harness / Environment / 协议（el-select 下拉）

三个下拉并排或顺序排列，均为 `el-select`：

- **Harness**：3 个选项，label 用 `id · 一句话说明`（说明取 registry description 的中文精简，
  见 §7.1 的 `label` 字段）。选中后下方出现一条 `.notice`（或 muted 提示）显示该 harness 的
  preferred 协议，例如选中 `mini_swe_agent` → `该 harness 默认走 openai-chat 协议。`
- **Environment**：`docker` / `host_process` 两个选项。
- **模型 API 协议**：默认 `auto`；选项 `auto / openai-chat / openai-responses / anthropic`。
  当用户手动选了与 harness preferred 不符的协议时，用 `el-alert`（warning）提示「该协议与
  所选 harness 的推荐协议不一致，可能运行失败」。

### 4.5 高级参数（el-collapse，默认折叠）

对齐尺子分支的「高级参数」折叠面板，新增 agent 专属三项（默认用后端默认值，用户可改）：

```
并发任务数（task_concurrency，默认 1）
每任务最大尝试次数（k）
最大重试次数（max_retries）
```

提交时并入请求体（§7.2）。

### 4.6 提交逻辑

- 单模型：`POST /api/evaluations`，body 带 agent 字段（§7.2）。
- 多模型（batch）：`POST /api/evaluations/batch`，每个模型一条 agent 任务，共用同一组
  benchmark/harness/environment/protocol。
- 提交成功后沿用现有跳转：`/evaluations/{id}`。

## 5. 评测列表页（Evaluations.vue 改造）

1. 顶部 `.notice` 文案修正：把「VLM / AISF / Dev / ASR / MT / TTS 仍为模拟执行」改为
   「尺子一/二与 Agent 评测为真实执行；VLM / AISF / ASR / MT / TTS 为模拟执行」。
2. 「测评尺子」列改为「测评类型」列：
   - 尺子任务：沿用 `row.ruler_label`。
   - Agent 任务：用 `el-tag` 展示 `row.agent.benchmark`（+ 可选 harness 短标签），
     例如 `swebench_verified · mini_swe_agent`。
3. 其余列（模型 / 进度 / 综合分 / 状态 / 操作）不变；Agent 任务的「暂停/恢复」按钮不显示，
   仅保留「取消」与「详情」（见 §10 边界 1）。
4. 创建弹窗 `benchmarks` 常量改为从能力接口拉取，不再硬编码。

## 6. 评测详情页 + Agent 结果面板

### 6.1 EvaluationDetail.vue 分支

- 标题：尺子任务用 `task.ruler_label`；Agent 任务用 `task.agent.benchmark`（无 ruler_label）。
- 进度 / 日志 / SSE 重连逻辑不变。
- 操作按钮：Agent 任务不显示「暂停/恢复」，只显示「取消」（运行中）与「查看报告」（完成后）。
- 结果区：Agent 任务渲染 `AgentResultPanel`（新增），尺子任务继续渲染 `EvalResultPanel` +
  `CaseResultTable`。分支判断：`task.agent` 是否存在。

### 6.2 新增 components/evaluation/AgentResultPanel.vue

展示 Agent 结果（`result` 结构见 §7.3），不复用尺子雷达/用例明细。布局：

```
[综合解决率卡片]  [完成情况卡片]
  .stat-number = result.total%（解决率）
  .stat-note   = 完成 evaluated/total 条

[运行指标卡]
  el-descriptions 两列：
    平均时延  avg_latency_seconds s
    平均步数  avg_steps
    总 token  total_tokens
    错误数    error
    结果目录  run_dir（.mono 展示，供下钻）

[摘要]
  .card 内展示 result.summary
```

- 无雷达图、无用例明细表（Agent 结果无 cases）。
- `result.extra.run_dir` 以 `.mono` 等宽展示路径；首版不提供文件下载（原始产物在服务端，
  后续可加只读浏览接口）。

## 7. 前端-后端接口契约

### 7.1 GET /api/agent-capabilities（新增，替代现有 /api/agent-benchmarks）

返回白名单化的能力清单，前端下拉/卡片数据源：

```json
{
  "benchmarks": [
    {"id": "swebench_verified", "label": "SWE-bench Verified",
     "description": "SWE-bench: Can Language Models Resolve Real-World GitHub Issues?...",
     "suggested_harnesses": ["mini_swe_agent"]}
  ],
  "harnesses": [
    {"id": "mini_swe_agent", "label": "mini-SWE-agent",
     "description": "...", "preferred_protocols": ["openai-chat"]}
  ],
  "environments": [
    {"id": "docker", "label": "Docker 容器"},
    {"id": "host_process", "label": "宿主进程"}
  ],
  "protocols": [
    {"value": "auto", "label": "自动（harness 默认）"},
    {"value": "openai-chat", "label": "OpenAI Chat Completions"},
    {"value": "openai-responses", "label": "OpenAI Responses"},
    {"value": "anthropic", "label": "Anthropic Messages"}
  ]
}
```

- 数据源：后端 adapter 维护一份静态白名单 manifest（`AGENTCOMPASS_CAPABILITIES`），
  不在请求时 `load_builtin_components()`（避免重 import）。白名单即过滤，天然只暴露
  §2 已确认可用的项。description 从 registry 静态抄录，label 为中文精简名。

### 7.2 POST /api/evaluations（扩展）

`EvalInput` 增加可选 agent 字段（非 Agent 任务不传，向后兼容）：

```json
{
  "model_id": "...",            // 单模型
  "dataset_id": null,           // Agent 任务可选/置空
  "benchmark": "Agent",          // 新枚举值
  "visibility": "private",
  "agent": {
    "benchmark": "swebench_verified",
    "harness": "mini_swe_agent",
    "environment": "docker",
    "model_api_protocol": "auto",
    "params": {"task_concurrency": 1, "k": null, "max_retries": null}
  }
}
```

- 后端 `_make_eval_item`：`benchmark == "Agent"` 时按 `agent.*` 落库（替代现在的硬编码
  `AGENTCOMPASS_BENCHMARKS["Dev"]` 映射），`simulated=False`、`result=None`。
- `batch` 接口同样扩展：`agent` 字段一次性给定，逐模型拆子任务。
- `build_run_kwargs` 泛化：`benchmark/harness/environment/model_api_protocol` 直接读 task 里的
  `agent` 字段，不再查硬编码映射表（后端改动小，见 §8）。

### 7.3 结果结构（已有，前端按此渲染）

`AgentCompassRunner._finish` 已回写 `result`（单维 + extra）：

```json
{
  "mode": "live", "total": 87.5, "summary": "...", "finished_at": "...",
  "dimensions": [{"key": "resolve", "label": "任务解决率", "score": 0.875, "weight": 1.0}],
  "extra": {"run_id": "...", "run_dir": "...", "evaluated": 8, "total": 10,
             "error": 1, "avg_latency_seconds": 12.3, "avg_steps": 5, "total_tokens": 12345}
}
```

`AgentResultPanel` 按 `result.dimensions[0].score` 取解决率、`result.extra` 取统计指标。

## 8. 文件改动清单

### 后端（小改，泛化为主）

| 文件 | 改动 |
|------|------|
| `app/evaluation/agentcompass_adapter.py` | 新增 `AGENTCOMPASS_CAPABILITIES` 白名单 manifest；`build_run_kwargs` 改为读 task.agent 字段（benchmark/harness/environment/protocol/params），不再查 `AGENTCOMPASS_BENCHMARKS["Dev"]` |
| `app/main.py` | `EvalInput`/`BatchEvalInput` 加 `agent` 字段 + `benchmark` 加 `"Agent"` 枚举；`_make_eval_item` 按 `agent` 落库；新增 `GET /api/agent-capabilities`；`dataset_id` 对 Agent 任务改为可选 |

### 前端（主体）

| 文件 | 改动 |
|------|------|
| `components/evaluation/BenchmarkPicker.vue` | 新增，卡片选择 benchmark（复用 ruler-card 样式） |
| `components/evaluation/AgentResultPanel.vue` | 新增，Agent 结果面板（单维 + 统计指标） |
| `components/evaluation/CreateEvaluationDialog.vue` | 测评类型分组；Agent 分支表单（benchmark/harness/env/协议/高级参数） |
| `views/Evaluations.vue` | notice 修正；「测评类型」列；Agent 任务隐藏暂停/恢复 |
| `views/EvaluationDetail.vue` | 标题/按钮分支；结果区按 `task.agent` 分流到 AgentResultPanel |

## 9. 分阶段实施

1. **Phase 1 后端泛化**：`agent` 字段 + `AGENTCOMPASS_CAPABILITIES` + `GET /api/agent-capabilities`
   + `build_run_kwargs` 泛化 + `dataset_id` 可选。目标：后端能对任一白名单 benchmark/harness/env
   跑通并回写结果（先用 swebench_verified 一条样例验证闭环）。
2. **Phase 2 发起弹窗**：BenchmarkPicker + Agent 分支表单（harness/env/协议/高级参数）+ 提交。
3. **Phase 3 结果展示**：AgentResultPanel + 详情页分支 + 列表页修正。
4. **Phase 4 联调验证**：`npm run build` + 后端 pytest 回归 + 手动跑通「发起 → 运行 → 结果」。

## 10. 边界与风险

1. **pause/resume 对 Agent 任务是「假」的**：`action_evaluation` 会把状态改成 paused，但
   `AgentCompassRunner.step()` 只认 queued→start、done→finalize，没有 pause 逻辑，running 的
   asyncio 任务会继续跑完。**首版 Agent 任务只开放 cancel，前端不显示暂停/恢复**（§5/§6 已体现）。
2. **on_progress 需实测**：`build_run_kwargs` 里 `progress="none"`，`on_progress` 回调是独立
   参数，理论仍触发；Phase 1 用 swebench_verified 一条样例跑通时验证进度条确实随
   `tasks_loaded/task_finished` 递增。
3. **host_process 依赖 aioshutil**：fork 里 `host_process` 是 try-import，缺 aioshutil 会被
   静默跳过。接入时只读检查 vendored 环境是否装了 aioshutil，未装则在能力清单里隐藏
   host_process（只暴露 docker）。
4. **协议错配必失败**：协议必须匹配用户注册的端点，前端用默认 auto + 提示 + 警告兜底（§4.4），
   不做强校验（后端运行期报错会进 task.logs 与 failed 态）。

## 11. 已定结论（评审确认）

- [x] 范围：3 harness（openai_chat / mini_swe_agent / naive_search_agent）+ 2 environment
  （docker / host_process）+ ~8 benchmark；排除 claude_code / codex / pi / hermes / openclaw /
  openhands / openevolve / terminus2 / aisf_agent / qwen3vl_gui / scicode_tool_use /
  researchharness，及 benchmark aisf_iot / terminalbench2_1。
- [x] 所有 AISF 相关不暴露。
- [x] model_api_protocol 暴露给用户（auto + 3 值，默认 auto，按 harness 提示 preferred）。
- [x] dataset_id 对 Agent 任务改为可选（前端隐藏）。
- [x] Agent 任务不开放 pause/resume，只开放 cancel。
- [x] UI 完全沿用现有 Element Plus 风格，不引入新设计体系。
