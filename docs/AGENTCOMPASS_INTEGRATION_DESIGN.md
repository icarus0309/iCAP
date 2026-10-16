# AgentCompass 集成设计文档（进程内 vendor）

> 分支：yyl-dev
> 状态：设计评审稿，未实施
> 目标：把 agent-eval-agentCompass（AgentCompass）以「进程内 vendor」方式集成进 iCAP，
>       iCAP 自包含、不 import 兄弟目录、不起 sidecar、不 spawn 子进程。
> 第一版范围：接通 swebench_verified（Dev 域），harness=mini_swe_agent，env=docker。

---

## 1. 背景与目标

- iCAP 是 FastAPI + Vue3 的 MaaS 平台。评测域现状：Ruler1/Ruler2 走自研引擎
  （`backend/app/evaluation/`），VLM/AISF/Dev/ASR/MT/TTS 走 `simulate_legacy_evaluations` 模拟。
- AgentCompass 是 Agent 评测框架：20+ benchmark、10+ harness、多环境（docker/host_process/daytona/modal）。
- 目标：把 AgentCompass 变成 iCAP 的一部分（vendor 代码 + 合并依赖），iCAP 后端进程内
  `import agentcompass` 并 `await async_run_evaluation(...)`，替代 Dev 等模拟评测。

## 2. 集成方式与关键约束

方式：**进程内 vendor**。把 `src/agentcompass/` 整包搬进 iCAP 仓库，iCAP 直接 import。

实测规模（依据，非估算）：
- 代码：295 个 .py，79,421 行，5.1 MB（`src/agentcompass/`，不含 __pycache__）
- 基础依赖：约 50 个包（见 §5）
- 数据：data/ 共约 1.05 GB（swebench_verified 7.8M 是第一版唯一进 git 的）
- Python：iCAP venv 3.14.0，AgentCompass 要求 >=3.12 → 版本兼容

约束与代价（必须接受的现实边界）：
1. 代码（5.1M）进 git 自包含 ✅
2. 依赖（50+）必须合并进 iCAP venv，否则 `import agentcompass` 失败。iCAP 后端从 6 个轻依赖
   变成 50+ 重依赖，其中 daytona/modal/opensandbox/harbor 是外部云沙箱 SDK（硬依赖，装了才能
   import `agentcompass.environments`）。
3. 数据（1.05GB）不进 git：swebench_verified/test.json（7.8M）进 git；其余按需下载。
4. docker 镜像 / 外部 CLI（codex/claude_code/openclaw 等 harness）：运行环境依赖，非代码，
   部署时预拉/预装，与"代码自包含"无关。

## 3. 目录结构设计

```
iCAP/
  backend/
    vendor/
      agentcompass/            # 整包 vendor（源：agent-eval-agentCompass/src/agentcompass/）
        runtime/  benchmarks/  harnesses/  environments/  analyzers/  recipes/  cli/
        launcher.py  errors.py  utils/  __init__.py
    app/
      evaluation/
        agentcompass_adapter.py    # iCAP 侧适配器（调用 async_run_evaluation）
        agentcompass_reports.py    # 结果 → 报告映射
    data/
      agentcompass/            # 评测数据目录（swe_bench_verified/test.json）
      agentcompass_results/    # 评测结果目录（独立，不混 agentCompass 自带 results/）
    requirements.txt           # 追加基础依赖（§5）
    requirements-swebench.txt  # 第一版评测引擎可选依赖
  docs/
    AGENTCOMPASS_INTEGRATION_DESIGN.md   # 本文档
```

## 4. vendor 范围与剔除清单

vendor 源目录：`/mnt/d/app/pyProjects/agent-eval-agentCompass/src/agentcompass/`
vendor 目标：`/mnt/d/app/pyProjects/iCAP/backend/vendor/agentcompass/`

**剔除清单（仅 web UI，共 2 处，已 grep 核实）：**

| # | 位置 | 操作 |
|---|------|------|
| 1 | `web/` 整个目录 | 删（UI 由 iCAP 前端重做） |
| 2 | `cli/main.py` `web_command`（第 1057–1067 行，`from agentcompass.web.app import app`） | 删该命令 |

