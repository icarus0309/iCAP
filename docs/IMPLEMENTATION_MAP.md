# 架构落实与替换点

原始说明书是平台能力与现有系统拓扑的介绍，并未给出后端路由契约、原仓库代码、服务凭证或真实数据格式。本工程依照四大业务域实现一个可以独立运行的版本，保留真实服务接入的边界。

| 说明书能力 | 页面 | FastAPI 路由 | 现状 |
| --- | --- | --- | --- |
| 模型广场与选型 | `/models` | `/api/models` | 从 DeepSeek 读取目录，支持自定义模型注册、编辑与多端点轮询 |
| 榜单与推荐 | `/leaderboard` | `/api/recommendations`、`/api/leaderboard` | 基于本地登记的评分数据；DeepSeek 目录信息不包含虚构分数 |
| 部署审批 | `/deployments` | `/api/deployments` | 申请与审批持久化；不启动集群任务 |
| 运营看板 | `/`、`/operations` | `/api/dashboard`、`/api/monitor`、`/ws/monitor` | 本地统计真实，设备指标模拟 |
| 数据集与下载 | `/datasets` | `/api/datasets`、`/api/datasets/{id}/files` | 上传、下载可用；不解析样本结构 |
| 标注与治理 | `/data-tools` | `/api/annotations`、`/api/data-governance` | 标注可保存；VLA 外部统计待接入 |
| 单/多模型评测 | `/evaluations` | `/api/evaluations`、`/api/evaluations/batch` | 任务生命周期、SSE 可用；执行器模拟 |
| 报告 | `/reports` | `/api/reports` | 自动生成文字摘要与发布状态；正式 Word/PDF 模板待接入 |
| 对话、论文、Agent 工具 | `/agents` | `/api/agents/*` | 聊天使用已注册模型的真实流式 API，支持双模型对战；论文和创作工具仍为示例 |
| 架构学习、训练、推理 | `/school` | `/api/school/*` | 学习视图、任务日志可用；训练与推理模拟 |

## 接入现有微服务

1. 将 `backend/app/main.py` 中的 `advance_jobs` 替换成实际任务调度器与持久化队列，任务状态与日志字段可沿用现有 API 响应结构。生产部署建议使用数据库和单独 worker。
2. 将评测类型 `Ruler1/Ruler2/VLM/AISF/Dev/ASR/MT/TTS` 映射到现有测评脚本或服务，不要将示例模型分数作为真实评价。
3. 在论文、Deep Research、PPT、翻译和下载接口中加入固定目标的服务适配器；不要将用户输入直接拼成可访问任意内网地址的代理 URL。
4. 模型注册的端点 URL、API Key、思考 Schema 与采样参数用于聊天调用；同一模型的多个端点在单进程内依次轮询，回答以 SSE 流式转发。
5. `DEEPSEEK_API_KEY` 可配置在仓库根目录 `.env`，用于 `/api/models` 获取该账号当前可用的 DeepSeek 模型目录和真实对话；密钥只由后端读取。
6. AI School 的脚本当前只作为文本记录。若接入真实执行，须使用隔离容器或专门的任务服务、资源配额、权限和审计，不能在 API 进程中直接执行。

## 实施约束

- 说明书中提及的 53 个路由、12 个 Pinia Store、4 个 Vuex 模块属于原平台规模描述，不是提供的页面和接口清单。本工程按业务流程组织 11 个主要页面与 Pinia 登录态；没有人为制造空白路由。
- 说明书给出的内网地址只是历史拓扑参考。本工程默认不访问它们。统一入口为 8081，开发时 Vite 代理 API 与 WebSocket。
- 演示模式账号仅用于本地体验；生产设置 `DEMO_MODE=false` 和必需环境变量，并通过反向代理提供 HTTPS。
