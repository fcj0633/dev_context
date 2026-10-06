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
uv run devcontext ask "详细解释项目的余票桶是如何设计的" --answer-mode teach --debug
```

`--answer-mode` 三种取值的差别、以及 `teach` 为什么更贵，见下文 Teaching Explain。

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
| `devcontext ask` | 按问题类型检索，检查证据充分性，必要时定向改写并有限重试，最后生成带 Citation 的回答。`--answer-mode` 取 `legacy`（模板式单次生成）、`explain`（证据驱动）或 `teach`（规划、逐节生成、编排、审稿）；正文按理解任务自适应展开，不提供四档回答深度配置 |
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

`ask --answer-mode explain` 启用“先找事实、再设计回答”的证据驱动路径。输入层只校验原始问题、回答模式和显式 Context 预算；Evidence Planner 随后把原问题转换为 1–6 条可验证的证据需求。每条需求只描述要确认的项目事实、满足条件、优先级、时间范围和所需来源，不包含搜索 Query、回答目标、解释策略或章节结构。`--plan-only` 默认展示这一版 EvidencePlan；兼容期可用 `--plan-format legacy` 查看旧 QuestionPlan。

Retrieval Controller 根据每条证据需求动态生成 SearchAction，并只调用系统已有的 keyword、vector、hybrid 与来源过滤能力。CORE 和 SUPPORTING 需求第一轮分别取得 5 个和 3 个候选；Coverage Checker 逐条检查来源与语义是否满足，只有仍为 PARTIAL 或 MISSING 的 CORE 才能执行第二条查询。第二轮会利用第一轮真实发现的类名、方法名、文件名、标题和明确缺口调整查询。单次请求最多 6 条需求、12 个逻辑 SearchAction 和两轮 Coverage 检查；检查器自身失败记为 UNVERIFIED，不冒充“没有证据”，也不会触发盲目补查。

检索 Context 预算由证据计划复杂度决定，而不是由回答深度决定：单条且非双来源需求使用 8000 字符，2–3 条需求或存在一条 BOTH 使用 16000 字符，4–6 条需求或存在多条 BOTH/当前核心需求使用 28000 字符；显式 `--max-chars` 优先。正文篇幅由理解任务决定，不以固定深度档位改变检索范围。

> 上面这套字符预算是 `legacy` 与 `explain` 两条路径的口径，保留不变以便与既有基线对比。`teach` 路径不走它——它从模型窗口推导每个视图的上限，见下文 Teaching Explain。

检索期间，找到的全部证据写入 **EvidenceWorkspace**，并同时冻结成不可追加的 EvidencePackage。两者分工是：Workspace 持有证据本体（不受任何 prompt 预算限制），Package 固定证据与需求的归属、逐条 Coverage、未解决需求和查询历史。形成 Package 后，Answer Planner、答案生成器与 Reviewer 均不得再次搜索或更改 Coverage。

**Coverage 判定读的是 Workspace，不是回答用的 Context。** 这一点是刻意设计的：早前的实现让 Coverage 读那份已被字符预算截断的 ContextBundle，于是一条需求可能只因为"它的证据被回答预算挤掉了"就被判为 MISSING——把呈现层的取舍误报成检索的缺失。现在 Coverage 通过 requirement-scoped 视图读 Workspace，与回答预算无关。

上一条还有一个连带修正：**第二轮检索的种子也改从 Workspace 取**，不再读被截断的 Context。否则第一轮被截断丢掉的类名或方法名，在第二轮再也拿不回来。

Package 的状态为 READY、PARTIAL、EMPTY 或 RETRIEVAL_FAILED：全部核心需求满足才是 READY；有可用证据但仍有核心缺口是 PARTIAL；没有直接项目证据则是 EMPTY；**检索本身没跑成**（本轮所有 SearchAction 都报错）是 RETRIEVAL_FAILED。最后一种单列，是因为把"接口不可达"说成"项目里没有这份证据"是一句更强、也是错误的话。

文档来源由 `config/source-policy.json` 按路径规则标记为验证报告、当前设计、普通文档、历史计划或未知来源；代码固定标记为当前实现。这些标记进入内部 Context、Answer Planner 和 `--debug` Trace，普通回答不会显示优先级。设计说明只能证明设计意图，历史或未来文档不能覆盖当前代码；无法消解的冲突必须在回答中明确说明。

Answer Planner 在 EvidencePackage 形成后决定回答目标、解释策略和章节。旧显式 explain 路径根据章节复杂度、冲突和结构问题决定审阅；Fast/Full 没有 Reviewer。审稿失败保留原草稿，不阻断回答。最终 `AnswerResult` 仍保持原结构并剥离 Citation，`--debug` 会显示证据计划、SearchAction、Coverage、回答规划、冲突、审稿、来源及阶段耗时。

**当前默认回答模式已切换为 `teach`**（`--answer-mode` 不给就用它）。切换依据与尚未闭合的部分一并说明如下，不把未跑的门槛当作已通过：

| 项 | 状态 |
|---|---|
| 教学评分（7 维） | **4.857 / 5** —— 心智模型／因果解释／渐进展露／示例／失败推演／取舍 六项满分，可读性 4.0 |
| 盲测胜率门槛 | **未取得结论。** 单例返回 `POSITION_BIASED`（正序判 explain 胜、换序判 teach 胜）；一个案例算不出胜率，门禁值为 `None`，既非通过也非不通过 |
| 全量 19 例 A/B | **尚未运行** |
| `detailed_in_2200_5000` | 该次对**两个臂都不通过**（explain 1,732 中文字低于下限，teach 10,923 高于上限），不能作为区分依据；对 teach 而言它本就是在拿本轮已废除的字数上限去量 |

那次评审偏好 `explain` 的理由本身是错的：它以"teach 声称装载由 Key 不存在触发"为据，而 `TicketAvailabilityTokenBucket.java:85-90` 显示 `BUCKET_MISSING(-1)` 确实就是进入 `loadBucket` 的分支——键不存在确实是触发装载调用的原因；锁内的 `hMultiGet` 字段级判定是另一层，teach 也陈述正确。该基准条目已修正。

`legacy` 与 `explain` 仍然可用（`--answer-mode legacy|explain`），且是 L1／L1.5／L2 既有基线的口径；要复现历史对比必须显式指定它们。

规划器、Answer Planner、答案生成器和 Reviewer 使用 high reasoning，生成上限分别为 8192、8192、32768 和 32768；充分性判断与查询改写使用 low reasoning、4096 token。`teach` 路径的 Explanation Planner、Writer、Composer、Reviewer 也使用 high reasoning，生成上限分别为 32768、32768、16384 和 32768。各角色模型可通过 `.env.example` 中的可选配置分别覆盖。

L2 回答质量集位于 `benchmark/l2-answer-quality.jsonl`，包含 19 条实现流程、设计取舍、异常边界、否定式和简单定位问题。确定性检查覆盖篇幅、重复标题、机械式“结论：”和有效引用；`teach-token-bucket-01` 另带教学维度（期望心智模型、必须解释的 why、可用场景、需要纠正的误解）。盲测胜率与事实正确性的人工核对**仍未完成**——详见上文默认模式切换的说明。

## Teaching Explain (V3)

`ask --answer-mode teach` 是当前质量最高的一条链路。它和 `explain` 的差别不在检索（两者共用同一套 EvidencePackage），而在**拿到证据之后怎么做**：`explain` 直接规划章节并生成；`teach` 先规划"读者应该按什么认知顺序理解"，再逐节写，再编排，再审。

### ExplanationPlan：把"怎么讲"变成被规划的产物

EvidencePlan 回答"我要查什么"，ExplanationPlan 回答"这些事实应该按什么顺序被理解"。后者包含：

| 字段 | 作用 |
|---|---|
| `core_mental_model` | **贯穿全文的那一句话**。`explain` 缺的正是它——没有组织轴，答案就只能是按代码模块罗列。这一字段非空是构造时的硬约束。 |
| `primary_strategy` / `secondary_strategies` | `PROBLEM_SOLUTION`、`CONCEPT_BUILDUP`、`TRADEOFF`、`FAILURE_ANALYSIS` 等，决定整体写法 |
| `sections[].section_type` | 14 种：`PROBLEM_SETUP`、`MENTAL_MODEL`、`MECHANISM`、`FAILURE_SCENARIO`、`TRADEOFF`、`BOUNDARY`… 章节按**认知角色**划分，不按 requirement 划分 |
| `sections[].claim_plans` | 每节要下的判断，以及**它凭什么下** |
| `sections[].evidence_state` | 该节的证据强度：`CONFIRMED` / `PARTIAL` / `UNVERIFIED` |

`claim_plans[].claim_type` 分四层，这是本版最重要的一条边界：

| 层 | 含义 | 强制规则 |
|---|---|---|
| `PROJECT_FACT` | 当前项目事实 | **必须绑定证据**，否则计划不成立 |
| `PROJECT_INFERENCE` | 由项目证据推出 | 措辞须体现是推导（"因此/这说明"） |
| `GENERAL_CONCEPT` | 通用技术知识 | 可以讲，但不得反推"本项目就是这样实现的" |
| `ILLUSTRATIVE_EXAMPLE` | 假设案例 | **必须 `conditional=True` 且写明 `assumptions`** |

外加强约束：`conditional` 的 claim 不得是 `CONFIRMED`。也就是说"如果……那么……"这种推演，系统在计划层就被要求写成条件句，而不是写成对项目的断言。这正是 `explain` 做不到、而 ChatGPT 那份回答最出彩的地方——它可以推演证据里没有的失败场景，但必须标明那是推演。

计划器的其余硬约束：章节标题不得等于任何 Evidence Requirement 的 target（命名即调查项，说明是复制而非规划）；`LOCATION_ONLY` 最多 2 节（简单定位不该被过度规划）；`NEGATIVE_CORRECTION` 必须含 `MISCONCEPTION` 节。

### 生成：Fast / Deep 双路径

普通 ask 默认 Fast＋低。下面记录旧生成路径，显式 `--teaching-generation-mode multi_pass` 可继续使用。
`single_stream` 兼容参数现映射到 Fast：轻量 Micro Planner 一次规划，单个 Streaming Writer
按章节连续生成，每节完整后检查引用及证据 allowlist，校验通过才发布给章节回调。
该模式跳过 Composer、Reviewer 和 Revision，仍使用原有检索、证据工作区与模型。

```powershell
uv run devcontext ask "详细解释当前购票占座的数据一致性是如何保证的" --answer-mode teach --teaching-generation-mode single_stream --perf --perf-json artifacts/single-stream.json
```

新入口的 profile 参数优先于 ANSWER_PROFILE；显式 `--teaching-generation-mode multi_pass` 可使用旧流程。
CLI 当前仍在生成结束后输出聚合答案，`--perf-json` 的 `stream` 字段提供首正文时间 TTFT、
首个校验通过章节时间 TTFS、章节计数、重试和完成状态。它们以整个请求开始为原点，
另有相对 Writer 开始的时间。发布章节前最多重试一次；发布后失败保留有效章节并标记
`partial`，不重播或自动切换模式。无有效输出则标记 `failed`。

需要复现实验时运行 `.venv\Scripts\python.exe scripts/run_single_stream_experiment.py`：
只执行 Q1/Q3 每种模式一次，共四次请求，没有预热、质量裁判或自动重跑。
产物保存在 `artifacts/single-stream-experiment/`；目录非空时拒绝覆盖，另一次实验须使用
`--output` 指定新目录。结果与限制见 [Single-Stream 实验报告](docs/performance/05-single-stream-teaching-experiment.md)。

节数 ≤ 3 且深度为 `brief`/`standard` 时走 **Fast Path**：一次生成。否则走 **Deep Path**：逐节生成，一节一次调用，每节**只喂该节绑定的证据**，最后交给 Composer 编排。

**Section-scoped Citation Validation。** 每一节只能使用它自己绑定的标签。即使用了存在于 Workspace、但不属于本节的证据，也记为 `invalid_citations`，不放行。这把 Citation 从"引用了一个存在的 chunk"升级为"这一节只能使用 Planner 指定的证据"。

**Composer 违反禁令时整体丢弃，不做局部修补。** 若编排后的正文出现草稿里没有的 Citation，输出整体回退为章节草稿的确定性拼接。理由：能凭空造出一个 Citation 的调用，它在同一段文本里的其他判断也都不可信。同时明确禁止 Composer 新增项目事实与未出现过的符号。

### 接地校验、Reviewer 与定向修订

**确定性校验（零成本、不漏）** 先跑，四条：`PROJECT_FACT_WITHOUT_EVIDENCE`、`CONDITIONAL_NOT_MARKED`、`EXAMPLE_NOT_LABELLED`、`UNVERIFIED_WRITTEN_AS_FACT`。

**Teaching Reviewer（LLM）** 接在其后，负责需要读懂语义的部分：9 类新 issue（`MISSING_MENTAL_MODEL`、`MISSING_WHY`、`POOR_SCAFFOLDING`、`MISLABELED_EXAMPLE`、`FACT_INFERENCE_CONFUSION`、`GENERAL_KNOWLEDGE_AS_PROJECT_FACT`、`SECTION_EVIDENCE_MISMATCH`、`UNHELPFUL_DETAIL`、`ABRUPT_TRANSITION`），并**同时接受既有的 8 类**（含 `UNSUPPORTED_CLAIM`）——上一轮的 legacy Reviewer 恰好抓到过一处真实过度声称，一个说不出这个词的教学 Reviewer 会在它替代的对象更强的地方更弱。

**三档门控**，避免一句话的答案也为审稿付费：

| 路径 | 门控 |
|---|---|
| `brief` / `LOCATION_ONLY` | 只跑确定性校验 |
| `standard` | 确定性校验发现问题或计划含冲突时才上 LLM |
| `detailed` / `deep` | 必跑 |

**定向修订。** Reviewer 输出 `section_issues` 与 `revision_required`，工作流**只重生成被点名的章节**，然后重新编排；最多一轮。不让 Reviewer 替换一份基本没问题的答案。审稿调用失败时**不阻塞答案**，记为 `fallback` 并保留原草稿。

### 证据视图与 Token 预算

`teach` 路径不继承 `explain` 那套 8000/16000/28000 字符预算。它从模型窗口推导每个视图的上限：先扣掉系统提示、问题与计划等不可裁剪部分和预留输出，剩下的才是证据视图的额度。窗口小到装不下任何东西是**合法结果**，返回空视图并记录被省略的证据，而不是报错。

**证据的稳定身份。** 每个 chunk 在整次请求内获得一个 `E` 编号，跨节、跨视图不变（`legacy`/`explain` 沿用的 `C1` 是按位置分配的，同一个 chunk 在不同 bundle 里编号不同，无法作为身份）。`C` 与 `E` 两个命名空间并存，互不干扰。

### 成本

`teach` 明显更贵。同一问题实测（detail 深度）：`legacy` 约 3,100 字、5 次 LLM 调用；`explain` 约 3,700 字、6 次；`teach` 约 18,800 字、8 次调用，端到端约 5 分钟。检索侧三者相同，差额在回答侧。`brief` 问题走 Fast Path，约 2 次调用。

### `--debug` 看什么

`teach` 在 `--debug` 下额外打印一段摘要，比读全文更快看出这轮做了什么：

```
Explanation Strategy:
PROBLEM_SOLUTION + CONCEPT_BUILDUP + TRADEOFF + FAILURE_ANALYSIS