说明：
- **aisf 相关代码保留**：`benchmarks/aisf_iot.py`（AisfIoTBenchmark）、`harnesses/aisf_agent.py`
  （AisfAgentHarness）及其 `__init__.py` 的 import 全部保留 vendor，不剔除。
- `registry.load_builtin_components()` 是 import **包**（`importlib.import_module("agentcompass.benchmarks")`），
  组件靠各包 `__init__.py` 的显式 `from .xxx import Xxx` 注册；web 不在 `_BUILTIN_MODULES` 列表里，剔 web 不影响注册。
- `cli/main.py` 的 `web_command` 依赖 `agentcompass.web.app`，剔 web 必须同步删该命令（唯一引用点，已核实）。
- `swebench_verified` 的 docker recipe import 了 `harnesses.claude_code`，因此 **claude_code harness 必须保留**，
  不得因"第一版用 mini_swe_agent"而扩大剔除范围。

## 5. 依赖合并

iCAP 现状 `backend/requirements.txt`（6 个包）：
```
fastapi>=0.115,<1  uvicorn[standard]>=0.30,<1  python-multipart>=0.0.9,<1
httpx>=0.27,<1  python-dotenv>=1,<2  pytest>=8,<10
```

追加 AgentCompass 基础依赖（自其 pyproject.toml `dependencies` 精确抄录，去重后）：
```
aiofiles  aiohttp  aioshutil  aiosqlite  anthropic  backoff  cyclopts  datasets
daytona  filelock  harbor  jsonpath-ng  jsonschema  litellm  modal  openai
opensandbox  packaging  Pillow  pydantic  PyYAML  requests  rich  shortuuid
tabulate  tenacity  toml  uvloop(非 win32)
```
（fastapi/httpx/python-dotenv/uvicorn 已存在，不重复。）

第一版评测引擎可选依赖 `backend/requirements-swebench.txt`：
```
swebench==4.1.0
mini-swe-agent==2.4.5
```
（`datasets` 已在基础依赖里；swebench_verified 的 `dependency_spec` 声明 `extra="swebench"`。）

决策（已定）：requirements.txt 追加（不改 pyproject.toml）。启动脚本 `pip install -r requirements.txt`
不变，第一版评测引擎可选依赖单独成 `requirements-swebench.txt` 按需装。

## 6. 数据与结果目录

- 数据（进 git）：`backend/data/agentcompass/` 下 swe_bench_verified/test.json（7.8M，第一版用）、
  aisf_iot/data.json（36K）、aisf_qa/data.json（4K）。其余重数据集（skillsbench/hle/gaia 等约 1GB）按需下载。
  swebench_verified 兜底下载源（benchmark 内置）：`http://opencompass.oss-cn-shanghai.aliyuncs.com/datasets/agentcompass/swe_bench_verified.zip`
  （阿里云 OSS，国内可达）。`load_tasks` 优先读本地 data_dir，缺失时走 HF。
- 结果：`backend/data/agentcompass_results/`（results_dir 根目录）。AgentCompass 原生 runtime 在其下
  生成 `results/<benchmark>/<model>/<run_id>/` 目录树（run_id 默认时间戳，如 `20261008_175223`），
  产物文件原样保留，iCAP 只读不改（详见 §8「结果保留策略」）。
- 两目录均通过 `.env` 配置（§10），运行时作为 `async_run_evaluation(data_dir=..., results_dir=...)` 显式传入。

## 7. iCAP 侧适配器设计

文件：`backend/app/evaluation/agentcompass_adapter.py`

