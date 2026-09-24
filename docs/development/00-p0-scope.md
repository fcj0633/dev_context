# DevContext-Java P0 范围冻结

> 文档编号：`00-p0-scope`
> 阶段：M0 Project Freeze
> 状态：**FROZEN**（冻结来源见每节标注）
> 本文回答：P0 做什么、不做什么、做到什么程度算完成。

---

## 0. 本文依据与术语

### 0.1 依据来源

| 代号 | 文件 | 用途 |
|---|---|---|
| DEF | `项目规划文档/devContex-项目背景和约束.md` | 项目定义、核心问题、Non-goals、目录结构 |
| PLAN | `项目规划文档/初步开发计划.md` | 阶段划分、每日验收、DoD、P1 候选池 |
| FREEZE | `项目规划文档/开发前任务冻结.md` | 本轮冻结指令、Frozen Architecture、Milestone 列表 |
| REQ | `项目规划文档/调研需求.md` | 调研问题清单、决策原则、优先级 |
| R-CONT | `调研文档/Continue调研.md` | 分层/Chunk/Index/增量索引的源码级证据 |
| R-JAVA | `调研文档/JavaParser调研.md` | AST/API/Range/Citation 的源码级证据 |
| R-RAG | `调研文档/RAGFlow调研.md` | Chunk 策略/字段加权/融合/Rerank/评测的源码级证据 |

**冲突优先级**：DEF > FREEZE > PLAN > 调研文档。存在冲突时记入 `01-architecture-decisions.md` 的 `Open Questions / Conflicts` 章节，不静默取舍。

### 0.2 术语统一

| 术语 | 含义 | 备注 |
|---|---|---|
| `knowledge_chunk` | 统一的 Chunk 存储单元，DOC 与 CODE 共用一张表 | 沿用 DEF 第 13 节 |
| `source_type` | `DOCUMENT` / `CODE` | 沿用 DEF 第 13 节 |
| `chunk_type` | `DOCUMENT_SECTION` / `CLASS` / `METHOD` / `CONSTRUCTOR` | PLAN 第 8 节写作 `SECTION`，与 DEF 不一致，见 Conflict 01 |
| CodeChunk | Java Parser 输出的 DTO（入库前的中间结构） | 不等同于 `knowledge_chunk` 行 |
| Repository | 被索引的代码库（此处为 `my12306`） | |

---

## 1. Project Goal

### 1.1 P0 要证明的命题

> **DevContext 能否把真实 Java Repository 中的 Java 代码与 Markdown 设计文档转换为结构化知识，并通过 Keyword、Vector、Hybrid 三种检索策略，得到可量化、可复现、可解释的 Context Retrieval 能力。**

这句话包含三个**必须被分别证明**的子命题：

**命题 A —— 结构化索引优于朴素文本切分（Structure-aware Indexing）**

需要证明：按 AST 节点（Method / Class / Constructor）切出的 Code Chunk，比按固定字符数切出的文本块更适合代码检索。
证据形式：Method Chunk 的 `content` 与源码行号一致；符号类查询（`purchaseTicket`）能命中正确 Chunk。

**命题 B —— 混合检索优于单路检索（Hybrid Retrieval）**

需要证明：`Keyword + Vector + RRF` 在自有 Benchmark 上的 Recall@K / MRR 不低于纯 Vector 与纯 Keyword。
证据形式：三套策略在同一组 ≥30 条 Query 上的对照指标表，**且数字必须是实测值**。

**命题 C —— 检索可以被独立度量（Evaluation-driven）**

需要证明：Retrieval 质量可以脱离 LLM 生成被单独评测，失败可以被定位到 Parsing / Chunk / Metadata / Retrieval 中的某一层。
证据形式：可重复运行的 Benchmark 脚本 + 失败案例分层报告。

### 1.2 P0 不证明什么

以下均**不属于** P0 的成功标准：

- 不证明系统比 Cursor / Claude Code / Continue 更强；
- 不证明可以回答任意 Java 项目的任意问题；
- 不证明可以修改、生成、执行代码；
- 不追求指标绝对值（P0 只有 30 条自建 Query，样本量不支持统计显著性结论）。

DEF 第 35 节明确：项目成功 = 开发者能通过实际代码与实验解释 10 个问题（为何分层 / 为何不能乱切 / 为何需要两路 / 为何 RRF / 为何 LangGraph / 为何 Rewrite / 为何限制 Retry / 如何评价一次 Retrieval / 如何用数据证明优化有效）。**P0 的验收本质上是这 10 个问题能否被回答，而不是指标高低。**

