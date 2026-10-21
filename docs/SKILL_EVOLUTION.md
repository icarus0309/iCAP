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

Hermes 依赖管理员安装并显式设置 `SKILL_EVOLVE_HERMES_ENABLED=true`、`HERMES_BIN` 等平台运行环境。未具备环境时 UI 禁用该选项；不会模拟 Agent 或设备执行。目前生产部署使用单 API 模式。

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