```python
"""iCAP 侧 AgentCompass 评测执行器（进程内 vendor 适配层）。"""

from agentcompass.launcher import async_run_evaluation   # vendor 后可用

# iCAP benchmark 枚举值 → AgentCompass 组件映射（第一版只接 Dev）
AGENTCOMPASS_BENCHMARKS: dict[str, dict] = {
    "Dev": {
        "benchmark": "swebench_verified",
        "harness": "mini_swe_agent",
        "environment": "docker",
        "model_api_protocol": "openai-chat",
        "harness_params": {"step_limit": 100, "command_timeout": 2400},
        "environment_params": {"resources": {"cpu": 2, "memory_mb": 4096}},
        "benchmark_params": {},          # 可放 sample_ids 限跑
    },
}
AGENTCOMPASS_IDS = frozenset(AGENTCOMPASS_BENCHMARKS)   # {"Dev"}


def build_run_kwargs(task: dict, model: dict, *, data_dir: str, results_dir: str) -> dict:
    """把 iCAP 任务 + 模型端点组装成 async_run_evaluation 的 kwargs。

    - model 端点的 url 是 base（不含 /chat/completions），原样传给 --model-base-url，
      由 AgentCompass 按 protocol 拼接，iCAP 侧不自行拼路径。
    - api_key 走 registry.api_key；protocol 用映射表的 model_api_protocol。
    - benchmark_params 支持 task.config 里透传 sample_ids。
    """


class AgentCompassRunner:
    def __init__(self, store, registry, *, data_dir, results_dir, stop, enabled: bool = False):
        self.store = store; self.registry = registry
        self.data_dir = data_dir; self.results_dir = results_dir
        self.stop = stop; self.enabled = enabled
        self._tasks: dict[str, asyncio.Task] = {}      # task_id → 运行中的 asyncio.Task

    async def step(self) -> None:
        """advance_jobs 每 tick 调用。取首个 queued/running 的 Dev 任务，启动/轮询。"""

    async def _start(self, task: dict) -> None:
        """create_task(async_run_evaluation(**build_run_kwargs(...), on_progress=...))"""

    def _on_progress(self, task_id: str, event) -> None:
        """进度回调：把 ProgressEvent 转成 task.progress / task.logs 写回 store。"""

    async def _finalize(self, task_id: str, run_result: dict) -> None:
        """完成后读 results/<benchmark>/<model>/<run_id>/，生成 report + result 写 store。"""

    def shutdown(self) -> None:
        """应用关闭：cancel 未完成的 asyncio.Task（精确到 task_id，不做批量清理）。"""
```

关键设计点：
- `enabled` 默认 False：iCAP 后端默认不启用真实评测（不起 docker），显式配置才开。
- 并发：iCAP 现有 worker 单 worker 串行；Dev 任务单并发（`task_concurrency=1`），复用 `active_task_id` 防重入。
- 子进程/容器清理：`shutdown` 只 cancel 自己 track 的 asyncio.Task；docker 容器由 AgentCompass 环境层
  管理，iCAP 不做批量 `docker rm`/`docker kill`。

## 8. 报告/结果映射

文件：`backend/app/evaluation/agentcompass_reports.py`

```python
def map_run_to_report(task: dict, run_result: dict, run_dir: Path) -> tuple[dict, dict]:
    """读 run_dir 下 run_info.json/metrics.json/run_metrics.json/summary.md，
    返回 (report, result)，结构与 iCAP 现有 report/result 兼容。"""
```

映射规则（swebench_verified 无 5 维度尺子，采用泛化结构）：
- `score` = round(正确数 / 总数 × 100, 1)
- `summary` = `summary.md` 文本（缺失则用 metrics 摘要拼一句）
- `dimensions` = 单一维度 `[{"key": "resolve", "label": "任务解决率", "score": correct/total, "weight": 1.0}]`
- `result.extra` = `{"run_id", "run_dir", "correct", "total", "raw_metrics"}`，供详情页 drill-down
- `report.simulated` = False；`report.ruler_id` = None（非尺子）；新增 `report.agent = {"benchmark","harness","environment"}`

### 结果保留策略（原始结果与生成方式原样保留）

集成后评测结果的**生成方式、目录结构、产物文件**完全由 AgentCompass 原生 runtime 生成
（vendor 后原样保留的 `runtime/results/store.py`），iCAP 不重写、不替换、不删除。

- 目录结构（原样）：`results/<benchmark>/<model>/<run_id>/`，run_id 默认时间戳
  （如 `20261008_175223`），也可用 `--run-name` 追加后缀（如 `20260910_193956_codex_1`）。
