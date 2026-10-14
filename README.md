# DevContext：配置、启动与使用

DevContext 从 Java 源码和 Markdown 文档中建立知识索引，结合代码符号关系和真实证据解释项目实现。当前目标项目是 `my12306`；外部代码和文档只读，索引、缓存及运行记录写入本项目或数据库。

## 1. 先确认使用哪个目录

本机有两个独立工作树，Python 环境与 `.env` 各自独立：

| 目录 | 当前功能 | 使用方式 |
|---|---|---|
| `D:\Java-learning\DevContext` | CLI / V5 Tool Agent + Web UI | 命令行搜索、问答、索引和评测 |
| `D:\Java-learning\DevContext-web-ui-v1` | CLI + Web UI V1，分支 `codex/web-ui-v1` | 浏览器聊天、历史会话、执行记录和证据查看 |

**本目录已整合 Web UI，CLI 和 Web 均可用。** 原 Web 工作树保留历史版本。详细说明见 [Web 接口与事件协议](docs/web/README.md)和[验收报告](docs/web/test-report.md)。

以下命令以 Windows PowerShell 为例。执行 Python 命令时应在实际使用的工作树根目录，确保读取正确的 `.env`。

## 2. 已经配置好：日常启动

已有 Python 环境、模型配置、数据库索引和前端构建产物时，启动 Web：

```powershell
cd D:\Java-learning\DevContext
docker compose up -d
uv run --extra web devcontext-web
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。保持服务终端运行，按 `Ctrl+C` 停止。已有服务占用 8000 时直接访问它，或按第 6 节更换端口。

只使用 CLI：

```powershell
cd D:\Java-learning\DevContext
docker compose up -d
uv run devcontext ask "OrderServiceImpl.createTicketOrder 主要负责什么？"
uv run devcontext ask "详细解释购票库存与订单的一致性如何保证。" --profile full
```

已有索引时，日常启动不用重复执行 `init-db` 或 `ingest`。CLI 单次命令执行后退出，不依赖后台 Web 服务；Web 工作树也支持这些 CLI 命令。

## 3. 首次安装与初始化

### 3.1 环境要求

| 环境 | 要求与用途 |
|---|---|
| Python + uv | Python 3.13，仓库锁定 3.13.15；bootstrap 可准备项目内 Python 和依赖 |
| Docker Desktop | 运行 PostgreSQL/pgvector；Docker Compose 可用，5432 端口空闲 |
| JDK + Maven | JDK 21+，Maven 在 PATH；首次索引与重建时分析 Java 源码及符号图 |
| curl | `curl.exe` 或 `curl` 在 PATH；文本模型和默认 Embedding 传输需要 |
| Node.js + npm | Node.js 22.12+ 或 24.x；仅 Web 前端构建、开发和测试需要 |

Docker 使用 `pgvector/pgvector:0.8.6-pg18-bookworm`，容器名 `devcontext-postgres`，映射本机 5432，数据保存在 Docker 命名卷。

### 3.2 准备环境和 .env

选择对应工作树进入，再运行：

```powershell
cd D:\Java-learning\DevContext
# 使用 Web 时，将上一行替换为：
# cd D:\Java-learning\DevContext

.\scripts\bootstrap.ps1
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

bootstrap 准备 uv、项目内 Python、`.venv` 和开发依赖。接着编辑 `.env`，按第 4 节配置来源路径、数据库和模型。已有 `.env` 请保留，示例文件不是本机生效配置。

CLI 工作树安装依赖：

```powershell
uv sync --locked --extra dev
```

Web 工作树安装依赖：

```powershell
uv sync --locked --extra web --extra dev
```

Windows 上 uv 若报“无法将文件移到不同磁盘”，使用 bootstrap 的项目内 Python 安装，或用 `uv sync --python '已有Python3.13的python.exe完整路径' --extra dev` 指定已有解释器；Web 再加 `--extra web`。

### 3.3 建立知识索引

确保配置的源码、文档目录均存在，已设置百炼密钥：

```powershell
docker compose up -d
docker compose ps
uv run devcontext init-db
uv run devcontext smoke-api
uv run devcontext ingest
```

