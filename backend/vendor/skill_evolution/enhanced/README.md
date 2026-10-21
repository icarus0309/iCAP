# OPT SKILL 优化器 — 直接模式自进化工程

> SKILL 提示词自进化平台，支持 **direct（单 API 生成最终输出）** 与 **hermes（轨迹采集）** 两种执行模式。
> 本工程默认并重点维护 direct 模式；运行时核心仅依赖 Python 标准库（urllib），`requests` 为可选扩展。

## 核心理念

通过 **执行 → 评估 → 经验沉淀 → 策略选择 → 变异 → 版本回滚** 的闭环，自动优化 `SKILL.md` 提示词文件。融合两种互补的优化策略：

| 策略 | 机制 | 适用场景 |
|------|------|----------|
| **小步迭代（StructuredMutator）** | ADD / DELETE / REPLACE 结构化编辑 | 常规优化，每轮逐步改进 |
| **GEPA 探索性重写** | 停滞时触发，direct 模式走串行完整重写 | 陷入局部最优时全局探索 |

当连续多轮提升低于阈值（`STALL_THRESHOLD` / `STALL_ROUNDS`）时，自动从「小步迭代」切换到「探索性重写」，超过触发前分数即接受并停止。

## 两种执行模式

| 模式 | 执行方式 | 评估方式 | 知识提取 |
|------|----------|----------|----------|
| `direct`（默认） | 完整 SKILL 作为 system，按顺序追加数据集 JSON messages，一次逻辑模型调用生成最终输出 | 只评结果（word / format / onlyllm 三类判据） | 基于最终输出对比提取 |
| `hermes` | 通过 Hermes CLI 调用 SKILL 采集工具执行轨迹 | 三维评估（保留） | 关键步骤知识提取（保留） |

- **direct 模式**：`SKILL_EXEC_MODE=direct`，只评估结果，`total_score == result`，按有效案例等权平均。
- **hermes 模式**：`SKILL_EXEC_MODE=hermes`，需要配置 `HERMES_*` 变量。

## 目录结构

```
direct/
├── optimize_skill.py            # 主入口（bootstrap / self-evolve 子命令）
├── run_in_wsl.sh                # WSL 一键启动脚本
├── requirements.txt             # 仅 requests（可选），核心只用标准库 urllib
├── .env.example                 # 配置模板（复制为 .env 填写真实值）
├── 单API调用兼容与知识提取改造方案.md  # direct 模式改造设计文档
│
├── src/
│   ├── config.py                # 配置加载（.env / 模型配置）
│   ├── model_client.py          # LLM API 调用客户端（urllib）
│   ├── dataset.py               # 数据集加载
│   ├── logger.py                # 日志模块
│   ├── bootstrap/               # 初版 SKILL 自动生成
│   ├── scoring/                 # 评分基础设施（base / rules / rubric / report）
│   └── self_evolve/             # ★ 核心自进化引擎
│       ├── orchestrator.py      # 主编排器（策略切换 / 版本回滚 / 事件）
│       ├── schemas.py           # 数据模型
│       ├── boundary.py          # 收敛判断
│       ├── config_builder.py    # direct 选项构建
│       ├── evaluation_policy.py # 评估策略
│       ├── mechanism_rules.py   # 机制规则
│       ├── diagnostics.py       # 诊断日志
│       ├── experience_pool.py   # 经验池（成功/失败/拒绝 + 语义去重）
│       ├── experience_merge.py  # 经验合并
│       ├── evaluators/          # 评估器
│       │   ├── components/      #   session_analyzer / scorer / batch_analyzer / direct_judge
│       │   ├── direct_analysis_evaluator.py
│       │   ├── outcome_grouped_evaluator.py
│       │   └── result_first_router.py   # 结果先行路由
│       ├── execution/           # 执行器
│       │   ├── direct_runner.py #   direct 单 API 执行
│       │   ├── direct_case.py
│       │   ├── dual_model_runner.py     # 双模型对比
│       │   ├── hermes_runner.py #   Hermes CLI 执行
│       │   ├── executor.py      #   跨平台执行器抽象（local/wsl/docker/ssh）
│       │   ├── pre_condition.py #   前置条件注入
│       │   ├── session_parser.py
│       │   └── trajectory_extractor.py  # 轨迹提取
│       ├── knowledge/           # 知识提取（direct 结果对比 + hermes 关键步骤）
│       └── mutators/            # 变异器
│           ├── structured_mutator.py    # 小步迭代（ADD/DELETE/REPLACE）
│           ├── direct_exploratory_mutator.py  # direct 串行探索重写
│           ├── freeform_mutator.py
│           └── gepa/            # GEPA 种群进化引擎
│
├── scripts/
│   ├── run_self_evolve.py       # .env 驱动的自进化启动器
│   ├── run_bootstrap_skill.py   # SKILL 生成启动器
│   └── convert_xlsx_to_dataset.py  # Excel → JSON 数据集转换
│
├── skills/                      # 目标 SKILL 文件
│   ├── iot_control_v2/
│   ├── iot_control_distill_v2/
│   └── memory/                  #   identity / movie 子 SKILL
│
├── datasets/                    # 测试用例集 + 评审模板（JSON / md / txt）
├── examples/direct/             # direct 模式接口演示（SKILL.md + judge.md）
├── docs/                        # 设计文档
├── tests/                       # 离线回归测试（不调用真实模型）
└── output/                      # 进化输出（每次运行一个子目录）
```

