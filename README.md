# AutoFlow

AutoFlow 是一个带人工交付门禁的多 Agent 开发工作流。输入 MT 需求和 Git 仓库后，系统在独立 worktree 中依次执行：

`Product -> Planner -> Reader -> Design -> Coder -> Build -> Test -> Review -> Acceptance -> Delivery`

Product Manager 先把原始需求细化成可测试规格，Requirement Analyst 独立打分并触发有限轮次的需求纠偏。构建或测试失败时，日志会回送给 Coding Agent；开发完成后 Product Acceptance 根据规格、diff、测试证据和 Review 结论验收，不达标则重新下发量化修正指标。最终产出产品规格、需求评分、代码阅读、计划、设计、diff、验证、审查、验收、Commit 和 MR 描述。

Planner 同时生成构建、测试和功能运行命令。工作流会实际运行功能并采集输出，最终生成 Delivery Manifest。远程用户可以下载 ZIP，其中包含变更文件、`DELIVERY.md`、实际运行输出和 `changes.diff`，无需访问服务器 worktree。

创建任务时默认开启“包含本地未提交代码”。系统会把源仓库中已暂存、未暂存和未跟踪且未被忽略的文件复制到隔离 worktree，并建立内部基线 Commit。Reader 和 Coder 因此可以在用户半成品代码上继续开发；源工作目录、暂存区和分支不会被修改，最终 diff 只包含 AI 在基线之后产生的变更。

默认同时开启“完成后同步到本地仓库”。AI 仍在隔离 worktree 中完成构建、测试和产品验收；验收通过后，系统只把 AI 产生的补丁应用到用户原仓库工作区，修改会直接出现在 IDE 中，并且不会执行 `git add` 或改变用户暂存区。如果工作流期间用户修改了冲突位置或切换了 HEAD，同步会停止并保留隔离产物。

## Local Runner

当 Web 后台与用户代码不在同一台机器时，在用户电脑安装 `autoflow-runner`。后台只保存任务、事件和文本产物；Reader、AI Client、Git、构建、测试以及本地文件同步都在 Runner 所在电脑执行。

项目 Owner 在控制台或 API 中为每台 Runner 创建独立令牌。令牌只显示一次，服务端只保存哈希：

```http
POST /api/projects/{project_id}/runner-tokens
{"runner_id":"zhang-laptop","label":"Zhang Laptop"}
```

生产环境必须通过 HTTPS 暴露后台。然后在用户电脑安装兼容版本，并配置 AI Provider Key 和该 Runner 自己的 Token：

```bash
cd /path/to/AutomatedWorkflow
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

export AUTOFLOW_RUNNER_TOKEN='服务端分配的令牌'
autoflow-runner \
  --server https://autoflow.example.com \
  --id zhang-laptop \
  --name 'Zhang Laptop' \
  --root /home/zhang/projects
```

Runner 注册上线后，新建任务的“执行节点”中会出现该电脑。仓库路径填写用户电脑上的绝对路径；后台通过 WebSocket 长连接下发任务，Runner 将进度、日志、终端输出、文件变更和任务终态实时回传。Runner 只允许访问 `--root` 指定的目录。

旧的全局 `AUTOFLOW_RUNNER_TOKEN` 仅用于迁移兼容。新部署应为每台 Runner 签发独立令牌，泄露时可以只撤销对应 Runner，不影响其他开发电脑。

Server 会拒绝低于 `AUTOFLOW_RUNNER_MIN_VERSION` 的 Runner，并在注册响应中提示推荐版本。Runner 可以检查或下载经过签名的发布包：

```bash
autoflow-runner --check-update
autoflow-runner --download-update
```

更新清单使用 Ed25519 签名，下载包还会校验 SHA-256。下载命令不会静默安装新版本；管理员检查后再替换 Runner。Tag Release 工作流会构建 wheel、生成签名清单并发布到 GitHub Release，生产环境需配置 `RUNNER_RELEASE_PRIVATE_KEY` Secret，并把对应公钥和清单 URL 下发给 Runner。

## 登录与项目权限

AutoFlow 只使用邮箱验证码登录，不提供密码登录或密码注册。首次启动前设置管理员邮箱、验证码 HMAC 密钥和 SMTP；浏览器使用可撤销的 HttpOnly 会话 Cookie：