- `init-db`：创建或补齐数据库扩展、表和索引。
- `smoke-api`：验证百炼 Embedding，不能代替文字模型连接验证。
- `ingest`：通过 JavaParser 解析源码及符号图、解析 Markdown、生成向量，再替换当前仓库的知识快照。Java 解析器会自动调用 Maven 构建。

入库要求代码、文档两类来源均非空。`ingest` 可能产生 Embedding 费用；内容未变化时可复用 `artifacts/embedding-cache.jsonl`，但仍是全量快照重建，不是增量索引。

验证索引和文字模型：

```powershell
uv run devcontext search --strategy keyword --query "createTicketOrder" --top-k 5
uv run devcontext ask "OrderServiceImpl.createTicketOrder 方法在哪个文件，主要负责什么？" --profile fast
```

源码、文档内容或来源路径变化后，应重新 ingest。仅切换文字模型无需重建索引；切换 Embedding 模型需要重建。当前数据库列为 `VECTOR(1024)`，不要只修改向量维度而忽略 schema。

## 4. 当前配置如何填写

### 4.1 配置读取与优先级

普通配置按 **进程环境变量 > 当前工作目录的 .env > 代码默认值** 读取。支持的 CLI 参数覆盖对应请求选项，例如 `--profile full`。

只有 `CHATGPT_API_KEY`、`OPENAI_BASE_URL` 另有 Windows 用户/系统环境变量兜底读取；其他密钥没有该额外逻辑。设置永久环境变量后通常需要重开终端。`.env` 已被 Git 忽略。

Web 的 `DEVCONTEXT_WEB_*` 直接读取启动进程环境，不通过 Settings 从 `.env` 加载；请用第 6 节的 `$env:` 设置并重启服务。

### 4.2 数据源与数据库

`.env` 示例，路径可使用正斜杠：

```dotenv
DEVCONTEXT_CODE_ROOT=D:/Java-learning/12306Project/12306/my12306
DEVCONTEXT_DOC_ROOT=D:/Java-learning/12306Project/docs
REPOSITORY_NAME=my12306

POSTGRES_DB=devcontext
POSTGRES_USER=devcontext
POSTGRES_PASSWORD=devcontext_dev
DATABASE_URL=postgresql://devcontext:devcontext_dev@localhost:5432/devcontext
```

| 配置 | 含义 |
|---|---|
| `DEVCONTEXT_CODE_ROOT` | 被分析的 Java 项目根目录，不是 DevContext 自己的目录 |
| `DEVCONTEXT_DOC_ROOT` | 被索引的 Markdown 文档目录 |
| `REPOSITORY_NAME` | 数据库中的仓库标识，默认 my12306；入库和查询需一致 |
| `POSTGRES_*` | Compose 首次创建数据库使用的名称、用户和密码 |
| `DATABASE_URL` | Python 实际连接地址，须与数据库凭据一致 |

上述密码是仓库自带的本地开发示例。更改 `POSTGRES_PASSWORD` 不会自动修改已有数据卷中的数据库用户密码；已有数据库需同步修改用户凭据和 `DATABASE_URL`。

### 4.3 Embedding：两种文字模型方案都需要

```dotenv
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSIONS=1024
EMBEDDING_TRANSPORT=curl
```

当前终端设置密钥：

```powershell
$env:DASHSCOPE_API_KEY = '替换为你的百炼密钥'
```

Embedding 用于入库、vector/hybrid 检索。keyword 搜索只读数据库。传输支持 curl、openai、auto，默认 curl；此处 openai 指 SDK 传输方式，不是文字模型供应商。

### 4.4 文字模型 A：DeepSeek

`.env`：

