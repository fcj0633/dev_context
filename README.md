# DevContext-Java

DevContext-Java 从 `my12306` 的 Java 源码和 Markdown 项目文档中提取结构化片段，使用 PostgreSQL/pgvector 建立关键词、向量和混合检索，并基于带引用的 Context 生成可追溯答案。系统不会修改源项目。

## 数据源

- 代码：`D:\Java-learning\12306Project\12306\my12306`
- 文档：`D:\Java-learning\12306Project\docs`
- `参考项目` 不读取、不索引。

两个数据源均为只读；索引和运行产物只写入本项目与 Docker 数据卷。

## 快速开始

要求：Java 21、Maven、Docker Desktop 和已配置的 `DASHSCOPE_API_KEY`。

> **本机开着 VPN 时，必须把阿里云域名排除出代理。** Hiddify 这类工具会设置 `HTTP_PROXY` / `HTTPS_PROXY`，而经代理访问 `dashscope.aliyuncs.com` 会在 TLS 握手阶段失败（报 `schannel: failed to receive handshake`，Embedding 调用随即报错）。
>
> 解决办法是把它加进 `NO_PROXY`，**不需要改代码**：
>
> ```powershell
> setx NO_PROXY "localhost,127.0.0.1,::1,.local,.aliyuncs.com"
> ```
>
> 只排除阿里云域名、保留 DeepSeek 继续走代理是有意的——DeepSeek 经代理访问正常，全量关掉代理反而会引入新问题。设置后需重开终端。注意 **curl 优先读小写 `no_proxy`**：如果环境里同时存在该变量，不要在只设大写时把它留成空值，否则绕过会失效。

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
uv run devcontext ask "详细解释当前购票占座的数据一致性是如何保证的" --answer-mode explain --debug
```

运行测试与评测：

```powershell
uv run pytest
mvn -q -f java-parser\pom.xml test
uv run devcontext evaluate
uv run devcontext evaluate --benchmark benchmark\cases.jsonl --baseline benchmark\baselines\retrieval-v1.json
uv run devcontext evaluate-retrieval-workflow
uv run devcontext evaluate-retrieval-workflow --suite regression --mode frozen
uv run devcontext evaluate-retrieval-workflow --suite l1.5 --mode live --runs 3
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
| `devcontext evaluate-retrieval-workflow` | 运行 18 条 L1.5 与 5 条关键回归案例，评估 RetrievalController 的证据覆盖、False READY、Context 保留和二次补查效果 |

`devcontext evaluate` 是 L1 固定 Query 底层检索回归：评测集由 CODE、DOC、MIXED 各 12 条组成，统计 Recall@3/@5/@10/@20、Full-case Success@3/@5/@10/@20、CODE/DOC hit、MIXED both-sources hit，并在 routed Top5 失败时区分 `RETRIEVAL_MISS`、`RANKING_MISS` 和 `COMPOSITION_MISS`。它只评估 keyword、vector、hybrid 和旧 routed 策略，不评估 EvidencePlanner、SearchActionPlanner、CoverageChecker 或 EvidencePackage，因此不能代表当前证据驱动链路的端到端质量。详细 Top20 排名写入 `artifacts/`，精简基线位于 `benchmark/baselines/retrieval-v1.json`；只有 benchmark 哈希一致时才进行前后对比。

`devcontext evaluate-retrieval-workflow` 是 L1.5 工作流评测。默认以冻结 EvidencePlan、冻结 SearchAction 和确定性 Oracle Coverage 运行真实 RetrievalPolicy、EvidencePool 与 ContextBuilder；`--mode live` 改为运行真实 LLM 规划与覆盖检查，但最终质量仍由人工 Ground Truth 独立评分。报告包含 Full-case Success、CORE Requirement Coverage、False READY、Context Survival、Second-round Rescue、状态准确率、动作数量、阶段延迟和 Token。第一版硬门槛只有执行错误为 0、False READY 为 0；其余指标先建立真实基线。`--suite regression` 会让 5 条关键问题真实运行到 EvidencePackage，而不是只检查 JSON 格式。

## Context Builder V1

`devcontext context` 将 Router + Retrieval Policy 返回的 Top-K 结果转换为结构化 Context。默认字符预算为 6000，预算包含 Citation 元数据、正文和条目分隔符；输出会展示 Java 文件与行号，或 Markdown 文件与完整标题层级。相同 Chunk 按数据库 ID 去重，输入同时包含 CODE 和 DOCUMENT 且预算允许时会优先保留两类证据。该命令只构建 Context，不调用答案生成模型；但规则无法判断问题类型时可能调用一次短 LLM Router。

Context Builder 的后续方向包括 tokenizer 预算、语义去重、相邻 Chunk 合并、Parent Context、动态来源配额、意图识别、二次检索、Query Rewrite 和 Reranker；这些能力不属于 Context Builder V1。