---

## 2. P0 Features

标注说明：`[F]` = 已冻结（FREEZE §5 或 DEF 明确）；`[S]` = 由来源资料推导、本轮确认。

### 2.1 Ingestion（索引构建链路）

| # | 功能 | 来源 | 验收要点 |
|---|---|---|---|
| 1 | Repository Scanner | DEF §27-1 [F] | 可扫描指定代码根目录，识别 `.java`；可扫描指定文档根目录，识别 `.md` |
| 2 | Java Parser（独立 CLI） | DEF §25 [F] | 输入 repo path，输出 CodeChunk JSON；可脱离 DevContext 单独运行 |
| 3 | Markdown Parser | DEF §9 [F] | Heading-aware 切分，产出 `heading_path` |
| 4 | Code Chunk 生成 | FREEZE §5.5 [F] | `METHOD` 为主，`CLASS` 为摘要，`CONSTRUCTOR` 独立 |
| 5 | Document Chunk 生成 | FREEZE §5.6 [F] | 按 `#`/`##`/`###` 层级切分 |
| 6 | 忽略规则 | PLAN §21 [S] | 排除 `target/`、`.git/`、`.idea/`、`node_modules/`、第三方前端库目录 |
| 7 | Parse Failure 容错 | R-JAVA §3.1 [S] | 单文件解析失败不中断整仓 ingestion，记录 problem 后继续 |

### 2.2 Storage（存储与索引）

| # | 功能 | 来源 | 验收要点 |
|---|---|---|---|
| 8 | `knowledge_chunk` 表 | DEF §13 [F] | DOC / CODE 共用一张表 |
| 9 | PostgreSQL + pgvector | FREEZE §5.7 [F] | 唯一存储，不引入第二套引擎 |
| 10 | 全文索引 | FREEZE §5.8 [F] | PostgreSQL FTS（`tsvector` + `ts_rank`） |
| 11 | 向量索引 | FREEZE §5.9 [F] | pgvector，P0 固定一个 Embedding 模型、一个维度 |
| 12 | Embedding Pipeline | PLAN §31-34 [S] | 经 `EmbeddingInputBuilder`，Metadata-only 字段不得进入 embedding |
| 13 | `content_hash` | PLAN §23 [S] | 保存但 P0 不做增量；允许整体删除后全量重建 |

### 2.3 Retrieval（检索）

| # | 功能 | 来源 | 验收要点 |
|---|---|---|---|
| 14 | Vector Retrieval | DEF §27-6 [F] | 可独立运行，返回 Top-K + similarity |
| 15 | Keyword Retrieval | DEF §27-7 [F] | 可独立运行，返回 Top-K + rank |
| 16 | RRF 融合 | FREEZE §5.10 [F] | `score = Σ 1/(k + rank)`，k=60 起步，不做加权求和 |
| 17 | 检索结果可观测 | PLAN §54 / R-RAG §5.8 [S] | 每条结果返回 `fts_rank` / `vector_rank` / `rrf_score` |

### 2.4 Evaluation（评测）

| # | 功能 | 来源 | 验收要点 |
|---|---|---|---|
| 18 | Benchmark（30 条 Query） | FREEZE §5.11 [F] | 覆盖 CODE / DOC / MIXED，含 Ground Truth |
| 19 | Recall@3 / Recall@5 | FREEZE §5.11 [F] | 阈值 = 0 取完整排名后再截断计算 |
| 20 | MRR | FREEZE §5.11 [F] | 奖励正确结果排名靠前 |
| 21 | Latency | FREEZE §5.11 [F] | 记录检索延迟 |
| 22 | 三策略对照 | DEF §24 [F] | Vector Only / Keyword Only / Hybrid 同一 Benchmark 对比 |
| 23 | Failure Analysis | PLAN §56-57 [S] | 失败案例按 Parsing → Chunk → Metadata → Retrieval 分层定位 |

### 2.5 Context & Generation（上下文与生成）

| # | 功能 | 来源 | 验收要点 |
|---|---|---|---|
| 24 | Context Builder | DEF §27 / PLAN §58-61 [F] | 去重、Token 预算、DOC/CODE 分格式、Citation 组装 |
| 25 | Citation | DEF §20 [F] | Java：`TicketService.java Lines 120-188`；Markdown：`文件名 + Section` |
| 26 | Minimal LLM Generation | PLAN §63-64 [S] | 极简 Prompt：仅依据 Context 回答、不足时明说、引用来源 |