```dotenv
LLM_PROVIDER=deepseek
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

当前终端：

```powershell
$env:DEEPSEEK_API_KEY = '替换为你的 DeepSeek 密钥'
```

可选角色模型为 `DEEPSEEK_PLANNER_MODEL`、`DEEPSEEK_ANSWER_PLANNER_MODEL`、`DEEPSEEK_ANSWER_MODEL`、`DEEPSEEK_REVIEWER_MODEL`；不设置时沿用 `DEEPSEEK_MODEL`。

### 4.5 文字模型 B：OpenAI-compatible 网关

`.env`：

```dotenv
LLM_PROVIDER=openai
OPENAI_MODEL=gpt-6.1-sol
OPENAI_CONTEXT_WINDOW=131072
OPENAI_MAX_OUTPUT_TOKENS=32768
```

当前终端：

```powershell
$env:CHATGPT_API_KEY = '替换为你的网关密钥'
$env:OPENAI_BASE_URL = 'https://你的网关地址/v1'
```

须同时配置密钥、兼容 API 地址和网关实际支持的模型；模型名、上下文窗口、输出上限以实际服务能力为准。这一路径使用 `OPENAI_MODEL`，不继承 DeepSeek 角色模型。

仅设置 CHATGPT_API_KEY 不会切换供应商，必须设置 `LLM_PROVIDER=openai`。调用失败不会自动换供应商。无论选择 A 或 B，百炼 Embedding 都独立配置。

以上密钥均为占位符。需要跨终端保存时，可用 `[Environment]::SetEnvironmentVariable('变量名', '你的值', 'User')`，再重开终端；也可以在本机 `.env` 填写对应变量。

### 4.6 回答策略、思考档位与超时

| 配置 | 默认值 | 含义 |
|---|---|---|
| `ANSWER_PROFILE` | fast | CLI 默认策略；Web 请求默认显式 fast，可在界面切 full |
| `ANSWER_REASONING_EFFORT` | 未设置 | Fast 默认 low、Full 默认 high；支持 low/medium/high |
| `ANSWER_HARD_TIMEOUT_SECONDS` | 未设置 | 整次请求截止，含检索；CLI 可用 --hard-timeout 覆盖 |
| `ANSWER_FAST_LATENCY_TARGET_SECONDS` | 45 | 观测目标，不强制中断或降档 |
| `ANSWER_FULL_LATENCY_TARGET_SECONDS` | 300 | 同上 |
| `ANSWER_PLANNER_TIMEOUT_SECONDS` | 180 | Fast 回答规划单次超时 |
| `ANSWER_WRITER_TIMEOUT_SECONDS` | 240 | Fast Writer 单次超时 |
| `ANSWER_FULL_PLANNER_TIMEOUT_SECONDS` | 300 | Full 回答规划单次超时 |
| `ANSWER_FULL_WRITER_TIMEOUT_SECONDS` | 600 | Full Writer 单次超时 |
| `LLM_SUPPORTED_REASONING_EFFORTS` | 未设置 | 声明未知模型实际支持的档位，例如 low,high |

思考设置主要影响回答 Planner/Writer，检索调用有独立策略。模型能力未知时省略参数；当前 deepseek-flash 内建声明 low/high，不把 medium 自动映射成 high。

Fast 使用 Single Stream，Full 使用 V3；按已验证章节发布，Full 可进行原有有限修复和正文 fallback。`--depth` 已移除，简要/详细要求直接写在问题里。

`TEACHING_GENERATION_MODE`、`OPENAI_V3_REASONING_EFFORT`、`TEACHING_REQUEST_TIMEOUT_SECONDS` 属于兼容路径；日常 Fast/Full 使用上表。内部 `ANSWER_ENGINE_ENABLED`、`ANSWER_REQUEST_POLICY` 由入口装配，不需手动设置。

### 4.7 Tool Agent 与 Symbol Graph

| 配置 | 代码默认 | 含义 |
|---|---|---|
| `TOOL_AGENT_ENABLED` | true | Agent 选择代码/文档检索、符号定位、Graph 工具 |
| `SYMBOL_GRAPH_ENABLED` | false | 固定检索的自动扩图开关，不限制 Agent 显式 Graph 工具 |
| `TOOL_AGENT_PLANNER_TIMEOUT_SECONDS` | 60 | 工具规划单次调用超时 |
| `SYMBOL_GRAPH_QUERY_TIMEOUT_SECONDS` | 2 | Graph 查询安全超时，最多 30 秒 |
| `SOURCE_POLICY_PATH` | 内置 config/source-policy.json | 文档来源角色、时间状态和权威策略 |

当前默认 **Tool Agent + Graph 工具 + 结构元数据检查**。旧 .env 中的 false 仍会覆盖默认值。回退到固定检索设置 TOOL_AGENT_ENABLED=false；再设 SYMBOL_GRAPH_ENABLED=false 则不自动扩图，结构 Coverage 检查仍保留。

## 5. CLI 使用示例

```powershell
# 搜索和查看上下文。
uv run devcontext search --strategy keyword --query "createTicketOrder" --top-k 5
uv run devcontext search --strategy hybrid --query "购票事务与幂等" --top-k 10
uv run devcontext context "订单创建与支付的代码关系" --top-k 10 --max-chars 8000