```dotenv
AUTOFLOW_AUTH_ENABLED=true
AUTOFLOW_BOOTSTRAP_ADMIN_EMAIL=admin@example.com
AUTOFLOW_EMAIL_CODE_SECRET=使用密码生成器创建的长随机密钥
AUTOFLOW_SMTP_HOST=smtp.example.com
AUTOFLOW_SMTP_PORT=465
AUTOFLOW_SMTP_SECURITY=ssl
AUTOFLOW_SMTP_USERNAME=autoflow@example.com
AUTOFLOW_SMTP_PASSWORD=SMTP授权码
AUTOFLOW_SMTP_FROM_EMAIL=autoflow@example.com
AUTOFLOW_AUTH_COOKIE_SECURE=true
```

验证码为 6 位数字，默认 10 分钟过期、60 秒内禁止同邮箱重复发送、最多尝试 5 次；同一来源 IP 默认 10 分钟最多发送 20 封。数据库只保存绑定邮箱与挑战 ID 的 HMAC。生产环境必须启用 HTTPS 并设置 `AUTOFLOW_AUTH_COOKIE_SECURE=true`。项目角色为 `owner`、`editor`、`viewer`：Owner 管理成员和 Runner Token，Editor 可以创建和操作任务，Viewer 只能查看任务和交付结果。

邮箱首次验证成功会自动创建普通用户，初始没有项目权限，可以自行创建项目成为 Owner，或由已有项目 Owner 按邮箱添加成员。私有部署可设置 `AUTOFLOW_REGISTRATION_ENABLED=false`，此时只有已经存在的用户会收到验证码，未知邮箱得到相同的通用响应以避免账号枚举。

Runner 遇到超出普通执行范围的操作时会发送权限请求并暂停任务。任务详情页向 Owner 显示“批准授权”和“拒绝授权”；决定持久化到数据库，并在 Runner 断线重连后继续下发。拒绝授权会终止对应任务，审批记录会保留用于审计。

## Git 平台集成

项目 Owner 可以配置 GitHub 或 GitLab 集成。生产环境先生成独立的 Fernet 密钥；平台 Token 加密后存入数据库，不会写入任务日志、Git remote 或命令参数：

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
export AUTOFLOW_CREDENTIAL_ENCRYPTION_KEY='生成的密钥'
```

```http
PUT /api/projects/{project_id}/git-integration
{"provider":"github","base_url":"https://api.github.com","repository":"owner/repository","token":"平台访问令牌"}
```

GitLab 的 `base_url` 使用实例 API 地址，例如 `https://gitlab.example.com/api/v4`。产品验收通过后，Editor 可以在任务详情页创建 Pull Request 或 Merge Request。发布前会校验本地 HTTPS remote 与配置的仓库和平台主机完全匹配；Token 仅通过临时 `GIT_ASKPASS` 传给 Git。当前远程 Runner 任务必须在 Runner 主机侧发布，Server 不会访问用户电脑上的 worktree。

Runner 协议使用统一 JSON Envelope：`id / type / timestamp / seq / runnerId / taskId / payload`。客户端持久化递增序号、最后处理的 Server 序号和未 ACK 消息；断线后发送 `Reconnect` 并重放未同步消息。支持 `Register`、`Heartbeat`、`Capability`、`TaskAssign`、`TaskProgress`、`TaskLog`、`TerminalOutput`、`AIChunk`、`FileChanged`、`PermissionRequest`、`CancelTask` 和任务终态等消息。

开发环境可以使用 `http://127.0.0.1:8765`；不要通过公网 HTTP 发送 Runner Token。

## 任务沙箱

构建、测试和功能运行默认在一次性 Docker 容器中执行。先在每台执行任务的 Server 或 Local Runner 主机上构建基础镜像：

```bash
docker build -t autoflow-sandbox:latest sandbox/
```

沙箱只把当前任务 worktree 以读写方式挂载到 `/workspace`，容器根文件系统只读，默认禁用网络，并启用非 root 用户、`no-new-privileges`、能力删除、CPU、内存和 PID 限制。容器在命令完成后自动删除，超时后会被强制清理。

只有完全可信的本地开发环境才能显式关闭容器隔离：

```dotenv
AUTOFLOW_SANDBOX_MODE=host
```

`host` 模式会直接继承 Runner 进程的本机权限和环境，不能用于外部用户任务。需要下载依赖的任务应使用预装依赖的自定义镜像；不要直接为所有任务开放网络。

## 持久化任务队列