## 快速开始

### 1. 环境准备

项目运行时核心仅依赖标准库，Python 3.9+ 即可。可选的 `requests` 依赖：

```bash
pip install -r requirements.txt
```

如需 hermes 模式，请确保 Hermes CLI 已安装并在 PATH 中。

### 2. 配置文件

复制 `.env.example` 为 `.env` 并填写真实值。关键配置：

```ini
# 执行模式（direct=默认；hermes=轨迹采集）
SKILL_EXEC_MODE=direct
SELF_EVOLVE_SKILL_PATH=skills/memory/SKILL.md
SELF_EVOLVE_DATASET_PATH=datasets/identity.json
SELF_EVOLVE_ROUNDS=3
SELF_EVOLVE_CASES_LIMIT=          # 留空跑全部用例
SELF_EVOLVE_RESUME=               # 续跑时填已有 output/evo_* 目录

# 公共 API（direct 执行模型未单独配置时回退到这里）
API_BASE=your_api_base_here
API_KEY=your_api_key_here
MODEL_NAME=qwen3.8-27b

# 执行模型（direct 模式）
EXEC_MAX_TOKENS=4000
EXEC_TIMEOUT=120
LARGE_EXEC_MODEL=                 # 大执行模型：小模型失败后才执行

# 评审 / 生成模型
EVAL_MODEL=deepseek-v4-pro
EVAL_TIMEOUT=3000
EVAL_MAX_TOKENS=8000
GEN_MODEL=deepseek-v4-pro
GEN_TIMEOUT=2000
GEN_MAX_TOKENS=8000

# 结果判据权重（word/format/onlyllm）
OUTPUT_WEIGHT_WORD=1
OUTPUT_WEIGHT_FORMAT=1
OUTPUT_WEIGHT_ONLYLLM=1
OUTPUT_WEIGHT_TEXT=0
OUTPUT_WEIGHT_TOOL=0
OUTPUT_WEIGHT_FILE=0

# 优化目标
RESULT_THRESHOLD=80
SELF_EVOLVE_THRESHOLD=85
MAX_STEPS=5
STALL_THRESHOLD=2.0
STALL_ROUNDS=2
GEPA_ITERATIONS=3
```

### 3. 运行自进化

**方式一：WSL 一键启动（推荐）**

```bash
bash run_in_wsl.sh
```

脚本会自动创建虚拟环境、安装依赖、按 `.env` 的 `SKILL_EXEC_MODE` 启动。

**方式二：命令行**

```bash
python optimize_skill.py self-evolve skills/memory/SKILL.md datasets/identity.json \
  --exec-mode direct --rounds 3 --threshold 85
```

**方式三：.env 驱动启动器**

```bash
python scripts/run_self_evolve.py
```

### 4. 生成初版 SKILL

```bash
python optimize_skill.py bootstrap "物联网设备控制" --output-dir generated_skills
```

## direct 模式结果评审

direct 模式只评估结果，支持三类判据（按权重归一化，权重为 0 的类型不参与）：

| 类型 | 说明 |
|------|------|
| `word` | 关键词黑/白名单硬校验 |
| `format` | JSON 结构格式校验 |
| `onlyllm` | rubric 模型判定（`issues` / `审核结果` / 0–100 分） |

