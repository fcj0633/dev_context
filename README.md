# DevContext-Java

DevContext-Java 从 `my12306` 的 Java 源码和 Markdown 项目文档中提取结构化片段，使用 PostgreSQL/pgvector 建立关键词、向量和混合检索，并基于带引用的 Context 生成可追溯答案。系统不会修改源项目。

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
uv run devcontext ask "购票库存参数由哪个责任链 handler 校验，为什么要先挡掉非法请求？" --debug
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
| `devcontext context` | 按问题类型检索证据，并构建带 `[C1]` 引用和字符预算的 LLM-ready Context |
| `devcontext ask` | 按问题类型检索，检查证据充分性，必要时定向改写并有限重试，最后生成带 Citation 的回答 |
| `devcontext evaluate` | 运行 36 条分层基准问题，输出 Router Accuracy、分类指标、分段耗时、失败诊断和策略差异 |

评测集由 CODE、DOC、MIXED 各 12 条组成。报告同时统计 Recall@3/@5/@10/@20、CODE/DOC hit@10、MIXED both-sources hit@10，并在 routed Recall@5 失败时区分 `RETRIEVAL_MISS`、`RANKING_MISS` 和 `COMPOSITION_MISS`。详细的 Top20 排名与失败案例表写入 `artifacts/`，可提交的精简基线位于 `benchmark/baselines/retrieval-v1.json`；只有 benchmark 哈希一致时才进行前后对比。

## Context Builder V1

`devcontext context` 将 Router + Retrieval Policy 返回的 Top-K 结果转换为结构化 Context。默认字符预算为 6000，预算包含 Citation 元数据、正文和条目分隔符；输出会展示 Java 文件与行号，或 Markdown 文件与完整标题层级。相同 Chunk 按数据库 ID 去重，输入同时包含 CODE 和 DOCUMENT 且预算允许时会优先保留两类证据。该命令只构建 Context，不调用答案生成模型；但规则无法判断问题类型时可能调用一次短 LLM Router。

Context Builder 的后续方向包括 tokenizer 预算、语义去重、相邻 Chunk 合并、Parent Context、动态来源配额、意图识别、二次检索、Query Rewrite 和 Reranker；这些能力不属于 Context Builder V1。

## LLM Answer + Citation V1

`devcontext ask` 执行 Query Router → Retrieval Policy → Context Builder → Context Sufficiency → DeepSeek-V4.1-Flash。模型只能根据当前 Context 回答，并使用 `[C1]` 等引用；程序会提取引用、拒绝不存在的 Citation，并根据 `ContextBundle` 中的真实元数据打印 Sources。最终 Context 为空时不调用答案模型。默认使用 `deepseek-flash`、低强度思考、4096 token 单次答案生成上限、Top 5 和 6000 字符 Context 预算；任何非正常结束的生成结果都会被拒绝。API Key 只从 `DEEPSEEK_API_KEY` 用户环境变量读取。

## Query Router 与 Retrieval Policy V1

Router 优先使用确定性规则，将问题分为 `CODE`、`DOC` 或 `MIXED`。没有明确规则信号时才使用 DeepSeek 输出一个严格标签；模型不可用或输出非法时安全回退到 `MIXED`，不重试。Router 请求最多允许 1024 个生成 token，以容纳模型内部推理，但只接受最终完整输出 `CODE`、`DOC` 或 `MIXED`。CLI 会显示 `Route: CODE/DOC/MIXED (rules/llm/fallback)`。

- CODE：仅从代码来源执行现有 Hybrid Retrieval。
- DOC：仅从文档来源执行现有 Vector Retrieval。
- MIXED：分别执行 CODE 和 DOCUMENT Vector Retrieval。DOCUMENT 使用固定的 Top20 候选池，并根据文件、标题和完整标题层级与 query 的词项重合度，最多把一个最明确的文档证据提升为锚点；随后仍从 CODE 开始稳定交错。该操作不修改原始检索分数。

该层不改变基础 Retriever 的关键词评分、向量距离或 RRF。文档锚点提升只用于 MIXED 证据组合，不影响 CODE 或 DOC 单源路由。评测报告在原有 Keyword、Vector、Hybrid 之外增加 Routed 指标，并将同一次运行中的 Hybrid 与 Routed 分类 Recall 和双源命中率直接对比。

V1 不判断 Citation 是否在语义上真正支持对应结论，也不实现 LangGraph、Reranker、复杂检索调参或 Context Builder V2。

## Agentic Retrieval V1

`ask` 在检索后先做证据充分性检查。CODE 问题至少需要代码证据，DOC 至少需要文档证据，MIXED 必须同时具备两类来源；缺来源时由规则直接判定不足。来源齐全后，短 DeepSeek 调用会继续判断证据是否真正覆盖目标类、方法、设计或流程。检查输出采用严格 JSON，网络错误、空回答或非法结构都会失败关闭为“不充分”，不会把不确定证据交给普通答案生成。

证据不足时，系统只针对 `missing_aspects` 指出的 CODE、DOCUMENT 或双侧缺口生成一个新检索 Query。原始 Router 决策不会重跑，Retrieval Policy 和 Context Builder 行为也不会改变。每轮新增结果放在旧结果前，按 Chunk ID 稳定去重，再使用原始问题重建 Context。最多重试 2 次，即总计最多 3 轮检索；证据足够便立即停止。达到上限或改写失败后，只允许基于已有证据生成部分回答，并明确说明尚不能确认的方面。最终 Citation 仍必须存在于最终 ContextBundle。

普通输出显示 Route、Sufficiency 和 Retries。加入 `--debug` 后会额外打印 JSON Trace，包括每轮检索 Query、目标类型、选中 Chunk、缺失方面、改写结果、重试次数和停止原因；Trace 不包含 Chunk 全文、API Key 或模型凭据。最坏路径可能发生 3 次充分性判断、2 次 Query Rewrite 和 1 次答案生成；若 Router 规则无法分类，还会多一次 Router 模型调用。这是 V1 已知的延迟与成本边界。

默认配置见 `.env.example`。API Key 始终从系统环境变量读取，不应写入 `.env` 或提交到 Git。

## 设计边界

当前版本不包含 LangChain、LangGraph、FastAPI、Web UI、SymbolSolver、调用图、增量索引或近似向量索引。`docs/` 中的自有项目文档可以提交；`docs/参考项目/` 及外部 `my12306` 数据源继续排除在 Git 之外。