### 2.6 Agentic（检索式 Agent）

| # | 功能 | 来源 | 验收要点 |
|---|---|---|---|
| 27 | DOC / CODE / MIXED Router | DEF §16 [F] | 初期允许 Rule-based，可区分三类查询 |
| 28 | LangGraph Basic Workflow | FREEZE §5.12 [F] | 在 Retriever + RRF + Evaluation 稳定后才接入 |
| 29 | Context Evaluation | PLAN §73 [S] | 判断当前 Context 是否足以回答 |
| 30 | limited Retry（Query Rewrite + Retry ≤ 2） | DEF §19 [F] | `MAX_RETRY = 2`；两次后明确返回"索引内容不足以可靠回答" |

### 2.7 Engineering（工程化）

| # | 功能 | 来源 | 验收要点 |
|---|---|---|---|
| 31 | Docker Compose 基础启动 | DEF §27-16 [F] | 至少能拉起 PostgreSQL + pgvector |
| 32 | README / 架构文档 | DEF §34 [F] | 围绕 Problem → Design → Experiment → Result |

---

## 3. P1 Candidates

**这是一个候选池，不代表全部都要实现。** 是否实现由 P0 的 Benchmark 失败案例决定（`Failure Case → Hypothesis → Change → Benchmark`）。

| 候选 | 解决的问题 | 来源 | 触发条件 |
|---|---|---|---|
| `pg_trgm` | CamelCase / 子串匹配弱（`Ticket` 命中不到 `TicketServiceImpl`） | FREEZE §5.8、R-CONT §4、R-RAG §13 | Keyword Only 在符号类 Query 上 Recall 明显偏低 |
| Cross-Encoder Rerank | 召回了但排序差 | FREEZE §6、R-RAG §6 | 正确 Chunk 进入 Top-20 但掉出 Top-5 |
| Incremental Indexing | 全量重建太慢 | R-CONT §7 | 单次全量 ingestion 时长影响迭代效率 |
| Oversized Method Statement Split | 超长方法 embedding 截断丢信息 | R-JAVA §13.1 | 超长方法相关 Query 命中率低 |
| Class → Method Context Expansion | METHOD Context 太局部 | R-JAVA §13.2 | MIXED 类 Query 缺少类级上下文 |
| Javadoc Weight Experiment | 中文 Javadoc 可能显著提升语义召回 | R-JAVA §13.4 | 含/不含 Javadoc 的对照实验 |
| Phrase / NGram Search | 多词标识符匹配优先级 | R-RAG §13 | 含多个 token 的标识符查询失败 |
| Better Query Rewrite | Retry 后仍未召回 | PLAN §76 | Retry 无效案例占比较高 |
| 多 Chunk 策略 A/B（DOC 侧） | Markdown 单 Section 过长 | R-RAG §2 | oversized Section 占比高 |

---

## 4. Non-goals

以下能力**明确不进入 P0**，且后续 AI Agent 不得主动添加（DEF §29、FREEZE §6）。

### 4.1 解析与语义层

```text
SymbolSolver
Reference Resolution
Call Graph
Inheritance Graph（跨文件）
完整 Java 类型推断
多语言 Parser（Python / Go / JS ...）
tree-sitter
ANTLR
正则解析 Java
LLM Parsing
```

### 4.2 检索层

```text
GraphRAG
Knowledge Graph
复杂 Parent-Child Chunk
LLM Auto-keyword
LLM Auto-question
Cross-language Retrieval
复杂 Rerank Pipeline（P0）
raw score weighted sum
多 Vector Database（Milvus / Qdrant / Pinecone / Chroma / Weaviate）
多 Search Engine（Elasticsearch）
```

### 4.3 生成与 Agent 层

```text
完整 Coding Agent
代码生成
代码自动修改
代码自动执行
自动 Commit
GitHub PR Agent
Multi-Agent
MCP
IDE Plugin
```

### 4.4 知识类型

```text
PDF
Word
HTML
图片
数据库知识
Web
```

> 例外：DEF §8.2 允许保留 `.yml` / `.xml` / `.lua` 的**简单文本检索扩展能力**，但前提是不影响 MVP 主线。P0 默认不纳入，如纳入需单独记录。

### 4.5 基础设施（FREEZE §13.3）

