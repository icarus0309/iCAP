# OPT — Outcome-Partitioned Tuning: SKILL 自优化系统

基于结果分组评估 + 结构化编辑的 SKILL 提示词自动迭代优化工具。

## 概述

OPT 面向 SKILL 提示词的**受控迭代优化**，核心思路：

1. **评估阶段** — 按得分阈值将用例拆分为成功组和失败组，分别提取经验（保留什么 / 修复什么）
2. **变异阶段** — LLM 规划限量 JSON 编辑操作（ADD/DELETE/REPLACE），由代码机械执行

## 项目结构

```
├── optimize_skill.py          # 入口（bootstrap / self-evolve 子命令）
├── scripts/run_self_evolve.py # 便捷启动脚本（从 .env 读取配置）
├── run_in_wsl.sh              # WSL 一键启动（环境检查 + 自动装依赖）
├── src/self_evolve/           # OPT 核心
│   ├── orchestrator.py        # 主编排器
│   ├── evaluators/            # OutcomeGroupedEvaluator + scorer + analyzer
│   ├── mutators/              # StructuredMutator（ADD/DELETE/REPLACE）
│   └── execution/             # Hermes CLI runner + pre_condition hook
├── ha_adapter/                # Mock HA REST 适配器（前置条件注入）
├── mock_ha/                   # Mock HA 服务器（独立进程，需提前启动）
└── docs/opt_design.md         # 详细设计文档
```

## 快速开始

```bash
# 1. 配置
cp .env.example .env
# 编辑 .env，填入模型 API 地址和密钥

# 2. (可选) 启动 Mock HA——如果需要前置条件注入的话
bash mock_ha/start_mock_ha.sh &

# 3. 运行自优化
bash run_in_wsl.sh

# 或直接调用
python optimize_skill.py self-evolve <skill_path> <dataset_path>
```

## 执行流程

```
加载 SKILL.md + 数据集
  │
  └─ [轮次循环] for round in 1..N:
       │
       ├─ 1. Mock HA 前置条件注入 (若 ENABLE_PRE_CONDITION=true)
       │
       ├─ 2. 执行全部用例
       │     Hermes CLI → 轨迹解析 → SessionAnalyzer(LLM 三维度分析)
       │     → WeightedScorer → 每用例产生 ScoreBreakdown
       │
       ├─ 3. OutcomeGroupedEvaluator 评估
       │     ├ 按 threshold 分组: success / failure
       │     ├ LLM 汇总成功经验 (≤200字) + 失败经验 (≤200字)
       │     └ 加载历史拒绝经验 → EvaluationResult
       │
       ├─ 4. BoundaryChecker 收敛判断
       │     avg_score >= stop_threshold → 停止
       │
       └─ 5. StructuredMutator 变异
            ├ 构建编辑上下文 (SKILL + 经验 + 拒绝经验)
            ├ LLM 输出 JSON 编辑计划 (ADD/DELETE/REPLACE, 最多 max_steps 步)
            └ 代码机械执行编辑 → 下一轮 SKILL
```

## 配置参数

| 参数 | 环境变量 | 默认值 | 说明 |
|------|---------|--------|------|
| rounds | `SELF_EVOLVE_ROUNDS` | 6 | 最大优化轮次 |
| threshold | `SELF_EVOLVE_THRESHOLD` | 85.0 | 停止优化阈值 |
| cases_limit | `SELF_EVOLVE_CASES_LIMIT` | None | 每轮执行用例上限 |
| output_dir | `SELF_EVOLVE_OUTPUT_DIR` | output | 输出根目录 |
| structured_mutator_max_steps | `STRUCTURED_MUTATOR_MAX_STEPS` | 5 | 编辑步数上限 |
| outcome_split_threshold | `OUTCOME_SPLIT_THRESHOLD` | 70.0 | 成功/失败分组阈值 |
| enable_pre_condition | `ENABLE_PRE_CONDITION` | false | Mock HA 前置条件注入 |

## 输出目录

```
output/opt_<skill_name>_<timestamp>/
├── initial_skill.md
├── optimized_skill.md          ← 历史最佳版本
├── final_skill.md              ← 最后一轮版本
├── round_1/
│   ├── skill/SKILL.md
│   ├── next_skill.md           ← 变异后 SKILL
│   ├── cases/<case_id>/
│   │   ├── session.export.jsonl
│   │   ├── session.normalized.json
│   │   ├── trajectory.json
│   │   ├── session_analysis.json
│   │   └── score.json
│   └── round_summary.json
├── round_2/ ...
├── lineage.json
└── self_evolve_summary.json
```

## 模型调用一览

| 调用点 | 每轮次数 | 说明 |
|--------|---------|------|
| SessionAnalyzer 三维合并分析 | N (用例数) | 单次 LLM 合并分析 accuracy/efficiency/output_quality |
| 成功组经验汇总 | 1 | ≤200字，提取保留模式 |
| 失败组经验汇总 | 1 | ≤200字，定位修复方向 |
| StructuredMutator 编辑规划 | 1-2 | JSON 编辑计划，解析失败时重试1次 |

**每轮总计: N + 3~4 次 LLM 调用**

## 命令行参数

```
python optimize_skill.py self-evolve <skill_path> <dataset_path> [options]

  -o, --output-dir        输出目录 (默认: output)
  --rounds                最大轮次 (默认: 6)
  --threshold             停止阈值 (默认: 85.0)
  --cases-limit           仅运行前 N 条用例
  --structured-mutator-max-steps  编辑步数上限 (默认: 5)
  --outcome-split-threshold      分组阈值 (默认: 70.0)

  # 前置条件
  --enable-pre-condition  启用 Mock HA 前置条件注入
  --mock-ha-url           Mock HA 地址 (默认: http://127.0.0.1:8124)
  --mock-ha-token         Mock HA token (默认: mock-token)

  # Hermes 配置
  --hermes-bin, --hermes-model, --hermes-home, --hermes-timeout
  --hermes-yolo, --hermes-max-turns

  # 模型配置
  --eval-model, --eval-api-base, --eval-api-key
  --gen-model, --gen-api-base, --gen-api-key

  # 执行器
  --executor-type (local/wsl/docker/ssh)
```

## 子命令

- `bootstrap` — 从需求文本自动生成初版 SKILL 包
- `self-evolve` — 运行 OPT 自演进流程

## 依赖

```
openai
requests
```