Mental Model:
余票桶是购票准入用的近似令牌池，不是库存账本……

Context:
  Workspace evidence: 20
  Bound evidence: 14
  S1 view: 3

Generation:
  9 sections
  0 targeted revision
```

Trace JSON 里另有 `explanation_plan`、`teaching.section_drafts`、`teaching.section_citations`、`teaching.section_confidence`、`teaching.context_views`、`teaching.revision_trace`。`compression_events` 存在但为空——读者应当能区分"没有发生"与"没有记录"。

### 本轮不在范围内

`teach` 不含 Symbol Graph、Repo Map、LangGraph、Web UI、增量索引，也没有把 `explain` 的固定字数上限一起改掉（那两处上限管的正是 L2 A/B 要对比的基线，同时改会混淆两个变量）。`EvidenceDigest`（把超长证据压成摘要再进 prompt）同样留待下一阶段——目前 Coverage 的 payload 仍送整块正文，这是它偏大的原因。

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


### 使用 APINebula 文字模型

项目 `.env` 设置 `LLM_PROVIDER=openai` 与 `OPENAI_MODEL=gpt-6.1-sol`。
`CHATGPT_API_KEY` 和 `OPENAI_BASE_URL` 从环境读取；当前网关地址为
`https://apinebula.ai/v1`。读取顺序为进程环境、项目 `.env`、Windows 用户环境、
Windows 系统环境。密钥不必写进项目文件。