任务启动只写入数据库 Job，不再依赖 API 进程内的临时线程队列。Server Worker 和 Local Runner 都通过原子租约领取任务；执行期间按租约时长的三分之一续租。Worker 或 Runner 失联后，过期租约会重新排队，并按指数退避自动重试。

每次进入工作流阶段都会更新 Job checkpoint。重试或服务重启后复用原 worktree，并从已有的产品规格、计划、阅读、设计和代码 Artifact 继续；源仓库补丁同步结果也会单独持久化，避免重复应用。

## 数据库迁移与维护

本地开发默认使用 SQLite；生产部署设置 PostgreSQL URL。服务启动时自动执行 Alembic `upgrade head`，现有 SQLite V1 数据库会由初始迁移原位接管：

```dotenv
AUTOFLOW_DATABASE_URL=postgresql+psycopg://autoflow:password@postgres:5432/autoflow
```

手动迁移和检查当前版本：

```bash
alembic upgrade head
alembic current
```

在线备份不会在命令行暴露 PostgreSQL 密码。SQLite 使用 Backup API，PostgreSQL 使用 `pg_dump --format=custom`：

```bash
autoflow-maintenance backup ./backups/autoflow.dump
autoflow-maintenance cleanup
```

清理任务仅删除过期会话、协议审计消息，以及终态任务的中间事件、Artifact 和受控 worktree；`delivery`、Commit 和 MR 描述会保留。Compose 生产模板默认启用 PostgreSQL 持久卷和健康检查，可通过 `docker compose --profile maintenance run --rm maintenance` 创建备份。

## 可观测性与告警

服务日志使用单行 JSON，HTTP 请求自动携带或生成 `X-Request-ID`，并在日志中关联 OpenTelemetry `trace_id`。`GET /api/ready` 同时检查数据库和持久化 Worker；`GET /metrics` 导出 HTTP 延迟、状态码、队列深度、任务最终失败和 Runner WebSocket 连接指标。

生产环境应保护指标端点并配置 OTLP Collector：

```dotenv
AUTOFLOW_METRICS_TOKEN=单独生成的监控令牌
AUTOFLOW_OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces
AUTOFLOW_ALERT_WEBHOOK_URL=https://alert-gateway.example.com/hooks/autoflow
```

当任务耗尽持久化重试次数时，告警由独立后台线程发送，不阻塞任务 Worker。Prometheus 规则位于 `deploy/prometheus-alerts.yml`，覆盖最终失败、HTTP 5xx 比例和队列积压。使用内置模板启动 Prometheus：

```bash
docker compose --profile observability up -d prometheus
```

## 快速启动

需要 Python 3.11+、Git 和 ripgrep。

```bash
cd /home/zhangkunjie/AutomatedWorkflow
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
# 编辑 .env，设置 OPENAI_API_KEY 和允许访问的仓库根目录
autoflow --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765>。API 文档位于 <http://127.0.0.1:8765/docs>。

不调用模型的本地演练：

```bash
AUTOFLOW_MOCK_LLM=true autoflow --port 8765
```

使用 ChatGPT 计划内的 Codex 额度（包括可用的 Free 额度）：

```bash
codex login --device-auth
```

完成浏览器登录后，在 `.env` 中设置：

```dotenv
AUTOFLOW_PROVIDER=codex_cli
AUTOFLOW_CODEX_MODEL=gpt-5.6-luna
AUTOFLOW_MOCK_LLM=false
```

`codex_cli` 模式会为每个 Agent 启动临时、只读的 Codex CLI 会话，并强制使用官方 OpenAI provider。ChatGPT 计划额度与 Platform API 额度相互独立；`openai` 模式仍需要有余额的 Platform API Key。

使用 DeepSeek API：

```dotenv
DEEPSEEK_API_KEY=你的密钥
AUTOFLOW_PROVIDER=deepseek
AUTOFLOW_DEEPSEEK_MODEL=deepseek-v4-flash
AUTOFLOW_DEEPSEEK_CODING_MODEL=deepseek-v4-pro
AUTOFLOW_MOCK_LLM=false
```

DeepSeek 模式通过其 OpenAI 兼容的 Chat Completions API 工作。规划、设计和审查使用 Flash，编码与错误修复使用开启思考模式的 Pro。旧名称 `deepseek-chat` 和 `deepseek-reasoner` 将于 2026-07-24 弃用，因此默认配置直接使用 V4 模型名。

## 角色级模型路由

每个 Agent 都可以独立配置 Provider 和 Model。未配置的角色继承 `AUTOFLOW_PROVIDER`。

```dotenv
ARK_API_KEY=你的火山方舟密钥
QWEN_API_KEY=你的阿里云百炼密钥
AUTOFLOW_DOUBAO_MODEL=你的模型 ID 或推理接入点 ID
AUTOFLOW_QWEN_MODEL=qwen3.7-plus
AUTOFLOW_QWEN_CODING_MODEL=qwen3-coder-plus

