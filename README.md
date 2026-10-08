# InnovationCore AI Platform

依据随附的《AI 创新平台 — 方案架构介绍》实现的可运行工程。前端为 Vue 3 SPA，后端为 FastAPI。原说明书是架构与宣传材料，没有提供 53 个页面、已有接口协议或真实业务服务的源码；本工程以四个业务域实现可操作的最小闭环，并在界面和接口中标明模拟能力。

## 快速启动

需要 Python 3.10+、Node.js 20.19+ 或 22.12+、npm。打开两个终端，在工程根目录执行：

**Linux / macOS，终端 1**

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload
```

**Windows PowerShell，终端 1**

```powershell
cd backend
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload
```

若 PowerShell 禁止激活脚本，可直接使用 `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`，再使用该解释器运行 `-m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload`。

**Linux 或 Windows，终端 2**

```bash
cd frontend
npm install
npm run dev
```

打开 http://localhost:5173 。演示账号 `admin`，密码 `demo1234`。Swagger 接口文档位于 http://localhost:8081/docs 。Vite 把 `/api` 和 `/ws` 代理到 8081，前端在 Windows 与 Linux 使用同一套代码。

工程根目录也提供一键启动脚本：Linux 执行 `bash start.sh`；Windows PowerShell 执行 `.\start.ps1`。脚本创建 Python 虚拟环境、安装依赖，并在前端尚未构建时运行 `npm ci && npm run build`，最后从同一端口提供前后端。交付包已附构建产物，因此通常只需安装 Python 依赖。默认访问 http://127.0.0.1:8081 。

## 功能与真实系统边界

| 业务域 | 已实现 | 当前模拟或适配点 |
| --- | --- | --- |
| MaaS | DeepSeek 实时模型目录、模型注册与多 API 端点管理、部署申请/审批 | 榜单与推荐仍依赖本地评分记录；实际模型部署审批不调用集群 |
| 数据与评测 | 数据集登记、文件上传下载、人工标注记录、治理统计、单模型/多模型评测创建、暂停/恢复/取消、SSE 进度日志、报告预览与发布状态 | 评分任务使用定时模拟执行器，未调用 Ruler1/2/VLM 真正评测脚本；VLA 统计待接入 |
| Agent | 注册模型的真实 SSE 对话、双模型对战、论文搜索与工具工作台 | 论文目录及辅助工具仍是示例结果 |
| AI School | 架构浏览、训练任务提交、日志/进度、推理沙箱、监控 WebSocket | 训练脚本只保存文本且**不执行**；推理和设备指标为演示数据 |

用户创建的模型配置（含 API Key）、任务、数据集、部署申请、报告存储在本地 `backend/data/platform.json`；上传的文件在 `backend/data/uploads/`。用户账号、密保答案摘要和可撤销会话存放在 `backend/data/auth.sqlite3`。这些路径已被 Git 忽略。单机单进程运行；JSON 业务存储与进程内执行器不适合多进程或生产集群。需集群部署时，应将 `Store` 换为数据库，并将评测/训练执行器换为队列和实际服务适配器。

### 用户注册与登录

注册时填写唯一用户名、中国大陆手机号、密码及三组不同的密保问题和答案。登录使用用户名和密码；用户名和手机号都不能重复注册。忘记或修改密码时，需正确回答至少两道已设置的密保问题。无需短信或邮件服务，也不会发送验证码。密码连续输错 5 次后，账号锁定 10 分钟；退出登录会使当前会话令牌失效。管理员账号继续使用原有用户名和密码登录。已有账号继续保留；如需使用密保找回密码，先在登录状态下设置三道密保问题。

创建模型、数据集、评测、部署申请、标注和实训任务时可选择“仅自己可见”或“公开给所有用户”，默认私有。公开资源可供其他已登录用户查看和使用，只有创建者及管理员能修改或删除；全局审批和报告发布仅限管理员。旧数据默认公开，由管理员管理。关联资源仍需各自可见，例如公开评测若引用私有数据集，不会向无权查看该数据集的用户展示。

密保答案仅以加盐摘要保存在本机数据库中。密保方式无法验证用户是否真正持有该手机号；注册时手机号用于账户识别和唯一性约束。若回答遗失且未登录，系统无法自行找回密码，需联系管理员处理。公开服务还应避免使用容易从社交资料猜到的答案。

## 环境变量

可参考 `backend/.env.example`，将实际配置放入仓库根目录 `.env` 或系统环境变量。后端启动时自动读取根目录 `.env`。`DEEPSEEK_API_KEY` 用于发现当前账号可用的 DeepSeek 模型并调用对话 API。开发默认开启 `DEMO_MODE=true`。生产应将其设为 `false`，并设置 `INNOVATION_ADMIN_USER`、`INNOVATION_ADMIN_PASSWORD`、`INNOVATION_SECRET_KEY`（至少 32 字符）、`CORS_ORIGINS`。认证采用签名 Bearer Token；HTTPS/TLS 在反向代理处配置。WebSocket 握手通过首条消息验证 Token，避免将凭证写进 URL。

模型广场可注册兼容 OpenAI Chat Completions 的服务。每个模型可配置多个 API 端点，调用时依次轮询。端点的思考 Schema 使用 `{"enabled":{"thinking":{"type":"enabled"}},"disabled":{"thinking":{"type":"disabled"}}}` 这样的 JSON；`enabled` 和 `disabled` 分别是开启和关闭思考时附加到请求体的字段。编辑模型时 API Key 输入框留空会沿用同一 URL 的已有密钥；模型 ID 注册后不可修改。

**单端口运行**：先在 `frontend` 执行 `npm run build`，然后在 `backend` 执行 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8081`，访问 http://localhost:8081 。FastAPI 自动托管已构建的前端，API 仍在 `/api`，WebSocket 在 `/ws`。此方式在 Windows 和 Linux 命令一致。Linux 生产也可由 Nginx 托管 `frontend/dist` 并反代 `/api`、`/ws` 到 8081；Windows 可用 IIS 或其他反向代理。跨机器访问时将 Uvicorn 的 `--host` 设为 `0.0.0.0`，配合防火墙、HTTPS 与认证使用。`VITE_API_BASE` 默认 `/api`，从子路径或跨域部署时需自行配置对应代理。

## 验证

```bash
cd backend
python -m pytest -q
cd ../frontend
npm run build
```

API 契约见 FastAPI `/docs`。源码入口分别为 `backend/app/main.py` 与 `frontend/src/main.js`。