OpenAI 模式下，检索规划、改写、覆盖检查、教学规划、正文和 Reviewer
统一使用 `OPENAI_MODEL`，不继承 `DEEPSEEK_*_MODEL`；显式模型参数仍优先。
百炼嵌入、数据库及现有索引保持原配置，无需重新入库。
普通 ask 默认 Fast；可用 `--profile full` 或兼容参数 `--teaching-generation-mode v3` 显式选择 Full。
切回 DeepSeek 时设置 `LLM_PROVIDER=deepseek`；调用失败不会自动切换供应商。


APINebula 接入会分离返回正文开头的 `<think>...</think>` 推理前缀，
传输与章节协议仍检查完整性，新流程的证据校验改为宽松策略。

### 回答执行模式与思考深度

新回答入口默认 **Fast＋低思考深度**；显式 `--profile full` 默认高。两档都支持
`--reasoning-effort low|medium|high`，只控制回答 Planner/Writer；检索阶段使用低。
正文按问题理解任务自适应展开；`--depth` 已移除，可在问题中说明简要或详细。
模型没有声明支持所选档位时真正省略 `reasoning_effort`，使用模型默认行为，并在 debug 中显示。
DeepSeek Flash 原生为 low/high/max，中档兼容值映射为高；本项目按用户选择省略 medium，不做映射。
未知模型可通过 `LLM_SUPPORTED_REASONING_EFFORTS` 显式声明原生支持，不要只因网关接受参数就声明支持。