- 数据集用例 output 为空时，自动注入 `DIRECT_JUDGE_PROMPT` 指定的运行级模板（如 `datasets/memory_rubric_online.txt`）。
- 也可在用例 output 中声明 `{"type":"onlyllm","data":"judge.md"}`，该路径优先相对数据集目录解析。
- `RESULT_THRESHOLD` 控制成功/失败分类及大模型触发；`SELF_EVOLVE_THRESHOLD` 控制整体优化目标。

## 双模型结果先行路由

小执行模型（`EXEC_*`）先执行；结果失败时再调用大执行模型（`LARGE_EXEC_MODEL`）对比，提取知识。小模型失败且大模型成功时，对比双方最终输出和 judge 原文提取知识，引用片段逐字验证通过才入经验池。

## 单 API 执行与知识提取

统一使用 `exec_mode=direct` 表示单 API 执行，`evaluation_mode=result_only` 表示只评估结果。执行时完整 SKILL 作为 system，按顺序追加数据集的 JSON messages，通过一次逻辑模型调用生成最终输出。

- 小模型使用 `EXEC_MODEL/API_BASE/API_KEY`，未配置时回退到通用 `MODEL_NAME/API_BASE/API_KEY`；大小模型均支持各自的 `*_TEMPERATURE`、`*_MAX_TOKENS`、`*_TIMEOUT`。
- 只有显式配置 `LARGE_EXEC_MODEL` 才启用大模型；结果 judge 使用 `EVAL_*`，改写使用 `GEN_*`。
- 模型客户端通过项目现有配置构建，CLI 参数覆盖环境配置。

直接模式接口演示：

```bash
python optimize_skill.py self-evolve examples/direct/SKILL.md examples/direct/dataset.json \
  --exec-mode direct --direct-judge-prompt examples/direct/judge.md --gepa-iterations 3
```

## 停滞与探索性重写（direct）

停滞触发 GEPA 时，direct 走串行完整重写，每轮使用固定全量评估集评估。首次超过触发前分数就接受并停止，每次触发最多 3 轮（`GEPA_ITERATIONS` 可设为 1/2，超过 3 按 3 执行）。不运行种群、父候选选择、Pareto 或分支合并；三轮未提高则保留原版和原拒绝经验。重写文本、逐案例评分和尝试 checkpoint 位于 `round_N/direct_exploration/attempt_N/`。

## 缓存与续跑

- 执行输出、评审、知识产物按输入、SKILL、模型、模板、阈值及版本分别校验缓存。
- 仅修改 judge/阈值时可复用兼容执行输出再评审；执行模式不同的运行不能直接续跑。
- 评审策略改变时重建历史评分状态，不比较不同口径的历史总分；有效案例集合变化时停止跨轮分数比较。
- 断点续跑：`SELF_EVOLVE_RESUME` 或 `--resume` 传入已有 `output/evo_*` 目录。

## 跨平台执行器（hermes 模式）

| 执行器 | 说明 |
|--------|------|
| `local` | 本地执行（默认） |
| `wsl` | WSL 中执行（自动路径转换） |
| `docker` | Docker 容器中执行 |
| `ssh` | 远程 SSH 执行 |

配置方式：`.env` 中设置 `EXECUTOR_TYPE=wsl`，或命令行 `--executor-type wsl`。

## 输出目录结构

```
output/evo_SKILL_<timestamp>/
├── initial_skill.md            # 初始 SKILL 副本
├── final_skill.md              # 最终优化后的 SKILL
├── summary.json                # 运行摘要（含 score_history / best_score）
├── lineage.json                # 版本演变
├── experience_pool.json        # 经验池
├── events.json                 # 事件流
├── self_evolve.log             # 诊断日志
└── round_N/                    # 每轮详情
    ├── round_summary.json
    ├── skill/                  # 本轮 SKILL
    ├── cases/                  # 用例结果
    └── direct_exploration/     # direct GEPA 串行重写
        └── attempt_N/
```

## 离线回归

不调用真实模型：

```bash
python -B -m unittest discover -s tests -v
```

## 相关文档

- `单API调用兼容与知识提取改造方案.md` — direct 模式与知识提取改造的完整设计
- `docs/design.md` — 架构设计文档
- `docs/merge_plan.md`、`docs/design_dataloop.md` — 数据流与合并方案