AUTOFLOW_AGENT_PRODUCT_PROVIDER=doubao
AUTOFLOW_AGENT_PRODUCT_MODEL=doubao-seed-2-1-pro-260628
AUTOFLOW_AGENT_READER_PROVIDER=doubao
AUTOFLOW_AGENT_READER_MODEL=doubao-seed-2-1-turbo-260628

AUTOFLOW_AGENT_PLANNER_PROVIDER=qwen
AUTOFLOW_AGENT_PLANNER_MODEL=qwen3.7-plus
AUTOFLOW_AGENT_ARCHITECTURE_PROVIDER=qwen
AUTOFLOW_AGENT_ARCHITECTURE_MODEL=qwen3.7-plus
AUTOFLOW_AGENT_CODER_PROVIDER=qwen
AUTOFLOW_AGENT_CODER_MODEL=qwen3-coder-plus
AUTOFLOW_AGENT_REVIEWER_PROVIDER=qwen
AUTOFLOW_AGENT_REVIEWER_MODEL=qwen3.7-plus
AUTOFLOW_AGENT_ACCEPTANCE_PROVIDER=doubao
AUTOFLOW_AGENT_ACCEPTANCE_MODEL=doubao-seed-2-1-pro-260628
```

Coder 也可设置为 `openai`、`deepseek` 或 `codex_cli`。OpenAI API Key 必须有 Platform 额度；Codex CLI 则使用本机已经完成的 ChatGPT 登录态。火山方舟和百炼均使用 OpenAI 兼容 Chat API。模型出现在目录中不代表账号已开通，配置前必须在对应控制台激活服务；豆包也可以填写已创建的推理接入点 ID。

## 运行边界

- 每个任务从目标仓库的当前 `HEAD` 创建独立 Git worktree 和功能分支。
- 开启“包含本地未提交代码”后，本地修改会成为隔离分支的内部基线；未跟踪符号链接、受保护目录和超过下载大小上限的快照会被拒绝。
- 开启“完成后同步到本地仓库”后，只有产品验收通过的 AI 补丁会写回原仓库；ZIP 下载退化为冲突或验收失败时的兜底方式。
- Reader 只读取 Planner 指定路径、需求关键词命中项以及根目录工程说明，受 `AUTOFLOW_MAX_CONTEXT_CHARS` 限制。
- Coder 只能写仓库内的文本路径，无法写 `.git`、构建目录或仓库外文件。
- 构建/测试命令来自任务输入或工程类型探测；危险命令、网络下载、`git push` 等会被拒绝。
- 默认不提交或推送代码。任务完成后可在控制台确认、创建本地 Commit；配置 Git 平台后，验收通过的 Server 本地任务还可由 Editor 显式创建远程 PR/MR。
- 建议将平台运行在专用开发容器或低权限系统用户中。命令过滤不是容器级安全边界。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 空 | OpenAI API 密钥 |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek Platform API 密钥 |
| `ARK_API_KEY` | 空 | 火山方舟/豆包 API 密钥 |
| `QWEN_API_KEY` | 空 | 阿里云百炼/千问 API 密钥 |
| `AUTOFLOW_PROVIDER` | `openai` | 全局回退：`openai`、`deepseek`、`doubao`、`qwen` 或 `codex_cli` |
| `AUTOFLOW_MODEL` | `gpt-5.6` | Responses API 模型 |
| `AUTOFLOW_DEEPSEEK_MODEL` | `deepseek-v4-flash` | 规划、设计和审查模型 |
| `AUTOFLOW_DEEPSEEK_CODING_MODEL` | `deepseek-v4-pro` | 编码与自动修复模型 |
| `AUTOFLOW_CODEX_MODEL` | `gpt-5.6-luna` | Codex CLI 模型，适合额度有限的轻量任务 |
| `AUTOFLOW_DOUBAO_MODEL` | 空 | 豆包模型 ID 或推理接入点 ID |
| `AUTOFLOW_QWEN_MODEL` | `qwen3.7-plus` | 千问通用规划与审查模型 |
| `AUTOFLOW_QWEN_CODING_MODEL` | `qwen3-coder-plus` | 千问编码与修复模型 |
| `AUTOFLOW_AGENT_<ROLE>_PROVIDER` | 空 | 指定角色 Provider；角色见上方示例 |
| `AUTOFLOW_AGENT_<ROLE>_MODEL` | 空 | 指定角色 Model，优先于 Provider 默认模型 |
| `AUTOFLOW_REASONING_EFFORT` | `medium` | 推理强度 |
| `AUTOFLOW_ALLOWED_ROOTS` | 用户主目录 | 逗号分隔的仓库白名单根目录 |
| `AUTOFLOW_DATABASE_PATH` | `./data/autoflow.db` | SQLite 文件 |
| `AUTOFLOW_DATABASE_URL` | 空 | 生产 PostgreSQL URL；设置后优先于 SQLite 路径 |
| `AUTOFLOW_RUNNER_TOKEN` | 空 | 当前 Local Runner 自己的 Bearer Token；全局共享值仅用于旧部署迁移 |
| `AUTOFLOW_RUNNER_OFFLINE_SECONDS` | `30` | 超过该心跳间隔后标记 Runner 离线 |
| `AUTOFLOW_RUNNER_MIN_VERSION` | `1.1.0` | Server 接受的最低 Runner 版本 |
| `AUTOFLOW_RUNNER_RECOMMENDED_VERSION` | `1.1.0` | Server 向 Runner 提示的推荐版本 |
| `AUTOFLOW_RUNNER_RELEASE_MANIFEST_URL` | 空 | Ed25519 签名的 Runner 发布清单 URL |
| `AUTOFLOW_RUNNER_RELEASE_PUBLIC_KEY` | 空 | Base64 编码的 Ed25519 发布公钥 |
| `AUTOFLOW_AUTH_ENABLED` | `true` | 启用用户登录和项目权限 |
| `AUTOFLOW_REGISTRATION_ENABLED` | `true` | 验证未知邮箱后自动创建普通用户 |
| `AUTOFLOW_AUTH_COOKIE_SECURE` | `false` | 生产 HTTPS 环境必须设为 `true` |
| `AUTOFLOW_AUTH_SESSION_HOURS` | `24` | 登录会话有效期 |
| `AUTOFLOW_EMAIL_CODE_SECRET` | 空 | 验证码 HMAC 密钥；生产必须设置 |
| `AUTOFLOW_EMAIL_CODE_TTL_SECONDS` | `600` | 邮箱验证码有效期 |
| `AUTOFLOW_EMAIL_CODE_COOLDOWN_SECONDS` | `60` | 同一邮箱重新发送冷却时间 |
| `AUTOFLOW_EMAIL_CODE_MAX_ATTEMPTS` | `5` | 单个验证码最大尝试次数 |
| `AUTOFLOW_EMAIL_CODE_IP_WINDOW_SECONDS` | `600` | 来源 IP 发送限额统计窗口 |
| `AUTOFLOW_EMAIL_CODE_IP_MAX_REQUESTS` | `20` | 单个来源 IP 在窗口内最多发送数 |
| `AUTOFLOW_SMTP_HOST` | 空 | SMTP 服务器地址 |
| `AUTOFLOW_SMTP_PORT` | `465` | SMTP 端口 |
| `AUTOFLOW_SMTP_SECURITY` | `ssl` | `ssl` 或 `starttls` |
| `AUTOFLOW_SMTP_USERNAME` | 空 | SMTP 登录账号；无认证中继可留空 |
| `AUTOFLOW_SMTP_PASSWORD` | 空 | SMTP 密码或授权码 |
| `AUTOFLOW_SMTP_FROM_EMAIL` | 空 | 验证码发件地址 |
| `AUTOFLOW_BOOTSTRAP_ADMIN_EMAIL` | 空 | 空数据库首次启动时创建的管理员邮箱 |
| `AUTOFLOW_MAX_FIX_ATTEMPTS` | `3` | 构建/测试自动修复次数 |
| `AUTOFLOW_MAX_PRODUCT_ITERATIONS` | `3` | 需求规格自动对齐轮数上限 |
| `AUTOFLOW_MAX_ACCEPTANCE_ITERATIONS` | `2` | 产品验收纠偏轮数上限 |
| `AUTOFLOW_PRODUCT_QUALITY_THRESHOLD` | `85` | 需求和验收通过分数 |
| `AUTOFLOW_WORKER_CONCURRENCY` | `2` | 当前服务实例的持久化 Worker 数量 |
| `AUTOFLOW_JOB_LEASE_SECONDS` | `60` | Job 租约有效期 |
| `AUTOFLOW_JOB_MAX_ATTEMPTS` | `3` | 基础设施失败时的最大任务尝试次数 |
| `AUTOFLOW_JOB_RETRY_BASE_SECONDS` | `5` | 指数退避的基础秒数 |
| `AUTOFLOW_SESSION_CLEANUP_HOURS` | `24` | 已过期或撤销会话的清理宽限期 |
| `AUTOFLOW_MESSAGE_RETENTION_DAYS` | `14` | Runner 协议审计消息保留天数 |
| `AUTOFLOW_EXECUTION_RETENTION_DAYS` | `90` | 终态任务中间执行数据保留天数 |
| `AUTOFLOW_WORKTREE_RETENTION_DAYS` | `30` | 终态任务 worktree 保留天数 |
| `AUTOFLOW_LOG_LEVEL` | `INFO` | JSON 日志级别 |
| `AUTOFLOW_METRICS_TOKEN` | 空 | `/metrics` Bearer Token |
| `AUTOFLOW_OTEL_SERVICE_NAME` | `autoflow` | OpenTelemetry 服务名 |
| `AUTOFLOW_OTEL_EXPORTER_OTLP_ENDPOINT` | 空 | OTLP HTTP Trace 上报地址 |
| `AUTOFLOW_ALERT_WEBHOOK_URL` | 空 | 最终失败异步告警地址 |
| `AUTOFLOW_CREDENTIAL_ENCRYPTION_KEY` | 空 | 加密 Git 平台 Token 的 Fernet 密钥 |
| `AUTOFLOW_MAX_CONTEXT_CHARS` | `80000` | 单阶段代码上下文字符上限 |
| `AUTOFLOW_MAX_DOWNLOAD_BYTES` | `104857600` | 交付 ZIP 内文件总大小上限 |
| `AUTOFLOW_SANDBOX_MODE` | `docker` | 命令执行模式：生产使用 `docker`，可信开发可显式使用 `host` |
| `AUTOFLOW_SANDBOX_IMAGE` | `autoflow-sandbox:latest` | 每任务执行镜像 |
| `AUTOFLOW_SANDBOX_NETWORK` | `none` | Docker 网络模式 |
| `AUTOFLOW_SANDBOX_MEMORY` | `4g` | 单命令容器内存上限 |
| `AUTOFLOW_SANDBOX_CPUS` | `4` | 单命令容器 CPU 上限 |
| `AUTOFLOW_SANDBOX_PIDS_LIMIT` | `512` | 单命令容器进程数上限 |
| `AUTOFLOW_MOCK_LLM` | `false` | 使用确定性 Mock Agent |

## API

- `POST /api/auth/email/request` 发送登录验证码
- `POST /api/auth/email/verify` 验证邮箱并创建登录会话
- `POST /api/tasks` 创建任务
- `POST /api/tasks/{id}/start` 启动工作流
- `GET /api/tasks/{id}` 获取任务、事件和产物
- `GET /api/tasks/{id}/download` 下载变更文件、运行说明和 diff 的 ZIP 包
- `GET /api/tasks/{id}/events` 订阅 SSE 事件
- `GET /api/runners` 查看已注册 Runner 及在线状态
- `WS /api/runner/ws` Local Runner 双向消息通道
- `PUT /api/projects/{id}/git-integration` 配置 GitHub/GitLab 凭据
- `POST /api/permissions/{id}/decision` 审批 Runner 权限请求
- `POST /api/tasks/{id}/cancel` 请求取消
- `POST /api/tasks/{id}/approve` 人工确认或创建本地 Commit
- `POST /api/tasks/{id}/publish` 创建 GitHub PR 或 GitLab MR

## 测试

```bash
pytest
```

## 后续演进

推荐按实际任务数据逐步加入：人工回答产品阻塞问题、按 Todo 并行 Coding Agent、Git 平台 OAuth、语言服务器索引、历史任务检索和仓库知识图谱。自动 push/merge 仍应保持显式人工门禁，并以真实任务评测集持续验证权限边界和交付质量。