```powershell
uv run devcontext ask "详细解释项目为何要使用责任链校验。" --debug
uv run devcontext ask "详细解释项目为何要使用责任链校验。" --profile full --debug
uv run devcontext ask "问题" --profile full --reasoning-effort high
uv run devcontext ask "问题" --profile full --hard-timeout 300 --perf-json artifacts/request.json
```

默认没有请求总时间截止，Fast 45 秒、Full 300 秒只是观测目标，超时不降档、不截断正文。
可选 `ANSWER_HARD_TIMEOUT_SECONDS` / `--hard-timeout` 从检索开始计时。
外部调用仍有超时：Fast Planner 180 秒、Writer 240 秒；Full Planner 300 秒、Writer 600 秒；检索 LLM 120 秒、嵌入 30 秒、数据库连接 10 秒/查询 30 秒。
`--debug` 直接显示各阶段耗时、模型、请求档位、实际传参和首节发布时间；`--perf-json` 保存正文、蓝图及诊断。
`complete` 表示当前交付路径正常完成，不是事实审查认证；部分发布后失败返回 partial，不重放已发布章节。

Fast/Full 支持 WHAT、WHY、HOW、COMPARE、DEBUG、LOCATE、GENERAL；空检索仍可解释通用原理，定位未命中不会编造文件路径。
Full 保留 WHY/HOW 骨架，其他问题采用适配结构；Fast 使用合并检索规划与最多一轮补检。
`single_stream` / `v3` 兼容参数映射 Fast / Full。旧 `multi_pass`、`legacy`、`explain` 可显式使用，不能与新 Profile/思考参数混用。
旧 `OPENAI_V3_REASONING_EFFORT`、`TEACHING_REQUEST_TIMEOUT_SECONDS` 不再控制新 Fast/Full。