- 产物文件（全部保留）：run_info.json、metrics.json、run_metrics.json、progress.json、
  progress.jsonl、summary.md、analysis_summary.md、analysis_summary.json、params.json、
  cli.txt、details/*.json（逐任务轨迹）、logs/*.log（运行日志）。
- iCAP 是**叠加读取层**：`map_run_to_report` 读这些文件生成 iCAP 自己的 report/result
  （存 platform.json）用于列表/详情展示；原始 results 目录与产物保持不动，
  result.extra 存 run_dir 路径供前端 drill-down 到原始产物。
- 结论：原始结果（AgentCompass）与平台报告（iCAP）两套并存——前者是事实来源，后者是展示视图。
- 历史结果：源仓库 `agent-eval-agentCompass/results/` 里的既有结果（如 terminal_bench_2_1、
  browsecomp_zh 等）原样保留不动（vendor 不碰源仓库）；如需在 iCAP 展示历史，可将其作为
  results_dir 的初始内容一并纳入（可选，非必需）。

结果目录结构（AgentCompass 既有格式，iCAP 只读）：
```
results/<benchmark>/<model>/<run_id>/
  run_info.json     # status / started_at / finished_at / request / schema_version
  metrics.json      # correct 率 + counts
  run_metrics.json  # summary（时延/token/步数）
  progress.json(.jsonl)
  summary.md  analysis_summary.md  details/*.json  logs/*.log
```

## 9. main.py 改动点

1. `advance_jobs`（约 255 行）：在 `simulate_legacy_evaluations` 之前插入 `await agent_runner.step()`；
   Dev 任务由 `AgentCompassRunner` 推进，`simulate_legacy_evaluations` 的排除条件加上 `AGENTCOMPASS_IDS`
   （Dev 不再走模拟）。
2. `_make_eval_item`（约 594 行）：`benchmark in AGENTCOMPASS_IDS` 时补 `agent` 映射字段（benchmark/harness/
   environment/protocol），`result=None`、`simulated=False`（与 Ruler 的 ruler_label/config 逻辑并列）。
3. `create_app`（约 280 行）：构造 `AgentCompassRunner(store, registry, data_dir=..., results_dir=..., stop=stop,
   enabled=os.getenv("AGENTCOMPASS_ENABLED")=="true")`，传入 `advance_jobs`；lifespan 关闭时调 `runner.shutdown()`。
4. 新增只读接口（v1 只加一个，详情接口推迟到前端阶段）：
   - `GET /api/agent-benchmarks` → 返回 `AGENTCOMPASS_BENCHMARKS` 映射 + 数据就绪状态（test.json 是否存在）。
   - `GET /api/evaluations/{id}/agent-result` → 透传 run_info/metrics/run_metrics/summary（推迟到前端阶段）。

## 10. 配置键（.env，跟随 iCAP 现有约定）

```
AGENTCOMPASS_ENABLED=false                       # 是否启用真实 Agent 评测（默认关，不起 docker）
AGENTCOMPASS_DATA_DIR=backend/data/agentcompass   # 数据目录
AGENTCOMPASS_RESULTS_DIR=backend/data/agentcompass_results   # 结果目录
```
- config 值原样使用，不自动拼路径；`results_dir`/`data_dir` 显式传给 `async_run_evaluation`。

## 11. 进程内调用流程（时序）

```
用户 POST /api/evaluations {benchmark:"Dev", model_id, dataset_id}
  → _make_eval_item 落库 status=queued
advance_jobs 每 tick:
  → AgentCompassRunner.step() 选中首个 queued Dev 任务
  → _start: build_run_kwargs(task, model) + asyncio.create_task(async_run_evaluation(...))
  → on_progress 回调 → 写 store（progress/logs），现有 /api/evaluations/{id}/events SSE 自然生效
  → async_run_evaluation 返回 dict（含 run_id / 结果目录）
  → _finalize: 读 results/<benchmark>/<model>/<run_id>/ → map_run_to_report → 写 report + result
  → task.status = completed / failed
```

## 12. 状态机映射

| AgentCompass run_info.status | iCAP task.status |
|------------------------------|------------------|
| running                      | running |
| completed                    | completed |
| failed / cancelled / 其它    | failed（cancelled 由 iCAP 的 pause/cancel 动作控制，见下） |

- iCAP 的 `PATCH /api/evaluations/{id}`（pause/resume/cancel）对 Dev 任务：AgentCompass 原生怎么做就怎么做，
  iCAP 不额外干预——cancel 通过 `stop` 事件 + `shutdown` 精确 cancel 对应 asyncio.Task，
  其余行为完全交由 AgentCompass runtime 决定。
- 进程重启：`AgentCompassRunner` 无内存态，重启后 queued 任务重新排队（同 Ruler 现有逻辑）。

## 13. 测试清单

`backend/tests/test_agentcompass.py`（不真跑 docker / 不真起容器）：

1. 映射表完整性：`AGENTCOMPASS_BENCHMARKS` 每项含 benchmark/harness/environment/model_api_protocol。
2. `build_run_kwargs`：给定 task + model fixture，断言 kwargs 的 benchmark/harness/environment/protocol、
   model_base_url（原样，不拼 /chat/completions）、data_dir/results_dir、harness_params 正确。
3. 报告映射：用 fixture 假 run 目录（run_info.json/metrics.json/summary.md），断言
   `map_run_to_report` 的 score/summary/dimensions/extra 结果。
4. on_progress 回写：mock store，喂 ProgressEvent，断言 task.progress / logs 更新。
5. vendor 后 import 完整：`import agentcompass` + `load_builtin_components()` 不抛异常、
   `BENCHMARKS.names()` 含 swebench_verified 与 aisf_iot、`HARNESSES.names()` 含 mini_swe_agent 与 aisf_agent。
6. 状态机：`step()` 对 queued → running → completed 的推进（mock async_run_evaluation 返回假 dict）。

## 14. 风险与前置条件

1. **Python 3.14 下云沙箱 SDK 可装性**（已验证 ✅）：daytona 0.224.1 / modal 1.6.1 / opensandbox 0.1.16 /
   harbor 0.24.0 均为 py3-none-any 纯 Python wheel，requires-python 未锁 <3.13，可装。
2. **依赖版本冲突**（已验证 ✅）：合并后的完整依赖树（约 200 包）在 Python 3.14.4 下 dry-run 解析无冲突，
   C 扩展（uvloop 0.23.0 / pillow 12.3.0 / pandas 3.0.6 / numpy 2.5.4 等）均有 cp314 wheel。
   实装后仍需跑现有 29 个测试回归确认行为兼容。
3. **docker 镜像**：swebench_verified 共 500 个 tag（`swebench/sweb.eval.x86_64.{repo}_1776_{issue}`），
   验证阶段只拉 astropy 1 个（quick start 的 `astropy__astropy-12907`），不批量预拉。
4. **eval 耗时**：镜像内跑 pytest，单条分钟级；第一版只跑 1 条 sample 验证闭环，不跑全量 500 条。
5. **conda 依赖**：eval 要求镜像内有 conda（`_discover_conda`），prebaked 镜像已含，仍需实测确认。

## 15. 分步实施清单（"需要做什么"）

1. **行尾符**：yyl-dev 工作区 63 个文件为行尾符噪音（8865 加 = 8865 减）。实施前先定策略
   （`.gitattributes` 或确认 core.autocrlf），避免 vendor 后 git diff 全是噪音。
2. **vendor 代码**：`cp -r` 整个 `src/agentcompass/` 到 `backend/vendor/agentcompass/`，按 §4 剔除 2 处（web/ + cli web_command）。
3. **依赖**：追加 §5 基础依赖到 requirements.txt，新增 requirements-swebench.txt；重装 venv 验证
   `import agentcompass` 成功 + 现有 29 测试回归通过。
4. **适配器**：写 `agentcompass_adapter.py`（映射表 + Runner）、`agentcompass_reports.py`（报告映射）。
5. **main.py**：接入 `advance_jobs` / `_make_eval_item` / `create_app` / 新增 2 个只读接口（§9）。
6. **数据**：swebench_verified/test.json 落位 `backend/data/agentcompass/`（进 git）。
7. **测试**：写 `test_agentcompass.py`（§13）。
8. **验证闭环**：`AGENTCOMPASS_ENABLED=true` + 预拉 1 个 astropy 镜像，跑 `astropy__astropy-12907` 一条，
   确认 iCAP 提交 → 进程内跑 → 报告回写全链路通，再谈扩量。

---

### 已定结论（评审确认）
- [x] §5 依赖管理：requirements.txt 追加（不改 pyproject.toml）。
- [x] §9.4 `/api/agent-result` 详情接口推迟到前端阶段（v1 只留 `/api/agent-benchmarks`）。
- [x] §12 pause/resume：AgentCompass 原生怎么做就怎么做，iCAP 不额外干预。