# 默认 teach + fast，或显式 Full。
uv run devcontext ask "OrderServiceImpl.createTicketOrder 如何处理重复请求？"
uv run devcontext ask "详细解释余票令牌桶的设计。" --profile full

# 调试与性能。
uv run devcontext ask "购票责任链如何执行？" --debug --perf
uv run devcontext ask "购票事务如何保证一致性？" --profile full --hard-timeout 300 --perf-json artifacts/full-request.json

# 只生成证据需求计划；仍会调用规划模型。
uv run devcontext ask "订单关闭如何实现？" --plan-only

# 查询符号。
uv run devcontext graph symbol OrderServiceImpl --json
```

context 在路由不明确时也可能调用分类模型。关系查询 callers/callees/implementations/hierarchy 要使用 symbol 结果中的完整 `symbol_key`，不要用简称代替。

| 兼容选项 | 用途 |
|---|---|
| `--answer-mode teach` | 默认教学回答，支持 Fast/Full |
| `--answer-mode explain` / `--answer-mode legacy` | 旧解释/模板式回答 |
| `--no-plan` | 旧版单查询链路 |
| `--teaching-generation-mode multi_pass` | 旧教学链路 |
| `--teaching-generation-mode single_stream` / `--teaching-generation-mode v3` | Fast/Full 兼容别名 |

`--profile`、`--reasoning-effort` 不与 explain、legacy、multi_pass、no-plan、plan-only 混用。执行 `uv run devcontext ask --help` 查看全部参数。debug/perf-json 会包含内部诊断，分享前检查内容。

## 6. Web UI 启动与使用

### 6.1 首次构建或更新前端后

```powershell
cd D:\Java-learning\DevContext
uv sync --locked --extra web --extra dev
cd frontend
npm ci
npm run build
cd ..
uv run --extra web devcontext-web
```

访问 [8000 端口页面](http://127.0.0.1:8000)，FastAPI 提供 frontend/dist。日常只启动后端即可，前端源码更新后重新 build。

左侧管理会话，中央提问，输入区选择 Fast/Full。Enter 发送、Shift+Enter 换行。引用查看器显示真实文件、行号、符号和内容；执行面板显示真实检索事件和最终摘要。

知识索引在 PostgreSQL；历史会话、答案、证据与执行记录在 `data/web/devcontext.sqlite3`。刷新或断网不会取消 Run；历史仅用于展示，不作为多轮 Agent Memory。

### 6.2 前端开发用两个终端

```powershell
# 终端一。
cd D:\Java-learning\DevContext
uv run --extra web devcontext-web
```

```powershell
# 终端二。
cd D:\Java-learning\DevContext\frontend
npm run dev
```

访问 [5173 开发页面](http://127.0.0.1:5173)，API 代理到 8000；更改后端端口时也要调整 frontend/vite.config.ts。

### 6.3 Web 专用参数

| 进程环境变量 | 默认 | 含义 |
|---|---|---|
| `DEVCONTEXT_WEB_HOST` | 127.0.0.1 | 本机监听 |
| `DEVCONTEXT_WEB_PORT` | 8000 | HTTP 端口 |
| `DEVCONTEXT_WEB_WORKERS` | 2 | Agent 线程/准入名额，1–16，不是 Uvicorn 进程数 |
| `DEVCONTEXT_WEB_DATABASE` | data/web/devcontext.sqlite3 | 会话文件，自定义建议用绝对路径 |

```powershell
$env:DEVCONTEXT_WEB_PORT = '8001'
uv run --extra web devcontext-web
```

V1 为本机单用户、单进程，没有账户隔离。同一会话一次一个 Run；重复提交去重。取消立即记录状态，调用返回或超时后才释放名额。核心证据不足、生成中断显示部分回答，重启不将未完成请求标为成功。

## 7. 常见问题

| 现象 | 检查 |
|---|---|
| 找不到 devcontext-web / 没有 web extra | 切到 DevContext-web-ui-v1，安装 --extra web |
| frontend_not_built / 页面 503 | Web 的 frontend 目录执行 npm ci、npm run build |
| 数据库连接失败 | docker compose ps、5432、实际密码与 DATABASE_URL |
| 修改 .env 仍用旧模型 | 同名 $env: 优先；确认工作目录，重启 Web |
| 配网关密钥却调用 DeepSeek | 检查 LLM_PROVIDER，密钥不会自动选择供应商 |
| keyword 正常，vector/hybrid 失败 | 检查百炼密钥、网络与维度；运行 smoke-api |
| ingest 找不到目录或没有文档 | 检查两个源目录，代码和 Markdown 都要非空 |
| Maven/Java 构建失败 | java -version、mvn -version、依赖下载 |
| 模型 401/403/402/404 | 密钥、权限/余额、地址与模型名，不会自动切换供应商 |
| Web 409/429 | 会话仍执行/全局名额已满，等待线程退出 |
| 取消后发送按钮未恢复 | 在途同步调用未返回，仍占用名额 |
| 检索到旧代码或旧文档 | 主动 ingest 重建，不是只修改目录配置 |

Windows 代理环境若访问百炼遇到 TLS/握手失败，可按实际网络设置绕过列表，例如：

```powershell
$env:NO_PROXY = 'localhost,127.0.0.1,::1,.aliyuncs.com'
$env:no_proxy = $env:NO_PROXY
```

有原列表时合并现有条目；curl 可能优先读取小写变量，避免大小写冲突。这只作用于当前终端及子进程，不要盲目关闭其他模型调用需要的代理。

## 8. 测试、评测与文件位置

CLI 工作树：

```powershell
uv sync --locked --extra dev
uv run --extra dev pytest
mvn -q -f java-parser/pom.xml test
```

Web 工作树：

```powershell
uv sync --locked --extra web --extra dev
uv run --extra web --extra dev pytest
# PostgreSQL 已准备好时开启集成测试。
$env:DEVCONTEXT_RUN_INTEGRATION = '1'
uv run --extra web --extra dev pytest
Remove-Item Env:DEVCONTEXT_RUN_INTEGRATION

