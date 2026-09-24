# DevContext-Java

DevContext-Java 从 `my12306` 的 Java 源码和 Markdown 项目文档中提取结构化片段，使用 PostgreSQL/pgvector 建立关键词、向量和混合检索。首个版本专注于可验证的检索闭环，不生成答案，也不修改源项目。

## 数据源

- 代码：`D:\Java-learning\12306Project\12306\my12306`
- 文档：`D:\Java-learning\12306Project\docs`
- `参考项目` 不读取、不索引。

两个数据源均为只读；索引和运行产物只写入本项目与 Docker 数据卷。

## 快速开始

要求：Java 21、Maven、Docker Desktop 和已配置的 `DASHSCOPE_API_KEY`。

```powershell
cd D:\Java-learning\DevContext
.\scripts\bootstrap.ps1
docker compose up -d
uv run devcontext init-db
uv run devcontext smoke-api
uv run devcontext ingest
```

搜索示例：

```powershell
uv run devcontext search --strategy keyword --query "purchaseTicket" --top-k 10
uv run devcontext search --strategy vector --query "为什么使用余票令牌桶" --top-k 10
uv run devcontext search --strategy hybrid --query "为什么缩短事务边界，相关实现在哪里" --top-k 10
uv run devcontext context "购票事务是如何实现的？" --top-k 5
uv run devcontext context "订单关闭的代码和设计依据" --top-k 10 --max-chars 8000
uv run devcontext ask "OrderServiceImpl.createTicketOrder 如何保证事务和幂等？" --top-k 5
uv run devcontext ask "订单超时关闭的设计依据是什么？" --top-k 10 --max-chars 8000
```

运行测试与评测：

```powershell
uv run pytest
mvn -q -f java-parser\pom.xml test
uv run devcontext evaluate
uv run devcontext evaluate --benchmark benchmark\cases.jsonl --baseline benchmark\baselines\retrieval-v1.json
```

## 命令

| 命令 | 用途 |
|---|---|
| `devcontext init-db` | 创建扩展、表和索引 |
| `devcontext smoke-api` | 验证百炼连接及 1024 维输出 |
| `devcontext ingest` | 全量重建 `my12306` 索引 |
| `devcontext search` | 运行关键词、向量或混合检索 |
| `devcontext context` | 将 Hybrid 检索结果构建为带 `[C1]` 引用和字符预算的 LLM-ready Context |
| `devcontext ask` | 使用 DeepSeek-V4.1-Flash 基于当前 Context 生成带 Citation 的回答 |
| `devcontext evaluate` | 运行 36 条分层基准问题，输出分类指标、分段耗时、失败诊断和基线差异 |

评测集由 CODE、DOC、MIXED 各 12 条组成。详细报告写入 `artifacts/`，可提交的精简基线位于 `benchmark/baselines/retrieval-v1.json`；只有 benchmark 哈希一致时才进行前后对比。

## Context Builder V1

`devcontext context` 固定复用现有 Hybrid Retrieval，将 Top-K 结果转换为结构化 Context。默认字符预算为 6000，预算包含 Citation 元数据、正文和条目分隔符；输出会展示 Java 文件与行号，或 Markdown 文件与完整标题层级。相同 Chunk 按数据库 ID 去重，输入同时包含 CODE 和 DOCUMENT 且预算允许时会优先保留两类证据。本阶段只构建 Context，不调用 LLM。

Context Builder 的后续方向包括 tokenizer 预算、语义去重、相邻 Chunk 合并、Parent Context、动态来源配额、意图识别、二次检索、Query Rewrite 和 Reranker；这些能力不属于 Context Builder V1。

## LLM Answer + Citation V1

`devcontext ask` 固定执行 Hybrid Retrieval → Context Builder → DeepSeek-V4.1-Flash。模型只能根据当前 Context 回答，并使用 `[C1]` 等引用；程序会提取引用、拒绝不存在的 Citation，并根据 `ContextBundle` 中的真实元数据打印 Sources。空 Context 不调用模型。默认使用 `deepseek-flash`、低强度思考、4096 token 单次生成上限、Top 5 和 6000 字符 Context 预算；任何非正常结束的生成结果都会被拒绝。API Key 只从 `DEEPSEEK_API_KEY` 用户环境变量读取。

V1 不判断 Citation 是否在语义上真正支持对应结论，也不实现 Router、自动类型分类、Query Rewrite、Retry、Context Sufficiency Judge、LangGraph、Reranker、检索调参或 Context Builder V2。

默认配置见 `.env.example`。API Key 始终从系统环境变量读取，不应写入 `.env` 或提交到 Git。

## 设计边界

当前版本不包含 LangChain、LangGraph、FastAPI、Web UI、SymbolSolver、调用图、增量索引或近似向量索引。本地 `docs/` 目录保存详细设计与历史资料，但按项目约定不纳入 Git。