```text
Redis
Kafka
RabbitMQ
Celery
Neo4j
Kubernetes
微服务化 DevContext 自身
复杂 DDD
Repository Pattern
Unit of Work
CQRS
```

> 判定规则：任何新增技术必须先回答"P0 的哪个需求需要它？"。答不出则不加。

---

## 5. P0 Definition of Done

### 5.1 Ingestion

- [ ] 能扫描真实 my12306 代码根目录并识别全部 `.java`
- [ ] 能扫描文档根目录并识别 `.md`，且排除第三方前端库 README
- [ ] Java METHOD Chunk 可正确生成（`method_name` / `signature` / `annotations` / `start_line` / `end_line` 全部正确）
- [ ] Java CLASS Summary Chunk 可正确生成（类签名 + 注解 + 成员方法签名列表，**不含整类源码**）
- [ ] Java CONSTRUCTOR Chunk 可正确生成
- [ ] Interface / 抽象方法（无方法体）不被遗漏
- [ ] Markdown Section Chunk 可正确生成（`heading_path` 正确）
- [ ] Citation 行号准确（`content` 与 `start_line`/`end_line` 指向的原始源码一致）
- [ ] Parse Failure 不导致整个 ingestion 失败，且被记录
- [ ] 产出 `ingestion_report.json`（文件数 / Chunk 数 / 失败数）

### 5.2 Storage

- [ ] `knowledge_chunk` 表建立，DOC / CODE 共用
- [ ] pgvector 可用，向量检索返回正确结果
- [ ] FTS 可用，`tsvector` 字段加权生效
- [ ] `content_hash` 已保存
- [ ] 整体删除后全量重建可重复执行且结果一致

### 5.3 Retrieval

- [ ] Vector Only 可独立运行
- [ ] Keyword Only 可独立运行
- [ ] Hybrid + RRF 可运行
- [ ] 每条结果返回 `fts_rank` / `vector_rank` / `rrf_score`

### 5.4 Evaluation

- [ ] ≥ 30 条 Benchmark Query
- [ ] CODE / DOC / MIXED 三类均覆盖
- [ ] 每条 Query 有 Ground Truth（CODE：file_path + class + method + line；DOC：file_path + heading_path）
- [ ] 可计算 Recall@3 / Recall@5 / MRR / Latency
- [ ] 三种 Retrieval Strategy 有同口径对照结果
- [ ] 评测调用的是**生产同一条** Retriever 代码路径
- [ ] 评测前不按相似度阈值过滤

### 5.5 Context & Generation

- [ ] Context Builder 可组装带 Citation 的 Context
- [ ] Context 有 Token Budget 约束
- [ ] DOC / CODE 有各自的格式化模板
- [ ] LLM 基于 Context 生成 Answer
- [ ] Answer 携带 Citation
- [ ] Context 不足时能明确表达"索引内容不足以可靠回答"

### 5.6 Agentic

- [ ] DOC / CODE / MIXED Router 可区分三类查询
- [ ] LangGraph Workflow 可完成一次完整编排
- [ ] Context Evaluation 节点可判定 Enough / Not Enough
- [ ] Query Rewrite 可执行
- [ ] Retry 上限 = 2，超限明确终止

### 5.7 Engineering & Docs

- [ ] Docker Compose 可启动 PostgreSQL + pgvector
- [ ] README 含 Problem / Design / Experiment / Result
- [ ] 架构图
- [ ] Benchmark 结果表
- [ ] 失败案例记录
- [ ] Demo 问题

---

## 6. 本轮已核实的语料与环境事实

> 本节记录**实测事实**，供 `03-development-milestones.md` 引用。这些事实改变了 DEF 中若干隐含假设，差异记入 `01-architecture-decisions.md` 的冲突章节。

### 6.1 语料现状（实测）

**代码根目录**

```text
D:\Java-learning\12306Project\12306\my12306\
```

- Maven 多模块：`services/` 下 5 个子模块 —— `gateway-services`(4)、`ticket-services`(100)、`user-services`(58)、`order-services`(38)、`pay-services`(36)
- `.java` 文件数（排除 `target/`）：**236**
- Java 版本：**21**（根 `pom.xml` `<java.version>21</java.version>`）
- Spring Boot **3.3.4**，Spring Cloud **2023.0.3**，Spring Cloud Alibaba **2023.0.3.2**
- 每个模块均含 `target/`（构建产物，必须排除）
- 最大文件：`OrderServiceImpl.java` 386 行；`TicketServiceImpl.java` 278 行