## LLM Answer + Citation V1

`devcontext ask` 的兼容路径执行 Query Router → Retrieval Policy → Context Builder → Context Sufficiency → DeepSeek。模型只能根据当前 Context 回答，并使用 `[C1]` 等引用；程序会提取引用、拒绝不存在的 Citation，并根据 `ContextBundle` 中的真实元数据打印 Sources。最终 Context 为空时不调用答案模型。兼容路径默认使用 `deepseek-flash`、低强度思考、8192 token 单次答案生成上限、Top 5 和 6000 字符 Context 预算；任何非正常结束的生成结果都会被拒绝。API Key 只从 `DEEPSEEK_API_KEY` 用户环境变量读取。

## Evidence-driven Explain

`ask --answer-mode explain` 启用“先找事实、再设计回答”的证据驱动路径。输入层只校验原始问题、回答模式、回答深度和显式 Context 预算；Evidence Planner 随后把原问题转换为 1–6 条可验证的证据需求。每条需求只描述要确认的项目事实、满足条件、优先级、时间范围和所需来源，不包含搜索 Query、回答目标、解释策略或章节结构。`--plan-only` 默认展示这一版 EvidencePlan；兼容期可用 `--plan-format legacy` 查看旧 QuestionPlan。

Retrieval Controller 根据每条证据需求动态生成 SearchAction，并只调用系统已有的 keyword、vector、hybrid 与来源过滤能力。CORE 和 SUPPORTING 需求第一轮分别取得 5 个和 3 个候选；Coverage Checker 逐条检查来源与语义是否满足，只有仍为 PARTIAL 或 MISSING 的 CORE 才能执行第二条查询。第二轮会利用第一轮真实发现的类名、方法名、文件名、标题和明确缺口调整查询。单次请求最多 6 条需求、12 个逻辑 SearchAction 和两轮 Coverage 检查；检查器自身失败记为 UNVERIFIED，不冒充“没有证据”，也不会触发盲目补查。

检索 Context 预算由证据计划复杂度决定，而不是由回答深度决定：单条且非双来源需求使用 8000 字符，2–3 条需求或存在一条 BOTH 使用 16000 字符，4–6 条需求或存在多条 BOTH/当前核心需求使用 28000 字符；显式 `--max-chars` 优先。因而同一问题的 `--depth brief` 与 `--depth detailed` 使用相同 EvidencePlan、SearchAction、检索预算和 Coverage，只在后续 AnswerPlan 与回答篇幅上不同。

检索结束后生成不可追加的 EvidencePackage，固定已经找到的证据、证据与需求的归属、逐条 Coverage、未解决需求和查询历史。形成 Package 后，Answer Planner、答案生成器与 Reviewer 均不得再次搜索或更改 Coverage。Package 的状态为 READY、PARTIAL 或 EMPTY：全部核心需求满足才是 READY；有可用证据但仍有核心缺口是 PARTIAL；没有直接项目证据则是 EMPTY。

文档来源由 `config/source-policy.json` 按路径规则标记为验证报告、当前设计、普通文档、历史计划或未知来源；代码固定标记为当前实现。这些标记进入内部 Context、Answer Planner 和 `--debug` Trace，普通回答不会显示优先级。设计说明只能证明设计意图，历史或未来文档不能覆盖当前代码；无法消解的冲突必须在回答中明确说明。

Answer Planner 只在 EvidencePackage 形成后决定回答目标、深度、解释策略、章节和目标篇幅。Requirement 不等于回答章节：一节可以组合多条证据需求，一条需求也可以服务于多个解释位置。detailed 固定审稿一次；standard 在冲突、零有效引用、结构失败、核心遗漏或篇幅失败时审稿；brief 默认不审稿。审稿失败保留原草稿，不阻断回答。最终 `AnswerResult` 仍保持原结构并剥离 Citation，`--debug` 会显示证据计划、SearchAction、Coverage、回答规划、冲突、审稿、来源及阶段耗时。

当前 rollout 默认仍为 `--answer-mode legacy`，便于对同一批问题做 A/B；质量门槛达标后再切换默认值。规划器、Answer Planner、答案生成器和 Reviewer 使用 high reasoning，生成上限分别为 8192、8192、32768 和 32768；充分性判断与查询改写使用 low reasoning、4096 token。各角色模型可通过 `.env.example` 中的可选配置分别覆盖。

L2 回答质量集位于 `benchmark/l2-answer-quality.jsonl`，包含 18 条实现流程、设计取舍、异常边界、否定式和简单定位问题。确定性检查覆盖篇幅、重复标题、机械式“结论：”和有效引用；盲测胜率、事实正确性、证据支持程度、实际延迟与 Token 成本仍需在切换默认模式前形成报告。

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