cd frontend
npm test
npm run build
npx playwright install chromium
npm run test:e2e
```

Playwright 自动启动测试服务，8000/5173 端口须空闲。真实问答和 live 评测会调用模型；单元测试不代表全部业务问答质量。

评测入口有 evaluate、evaluate-retrieval-workflow、evaluate-tool-agent、evaluate-symbol-graph、evaluate-answers，使用各自 --help 查看参数。

| 文件/目录 | 用途 |
|---|---|
| .env.example / src/devcontext/config.py | 配置模板 / 字段默认值与校验 |
| scripts/bootstrap.ps1 | Windows 环境准备 |
| docker-compose.yml / sql/ | 数据库容器 / 知识索引 schema |
| java-parser/ | Java 解析与符号关系 |
| artifacts/ | 向量缓存、解析结果、诊断和评测报告 |
| docs/ | 架构、历史实现与评测 |
| Web 的 frontend/、data/web/ | 前端 / 持久化会话 |
| Web 的 docs/web/ | API、事件协议、审查和测试报告 |

进一步阅读：[文档目录](docs/README.md)、[架构决策](docs/architecture-decisions.md)、[V5 实现与验收](docs/V4-graph等功能规划/07-V5-agent执行流优化实现与验收.md)。历史文档对应当时版本，配置和启动以当前代码及本文为准。