**文档根目录**

```text
D:\Java-learning\12306Project\docs\
```

- `.md` 文件数：**84**
- 其中约 30 个为第三方前端库 README（`sbadmin2` / `bootstrap` / `flot` / `metisMenu`，位于 `5-后续开发规划/baseline/results/*/`）—— **必须排除**
- 实际项目文档约 **54** 个，分为：
  - `0-历史文档/`：用户/车票/订单支付模块的功能分析与设计（19 篇）
  - `4-原项目对标学习/`
  - `5-后续开发规划/`：设计分析、实验报告、压测基线（**含 D2/D3/D4 设计分析与实测结论**）
  - `6-面试复习/`：项目全景、核心链路、技术专题、优化故事、设计与取舍
  - `my12306-项目介绍与亮点.md`

### 6.2 关键事实：代码与文档不同根

DEF §7 / §8 的隐含假设是"一个 Java Repository 同时包含 Java 与 Markdown"。**实测不成立**：

```text
12306Project/
├── 12306/my12306/      ← 只有 Java（236 个 .java，0 个 .md）
└── docs/               ← 只有 Markdown（84 个 .md）
```

这意味着 Repository Scanner 需要支持**两个独立根目录**（code root + doc root），而不是单一 `--repo`。处理方式见 `01-architecture-decisions.md` ADR-012。

### 6.3 对 DEV 有利的发现：Benchmark 题目已有真实出处

DEF / PLAN 中举例的 Benchmark Query（"为什么使用 Token Bucket"、"为什么 Feign 调用要移出事务，具体改了哪些代码"）**在文档中确实存在对应文档**：

```text
docs/5-后续开发规划/D2-设计分析-余票令牌桶与Lua.md
docs/5-后续开发规划/D4-设计分析-Feign移出事务与支付通知异步化.md
docs/5-后续开发规划/baseline/D2余票令牌桶与Lua实验报告.md
docs/5-后续开发规划/baseline/D4跨服务事务边界与可靠补偿实验报告.md
```

这正好覆盖 DEF 定义的三类信息中的 `Design Decision` 与 `Experiment / Evidence`，MIXED 类 Query 的 Ground Truth 可以直接从真实文档构造，不需要编造。

### 6.4 本轮环境（沙箱）

本轮工作在有出网白名单限制的隔离 Linux 环境中进行：

| 项 | 实测结果 |
|---|---|
| Java | OpenJDK 11（**不是** 21，不能用于运行 JavaParser 模块） |
| Python | 3.10.12 |
| Docker | 未安装 |
| psql | 未安装 |
| Maven | 未安装 |
| 出网 | 仅允许 `api.deepseek.com`；GitHub / PyPI / Maven Central 均被阻断 |

> **重要**：上述是**本轮工作沙箱**的能力，**不代表开发者本机（Windows 11）**。M1 之前需要在开发者本机确认 Java 21 / Maven / Docker / PostgreSQL 的可用性，该项列入 `02-tech-stack-todo.md` 的待确认项与 M1 的前置条件。

---

## 7. 与本文相关的待确认项

以下问题本文无法单方面确定，已登记到 `01-architecture-decisions.md`：

| 待确认项 | 位置 | Severity |
|---|---|---|
| `chunk_type` 命名（`SECTION` vs `DOCUMENT_SECTION`）与取值缺失 `CONSTRUCTOR` | Conflict 01 | MEDIUM |
| `knowledge_chunk` 字段集不足（缺 `signature` / `annotations` / `javadoc` / `title`） | Conflict 02 | MEDIUM |
| Benchmark 规模与 MIXED 配额不一致 | Conflict 03 | MEDIUM |
| Rerank 在 DEF 与 FREEZE 中的阶段归属差异 | Conflict 04 | LOW |
| "V1" 术语歧义（LangGraph DAG vs 全项目） | Conflict 05 | LOW |
| 代码根与文档根分离后的 Repository 定义 | Conflict 06 / ADR-012 | **HIGH** |
| 增量索引的机制（hash vs Git） | Conflict 07 | LOW |
| 文档根下第三方前端库的排除规则 | Risk 03 | MEDIUM |
| PostgreSQL FTS 对 CamelCase 的天然弱点 | Risk 01 | MEDIUM |
| 语料规模较小，指标统计效力有限 | Risk 04 | LOW |

**其中 Conflict 06（Repository 双根）是唯一需要在 M1 之前确认的 HIGH 项。**