OpenAI 的 JSON 结果内部通过 SSE 收集，完成后才交给结构解析器，避免长非流式蓝图触发网关 524。


## 当前 Full 教学流程与配置迁移

普通 ask 默认 Fast＋low；Full 默认 high，按问题需要安排解释，章节和例子不再由四档深度控制。

```powershell
uv run devcontext ask "项目中的责任链校验是什么，它与业务流程是什么关系？" --profile full --debug
uv run devcontext ask "详细解释项目为何要使用责任链校验。" --profile full --reasoning-effort high --debug
uv run devcontext ask "详细解释当前购票占座的数据一致性是如何保证的。" --profile full --debug --perf-json artifacts/full-how.json
```

`--depth` 已移除；请在问题中表达简要或详细。旧计划/诊断中的深度字段读取时忽略，旧显式模式也不再用档位控制章节数、预算或审阅。`--teaching-generation-mode v3` 映射当前 Full，不还原历史版本。

配置优先级为命令参数、进程环境、项目 .env、代码默认。`ANSWER_REASONING_EFFORT` 是全局覆盖项；不设置时 Fast 用 low、Full 用 high。思考参数只影响回答 Planner/Writer，检索阶段保持 low；能力未知时省略参数并在 debug 说明。

| 配置 | 默认 | 行为 |
|---|---:|---|
| ANSWER_PROFILE | fast | 默认模式 |
| ANSWER_REASONING_EFFORT | 未设置 | 可用 low/medium/high；显式值覆盖模式默认 |
| ANSWER_HARD_TIMEOUT_SECONDS | 未设置 | 可选完整请求截止，包含检索和重试 |
| ANSWER_FAST_LATENCY_TARGET_SECONDS | 45 | Fast 软观测目标 |
| ANSWER_FULL_LATENCY_TARGET_SECONDS | 300 | Full 软观测目标，不中断或降档 |
| ANSWER_PLANNER_TIMEOUT_SECONDS | 180 | Fast Planner 单次调用超时 |
| ANSWER_WRITER_TIMEOUT_SECONDS | 240 | Fast Writer 单次调用超时 |
| ANSWER_FULL_PLANNER_TIMEOUT_SECONDS | 300 | Full Planner 单次调用超时 |
| ANSWER_FULL_WRITER_TIMEOUT_SECONDS | 600 | Full Writer 单次调用超时 |

