# Skill 自进化

入口：智能应用 → Skill 自进化，路由 `/skill-evolution`。

## 服务与引擎

- A / `opt`：上游 Outcome-Partitioned Tuning，成功/失败分组、有限结构化编辑、拒绝经验。
- B / `enhanced`：上游进阶引擎，结果优先路由、对照知识提炼、经验池、停滞探索。
- 支持生成初版、仅测评、迭代优化；默认三个模型角色复用后端平台专用连接，也可选择有权限的模型广场资源。模型端点在每次任务运行时解析，执行进程内平均轮转。
- 单 API 模式把 Skill 作为 system、用例消息作为后续 messages；统一按结果评估。A 的原三维权重显式设为 0/0/1。A 的 Rubric 适配器严格支持数值分数、issues 数组及审核结果，防止旧解析器将缺少 issues 的数值零分误判为成功。
- 普通文本输入统一转消息数组；A 的 Rubric 路径在每条用例中显式写入。不同引擎的内部状态和 checkpoint 不混用。
- `file` 判据仅接受上传的文本期望文件；单 API 模式比较模型最终文本，不读取用户指定的服务器实际文件路径。附件支持 md/txt/json/csv。
- B 的大模型仅在小模型结果未达 `result_threshold` 时调用。知识证据仍由上游逐字验证；未验证的知识不会进入经验池。Direct 探索每次最多三次顺序重写，重新评审全量有效用例。

上游基线和未修改的源码位于 `backend/vendor/skill_evolution`；平台适配在 `backend/app/skill_evolution`。每个任务启动独立 Python 子进程，避免两个 `src` 包和环境配置相互覆盖。

## API

所有接口使用现有 Bearer 登录会话。

| 方法 | 路径（前缀 `/api/skill-evolution`） | 用途 |
|---|---|---|
| GET | `/capabilities` | 引擎、可用模型角色、执行环境，不返回密钥 |
| GET / POST | `/jobs` | 权限过滤后的列表 / 预校验并创建任务 |
| GET | `/jobs/{id}` | 状态、最后 300 行日志、评分、逐例结果、最佳 Skill、生效配置 |
| POST | `/jobs/{id}/cancel` | 所有者或管理员取消排队/执行任务 |
| POST | `/jobs/{id}/resume` | 所有者或管理员从失败/中断/取消状态续跑 |
| GET | `/jobs/{id}/artifacts/{name}` | 权限校验后的产物下载，固定允许清单 |

任务参数定义在 `schemas.py`。前端按 2.5 秒轮询。默认单进程调度一个任务，同时允许每用户最多三个待处理任务。服务重启将 running 标记为 interrupted，可人工续跑；queued 继续排队。续跑不重置模型调用预算。任务最多运行六小时。多 worker 部署前须将 JSON 任务队列替换为跨进程调度器。

资源默认 private；public 允许所有登录用户查看输入、结果与日志，但仅所有者/管理员可取消、续跑。模型资源的访问权限在提交及实际执行时都检查。

## 后端私有配置

在 `INNOVATION_DATA_DIR/skill-evolution.env` 中配置（该目录已被 Git 忽略）：

```dotenv
SKILL_EVOLVE_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
SKILL_EVOLVE_MODEL=qwen3.8-27b
SKILL_EVOLVE_API_KEY=<由部署人员私下配置>
```

同名进程环境变量优先。生产文件 `/var/lib/icap/skill-evolution.env` 由 icap 用户持有，权限 0600。专用凭据不注册到模型广场、不返回前端、不进入任务记录和可下载产物。模型连接只通过子进程 stdin 临时传递，避免出现在命令行参数和落盘任务配置中。HTTP 错误不记录厂商响应体，输出和日志另做密钥脱敏。

### Hermes Agent 运行环境

Linux 服务配置 `SKILL_EVOLVE_HERMES_ENABLED=true`、`ICAP_HERMES_RUNTIME=/opt/icap-hermes`。该目录包含官方 Hermes v0.22.3 源码、独立 Python 3.14 环境及 `venv/bin/hermes`；安装验证通过后由管理员创建 `READY` 文件。目录由 root 管理，服务用户只能读取。未启用或未安装时前端禁用此选项。

- A、B 都支持 Hermes，B 的对照大模型也在独立用例环境运行；执行模型沿用页面选择和平均路由。
- 平台将消息数组同时渲染为 Hermes 的文本提问，并保留原消息供评审；CLI 使用官方支持的 `tool` 会话来源，每用例单独导出。`key_steps` 支持 `step_id`、`id` 或 `name` 标识（无标识时自动编号），统一转为非空字符串，避免上游忽略数字零或丢失别名。
- 通过任务内的回环 HTTP 转发器调用模型，真实厂商凭据只存在父工作进程。Hermes 的配置、环境、命令行及工具均不持有真实密钥。转发器仅开放该任务的执行/对照模型，任务结束即失效；全部推理请求计入同一预算。
- 使用 bubblewrap 隔离挂载和 PID：只读系统运行文件与 Hermes 运行时，仅当前用例目录可写。无服务器数据目录、其他任务、宿主 `/proc` 或管理配置访问权。Ubuntu 24.04 通过仅针对 `/usr/bin/bwrap` 的 AppArmor `userns` 规则允许命名空间，无需关闭全局限制。
- 提供 terminal、file 工具，最多 12 个 Agent 回合；单用例受页面超时限制，进程数、文件大小和 CPU 时间另有限制。取消或超时会清理 Agent 及工具子进程。未配置浏览器、联网搜索、Home Assistant 设备服务。
- 从 Hermes 持久化会话导出真实消息和工具调用，交由原引擎评审过程、简洁度和结果（默认权重 0.4/0.2/0.4）。无参考步骤时按实际步骤评分的平均值计算过程分，修正上游空权重映射恒得零分的问题。
- `file` 的 `actual_path` 只接受用例工作目录内的相对文本文件名，默认 `result.txt`；服务端评审拒绝路径/符号链接逃逸和超大文件。界面提供可直接使用的“载入工具示例”。
- 用于评审的期望文件和 Rubric 不复制到 Agent 工作目录，其他业务资源按原相对路径提供。`word` 判据使用 `{"whitelist":["必含词"],"blacklist":["禁用词"]}` 对象。

Hermes 安装来源：[官方仓库 v0.22.3](https://github.com/NousResearch/hermes-agent/tree/v0.22.3)、[官方安装说明](https://hermes-agent.nousresearch.com/docs/getting-started/installation/)。源代码未改动；平台适配集中在 `app/skill_evolution/hermes.py`。

## 产物

数据保存在 `INNOVATION_DATA_DIR/skill-evolution/{job_id}`。`optimized_skill.md` 是历史最佳，`final_skill.md` 是最后一轮版本；`report.json` 含轮次、逐用例反馈、经验和版本演进。下载 ZIP 只打包平台允许的报告与 Skill，不打包凭据或完整执行环境。原始上游产物留在 outputs/ 供后端诊断。

## 验证

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_skill_evolution.py -q
cd ../frontend
npm run build
```

集成测试通过本地 HTTP 模型运行真实上游编排器，覆盖 A/B 零分评审、两轮编辑、B 大模型分流、证据拒绝、停滞探索、预算失败、权限与路径限制、取消及续跑。
