# DevContext Web UI V1

本机 Vue 3 + TypeScript 问答界面，通过 FastAPI + SSE 复用原有 Ask 链路。知识索引继续使用 PostgreSQL/pgvector；SQLite 保存会话、消息、执行状态、验证章节、证据与事件。每次问题独立执行，历史仅用于展示。

## 本地启动

需要 Python 3.13、uv、Node.js 22.12+ 或 24.x、npm，以及原项目的数据库、知识索引和模型配置。以下是本次独立工作树；其他克隆请替换目录。

```powershell
cd D:\Java-learning\DevContext-web-ui-v1
# 首次安装 Python 环境；已有 .venv 时可跳过。
.\scripts\bootstrap.ps1
uv sync --locked --extra web --extra dev

# 仅在没有 .env 时复制示例，然后填写源目录、数据库和模型配置。
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# 已有运行中的数据库与索引时，直接跳到前端构建。
docker compose up -d
uv run devcontext init-db
uv run devcontext smoke-api
# 仅首次索引或主动重建时执行，已有索引无需 ingest。
uv run devcontext ingest

cd frontend
npm ci
npm run build
cd ..
uv run --extra web devcontext-web
```

打开 [本地 Web UI](http://127.0.0.1:8000)。本次工作树的环境、配置与前端产物已经准备好，可直接运行最后一条启动命令。SQLite 首次启动自动建表。

前端开发用两个终端：

```powershell
# 终端一，仓库根目录
uv run --extra web devcontext-web
# 终端二
cd D:\Java-learning\DevContext-web-ui-v1\frontend
npm run dev
```

打开 [开发界面](http://127.0.0.1:5173)，Vite 代理 `/api` 和 `/health`。

## 配置与运行限制

模型和知识库仍由原 Settings 读取 `.env`。以下 Web 参数从启动进程的环境变量读取：

| 参数 | 默认 | 作用 |
|---|---|---|
| `DEVCONTEXT_WEB_HOST` | `127.0.0.1` | 监听地址 |
| `DEVCONTEXT_WEB_PORT` | `8000` | HTTP 端口 |
| `DEVCONTEXT_WEB_WORKERS` | `2` | Agent 线程与准入名额，范围 1–16 |
| `DEVCONTEXT_WEB_DATABASE` | 仓库根目录 `data/web/devcontext.sqlite3` | 会话数据库 |

`WORKERS` 指 Agent 线程。Uvicorn 始终一个进程；同一 SQLite 的第二实例拒绝启动。V1 默认本机单用户，无登录、访问隔离、动态知识库管理和多轮 Memory。Fast/Full 仅控制已有教学策略，Tool Agent、Graph、模型和预算继承现有配置。

同一会话仅一个占用名额的 Run，全局满返回 `429`，会话冲突返回 `409`；拒绝不会留下消息。刷新、切换会话和 SSE 断开不取消后台 Run。

停止立即保存 `cancelled` 并保留验证章节。在途同步调用等待现有超时或返回，线程退出前名额不释放，界面提示“正在停止”。应用关闭最多等待 30 秒；Python 进程仍可能等待在途线程的原有超时，不强杀线程。重启不自动重跑：遗留活动请求有章节则恢复 `partial`，否则 `failed`，原因 `service_interrupted`；既有终态保持。

## API 与事件

统一前缀 `/api/v1`；`GET /health` 只检查应用和 SQLite，不调用模型。

| 方法与路径 | 输入 / 返回 |
|---|---|
| `GET /sessions?limit=50&offset=0` | `{items}`，limit 最大 100 |
| `POST /sessions` | `{title?}`，创建会话 |
| `GET /sessions/{id}` | 会话、消息、Run 快照 |
| `PATCH /sessions/{id}` | `{title}`，修改标题 |
| `DELETE /sessions/{id}` | 删除关联数据；执行未退出时 `409` |
| `POST /sessions/{id}/runs` | `{query,profile?,client_request_id}`；返回 Run 与 `user_message`，HTTP 202 |
| `GET /runs/{id}` | 状态、章节、答案、证据、摘要、真实线程占用与事件序号 |
| `GET /runs/{id}/events?after=0` | SSE 回放并订阅 |
| `POST /runs/{id}/cancel` | 幂等取消并返回快照 |

问题必须非空、最多 10,000 字符，profile 仅 `fast/full`，默认 fast。请求 ID 在会话内唯一，长度 1–100，允许字母、数字、下划线、连字符，推荐 UUID。同 ID 同内容返回已有 Run；不同内容返回 `409 idempotency_conflict`。明确重新提问使用新 ID，网络重试使用原 ID。未知资源 `404`，无效输入 `422`，存储不可用 `503`；错误不包含 provider 原文。

事件为 `{run_id,seq,type,timestamp,data}`，seq 在 Run 内从 1 连续递增，时间为 UTC ISO 8601：

| type | data |
|---|---|
| `run.created/run.started` | 待启动 / 执行状态 |
| `retrieval.action_completed` | 实际策略、来源范围、结果数、耗时、轮次 |
| `retrieval.context_built` | 材料数、轮次、截断标记 |
| `evidence.updated` | `items` 真实证据快照，`replace` 指示替换 |
| `answer.section_upsert` | `section_id,position,markdown,citations` |
| `run.summary` | 最终白名单执行摘要 |
| `run.finished` | 终态、原因、安全错误、最终答案 |

SSE 使用 `id: seq`、`event: type`、JSON `data`。恢复时 `Last-Event-ID` 优先于 `after`，无效游标返回 `400`；15 秒心跳，终态回放后关闭连接。数据库事务先保存，再通知订阅者。线程桥接只发送可合并唤醒信号，客户端按序从 SQLite 读取，慢订阅者不阻塞 Agent。

章节只来自 Writer 既有验证回调，按 `(run_id,section_id)` 更新、按 seq 去重；同内容重复回调不追加。规划、Coverage 与 Graph 步骤在最终 Trace 摘要显示，运行中只显示真实 Observer 完成事件，不伪造进度。

证据绑定实际教学 `ContextItem`，最终与 `ContextBundle/EvidenceCatalog` 校对，保存 chunk ID、文件、行号、符号、内容与截断状态；无法解析的引用不推测来源，也不提供任意本地文件读取。Markdown 禁用 HTML 和危险链接。模型原始响应、私有推理、提示词、拒绝片段及敏感配置不进入公开摘要或 SQLite；配置密钥与数据库连接串在公开文本中脱敏。

## 终态规则

- `completed`：生成 complete，证据包 READY。
- `partial`：已有验证章节但生成中断、核心证据不足、检索失败或服务中断；原因分别保留。
- `failed`：没有验证回答的执行失败，错误提示不充当成功答案。
- `cancelled`：用户停止，保留此前验证内容。

最终答案与章节拼接核对，不一致保存验证章节并标 `partial/answer_mismatch`。取消与完成以先提交的终态为准。`worker_active` 独立于终态，取消后的真实线程退出前继续占用名额。

## 测试与真实验收

```powershell
cd D:\Java-learning\DevContext-web-ui-v1
uv sync --locked --extra web --extra dev
uv run --extra web --extra dev pytest
# PostgreSQL 可用时，开启全部原有数据库集成测试。
$env:DEVCONTEXT_RUN_INTEGRATION = '1'
uv run --extra web --extra dev pytest
Remove-Item Env:DEVCONTEXT_RUN_INTEGRATION

cd frontend
npm ci
npm test
npm run build
npx playwright install chromium
npm run test:e2e
npm audit --audit-level=moderate
```

Playwright 自动启动测试后端和 Vite，8000/5173 端口须空闲。测试后端使用临时 SQLite 与确定性工作流，不调用付费模型。`tests/web_fixture.py` 仅用于测试。

真实验收使用原有 PostgreSQL、索引和模型，执行两条付费问答：

```powershell
cd D:\Java-learning\DevContext-web-ui-v1
uv run --extra web python scripts/verify_web_v1_live.py
```

报告写入 `artifacts/web-v1-live.json`，实际会话存入 `data/web/live-acceptance.sqlite3`。脚本核对真实 Fast/Full、引用、章节、刷新与 SSE 重放，不把替身测试计为真实通过。