Full 正常只有 Planner、Organizer、Writer；回答模型调用两次，无 Reviewer。恢复 WHY/HOW 专用场景、完整示范与依赖覆盖检查，WHAT 用职责、关系和贯穿例子解释。证据不足缩小具体断言，必要证据超窗口明确失败。章节流式发布后不重试、不重放；complete 只表示完整交付，不认证内容质量。

## Full 结构恢复与正文兜底

Full 正常路径仍为一次 Planner、一次 Writer。蓝图中的重复编号和悬空核心引用需要修正；合法但跳号的编号可保留。非法场景会设为 NONE，清空章节场景引用，保留机制骨架，不猜测分叉父节点。UNKNOWN 结论不会作为已建立保证。

Planner 最多修正一次，修正输入包含原始响应及具体字段错误；Writer 首节发布前最多重试一次，携带错误与精确章节协议。修正输入超模型窗口时跳过重试。两次 Planner 响应及 Writer 拒绝输出均进入诊断。

仍无法执行 Full 时，且未发布正文，使用同一批证据、同一模型与思考档位执行一次普通 Markdown 正文兜底，不重新检索。兜底先缓冲再整体发布；正常结束且正文非空为 complete，截断或异常但有正文为 partial，无正文为 failed。已发布章节后出错不兜底、不续写、不重放。HTTP 400/401/402/403/404 等配置、权限或余额错误不重复调用；显式硬截止仍停止后续调用。

Debug 的 `delivery_path` 区分 `full`、`full_repaired`、`full_direct_fallback`；正文兜底也会在普通结果信息中明确标记。兜底计作实际一个交付单元，不把原蓝图章节计为已完成。`--perf-json` 保存原始规划响应、警告、重试、兜底原因及分阶段耗时。最多五次回答模型调用，无无限重试。
