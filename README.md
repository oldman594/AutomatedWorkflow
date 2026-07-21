# AutoFlow

AutoFlow 是一个带人工交付门禁的多 Agent 开发工作流。输入 MT 需求和 Git 仓库后，系统在独立 worktree 中依次执行：

`Product -> Planner -> Reader -> Design -> Coder -> Build -> Test -> Review -> Acceptance -> Delivery`

Product Manager 先把原始需求细化成可测试规格，Requirement Analyst 独立打分并触发有限轮次的需求纠偏。构建或测试失败时，日志会回送给 Coding Agent；开发完成后 Product Acceptance 根据规格、diff、测试证据和 Review 结论验收，不达标则重新下发量化修正指标。最终产出产品规格、需求评分、代码阅读、计划、设计、diff、验证、审查、验收、Commit 和 MR 描述。

Planner 同时生成构建、测试和功能运行命令。工作流会实际运行功能并采集输出，最终生成 Delivery Manifest。远程用户可以下载 ZIP，其中包含变更文件、`DELIVERY.md`、实际运行输出和 `changes.diff`，无需访问服务器 worktree。

创建任务时默认开启“包含本地未提交代码”。系统会把源仓库中已暂存、未暂存和未跟踪且未被忽略的文件复制到隔离 worktree，并建立内部基线 Commit。Reader 和 Coder 因此可以在用户半成品代码上继续开发；源工作目录、暂存区和分支不会被修改，最终 diff 只包含 AI 在基线之后产生的变更。

默认同时开启“完成后同步到本地仓库”。AI 仍在隔离 worktree 中完成构建、测试和产品验收；验收通过后，系统只把 AI 产生的补丁应用到用户原仓库工作区，修改会直接出现在 IDE 中，并且不会执行 `git add` 或改变用户暂存区。如果工作流期间用户修改了冲突位置或切换了 HEAD，同步会停止并保留隔离产物。

## Local Runner

当 Web 后台与用户代码不在同一台机器时，在用户电脑安装 `autoflow-runner`。后台只保存任务、事件和文本产物；Reader、AI Client、Git、构建、测试以及本地文件同步都在 Runner 所在电脑执行。

服务端生成一个高强度共享令牌并写入 `.env`：

```dotenv
AUTOFLOW_RUNNER_TOKEN=使用密码生成器创建的随机令牌
```

生产环境必须通过 HTTPS 暴露后台。然后在用户电脑安装相同版本，并配置 AI Provider Key 和同一个 Runner Token：

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

Runner 协议使用统一 JSON Envelope：`id / type / timestamp / seq / runnerId / taskId / payload`。客户端持久化递增序号、最后处理的 Server 序号和未 ACK 消息；断线后发送 `Reconnect` 并重放未同步消息。支持 `Register`、`Heartbeat`、`Capability`、`TaskAssign`、`TaskProgress`、`TaskLog`、`TerminalOutput`、`AIChunk`、`FileChanged`、`PermissionRequest`、`CancelTask` 和任务终态等消息。

开发环境可以使用 `http://127.0.0.1:8765`；不要通过公网 HTTP 发送 Runner Token。V1 使用共享 Runner Token 和主机命令安全策略，能够上报 Docker 能力，但尚未把构建命令强制放入容器沙箱。

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
- 默认不提交代码。任务完成后可在控制台选择“确认完成”或“提交代码”。系统始终不 push、不创建远程 MR。
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
| `AUTOFLOW_RUNNER_TOKEN` | 空 | Local Runner 内部 API 的 Bearer Token |
| `AUTOFLOW_RUNNER_OFFLINE_SECONDS` | `30` | 超过该心跳间隔后标记 Runner 离线 |
| `AUTOFLOW_MAX_FIX_ATTEMPTS` | `3` | 构建/测试自动修复次数 |
| `AUTOFLOW_MAX_PRODUCT_ITERATIONS` | `3` | 需求规格自动对齐轮数上限 |
| `AUTOFLOW_MAX_ACCEPTANCE_ITERATIONS` | `2` | 产品验收纠偏轮数上限 |
| `AUTOFLOW_PRODUCT_QUALITY_THRESHOLD` | `85` | 需求和验收通过分数 |
| `AUTOFLOW_MAX_CONTEXT_CHARS` | `80000` | 单阶段代码上下文字符上限 |
| `AUTOFLOW_MAX_DOWNLOAD_BYTES` | `104857600` | 交付 ZIP 内文件总大小上限 |
| `AUTOFLOW_MOCK_LLM` | `false` | 使用确定性 Mock Agent |

## API

- `POST /api/tasks` 创建任务
- `POST /api/tasks/{id}/start` 启动工作流
- `GET /api/tasks/{id}` 获取任务、事件和产物
- `GET /api/tasks/{id}/download` 下载变更文件、运行说明和 diff 的 ZIP 包
- `GET /api/tasks/{id}/events` 订阅 SSE 事件
- `GET /api/runners` 查看已注册 Runner 及在线状态
- `WS /api/runner/ws` Local Runner 双向消息通道
- `POST /api/tasks/{id}/cancel` 请求取消
- `POST /api/tasks/{id}/approve` 人工确认或创建本地 Commit

## 测试

```bash
pytest
```

## 后续演进

推荐按实际任务数据逐步加入：人工回答产品阻塞问题、按 Todo 并行 Coding Agent、容器沙箱、GitLab/GitHub OAuth 与 MR API、语言服务器索引、历史任务检索和仓库知识图谱。不要在缺少评测集和权限隔离时直接开放自动 push/merge。
