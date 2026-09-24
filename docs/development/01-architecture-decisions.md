# DevContext-Java 架构决策记录（ADR）

> 文档编号：`01-architecture-decisions`
> 阶段：M0 Project Freeze
> **本文不重新设计架构。** 它把三轮调研已经形成的结论，整理成可追溯、可反驳的决策记录。

---

## 0. 阅读约定

### 0.1 状态取值

| Status | 含义 |
|---|---|
| `FROZEN` | 已由 `开发前任务冻结.md` §5 或项目定义明确冻结，不得自行修改 |
| `PROPOSED` | 由本轮从调研资料推导，尚无实验依据，等待后续验证或推翻 |
| `DEFERRED` | 明确后移到 P1 / P2 |

**注意**：本文不把任何 `PROPOSED` 伪装成 `FROZEN`。凡是没有充分依据的，一律标 `PROPOSED`。

### 0.2 ADR 结构

```text
Decision     最终结论
Context      这个问题为什么会出现
Reason       为什么这么选（证据来源）
Alternative  还存在哪些候选
Why Not      为什么没选它们
Status       FROZEN / PROPOSED / DEFERRED
```

---

## ADR-001 Java 解析器选型

### Decision

采用 **JavaParser**，且**只引入 `javaparser-core`**。

### Context

DevContext 的 Java Chunk 需要按 Package / Class / Interface / Method / Constructor / Annotation 的语法边界切分，并保留 `class_name` / `method_name` / `annotations` / `signature` / `start_line` / `end_line` 等强类型 metadata（DEF §10）。

用一个真实仓库验证这个需求时，会立刻遇到：Java 大括号可嵌套、字符串字面量与注释里可以包含任意字符、泛型与注解让"找方法边界"必须真正解析语法。正则或固定长度切分在这个问题上不可行。

### Reason

1. **DevContext 是 Java Only。** Continue 之所以用 tree-sitter，是因为它要支持 20+ 语言（`core/util/treeSitter.ts` 的 `supportedLanguages` 表），代价是语法节点退化为弱类型字符串（`core/indexing/chunk/code.ts` 里满是 `node.type` 字符串判断）、每种语言要单独写 `.scm` 查询文件、要加载 wasm parser。DevContext 收窄到 Java 后，这些代价完全没有必要承担。（R-CONT §10、R-JAVA §1.2）
2. **JavaParser 提供强类型 AST。** `MethodDeclaration` / `ClassOrInterfaceDeclaration` / `AnnotationExpr` 是真正的 Java 类，有 getter 与编译期检查。抽取 Chunk 的代码是 `cu.findAll(MethodDeclaration.class)`，而不是字符串匹配。
3. **`javaparser-core` 零运行时依赖。** `javaparser-core/pom.xml` 的 `<dependencies>` 段为空。这与 DEF §25"Java Parser 作为独立 CLI，输出 JSON chunks"的设计完全契合——只需一个 jar，打包部署成本接近零。（R-JAVA §1.3）
4. **能力接口（mixin）设计降低了抽取器的复杂度。** `getAnnotations()` 定义在 `BodyDeclaration` 上，对所有 body declaration 统一可用；`getRange()` 定义在 `Node` 上，所有节点统一可用（R-JAVA §2.1）。抽取器可以针对"能力"编程，而不是针对"节点类型"编程。

### Alternative

1. tree-sitter（Continue 的方案）
2. ANTLR 自建 Java 语法
3. 正则 / 字符串匹配
4. 交给 LLM 解析

### Why Not

- **tree-sitter**：Java-only 场景下，强类型 AST 严格优于弱类型节点字符串。Continue 用它是多语言的必然代价，不构成 DevContext 的理由。（R-JAVA §15.7）
- **ANTLR**：需要自己维护 Java 语法文件并手写 AST 遍历，工作量与出错面都远大于引入一个成熟库；且 DevContext 的学习目标是 Context Retrieval，不是 Parser 实现。
- **正则**：Java 语法无法用正则正确切分（嵌套大括号、泛型、字符串字面量），会产出错误的 `start_line`/`end_line`，而 Range 是 Citation 的基础。
- **LLM Parsing**：不确定、不可复现、成本高，且违反 FREEZE §4.3 已形成的方向。

### Status

`FROZEN`（FREEZE §5.4）

---

## ADR-002 Java Chunk 粒度

### Decision

```text
METHOD       → 主要检索单位（一个方法 = 一个 Chunk）
CLASS        → 摘要型 Chunk（不是整类源码）
CONSTRUCTOR  → 独立 Chunk
```

### Context

Chunk 粒度决定了"系统认为什么是一份可以独立理解和检索的知识单元"（R-CONT §3）。粒度选错，后面 Embedding、Retriever、Reranker 再强也只能补救。

### Reason

**为什么 METHOD 是主体：** 一个方法通常对应一个完整业务行为（`purchaseTicket`），并且 JavaParser 能天然给出它的完整边界（`getRange()`）与语义标签（方法名、签名、注解）。它是"相对精准 + 相对完整"的语义单元，因此 RAGFlow 面对的"段落太长 / 太短"矛盾在 Java 侧**天然不存在**（R-RAG §9.5）。

**为什么还需要 CLASS，且必须是摘要：** "TicketServiceImpl 负责什么"无法通过检索单个方法回答——类级注解（`@Service` / `@RestController` / `@FeignClient`）与继承关系（`getExtendedTypes()` / `getImplementedTypes()`）只存在于类上。

但**不能把整类源码当一个 Chunk**：

1. 一个 500 行的类作为单 Chunk，其 embedding 是全类语义的平均，对任何具体问题都"有点像但都不准"；
2. 它会与类内每个 Method Chunk 内容重叠，导致检索结果中同一段代码反复出现；
3. 它会挤占 Context Token 预算。

因此 CLASS Chunk 采用：

```text
类注解 + 类声明 + extends / implements + 成员方法签名列表
```

这样 CLASS 与 METHOD 形成分工：CLASS 是地图（"这个类提供哪些能力"），METHOD 是具体地点（"某个功能怎么实现"）。（R-JAVA §13.2、R-JAVA §22）

**为什么 CONSTRUCTOR 要独立：** 构造器没有返回类型，`getType()` 不存在，与 METHOD 的字段集不同。混在一起会让 `method_name` 字段语义污染（构造器的名字恰好等于类名）。DEF §10 明确把 Constructor 列为要识别的节点。（R-JAVA §5.3）

### Alternative

1. 只存 METHOD
2. METHOD + 完整 Class 源码
3. 按固定 token 数切分（512 / 1024）
4. Parent-Child（Class 为父、Method 为子）

### Why Not

- **只存 METHOD**：丢失"类是什么"这一层。MIXED 场景问"为什么 Feign 调用要移出购票事务"时，设计文档提到的是类名 `TicketServiceImpl`，而答案在某个具体方法里；没有类级 Chunk 就缺一跳桥梁。
- **完整 Class 源码**：见上，污染检索语义并与 Method 内容重叠。FREEZE §5.5 明确禁止。
- **固定 token 切分**：正是本项目要避免的做法——会破坏方法结构，且产生多个行号范围重叠的 Chunk，使 RRF 融合与 Evaluation 变复杂。
- **Parent-Child**：RAGFlow 的父子 Chunk（`children_delimiters` + `mom`）解决的是"段落太长/太短"，代价是同一段文本存两遍、embedding 成本翻倍。Java 已经有 Method 这个天然粒度，V1 不需要。（R-RAG §9.5）

### Status

`FROZEN`（FREEZE §5.5）

---

## ADR-003 Java Chunk 的 content 来源

### Decision

```text
content = Method Range + 原始 Source File 行切片
```

不使用 `Node.toString()`，也不使用 `LexicalPreservingPrinter`。

### Context

有了 `start_line` / `end_line` 之后，还需要 `content`。JavaParser 提供了几种方式，但它们**行为不同**，选错会让 Citation 失效。

### Reason

**`toString()` 会重新格式化。** `Node.toString()` 走 `PrettyPrintVisitor`（`printer/PrettyPrintVisitor.java:1092`）。语义不变，但缩进、空行、行内注释位置都可能改变。后果是 `content` 与 `start_line`/`end_line` 指向的原始文本**不一致**，进而：

- Citation 里的行号范围与用户实际看到的代码对不上；
- Evaluation 中"检索到的 Chunk 是否命中 Ground Truth"无法用行号核验。

**Range 切片天然自洽。** JavaParser 的 `Position` 是 1-based（`Position.java:29`），`Range` 是闭区间（`Range.java:26`）。直接：

```java
String[] lines = fileContent.split("\n", -1);
Range r = method.getRange().orElseThrow();
String content = String.join("\n",
        Arrays.copyOfRange(lines, r.begin.line - 1, r.end.line));
```

结果是 `content` 与 `[start_line, end_line]` **天然一致**，且只需几行代码。Continue 的 Chunk 同样是 `content + startLine + endLine` 三元组、不做任何重新格式化，两者做法一致。（R-JAVA §9、R-CONT §8.4）

**一个必须知道的好消息**：注解**在** Method 的 Range 内。`MethodDeclaration` 的产生式把 `begin` 继承自修饰符/注解的最早位置（`java.jj:2254-2290`、`1355-1401`），所以 `Lines 120-188` 会自然覆盖 `@Transactional` 行。

**一个必须知道的前提**：Range 来自 tokenRange，因此**绝不能调用 `setStoreTokens(false)`**——它会同时让所有 `getRange()` 返回空，并连带关闭注释归属（`ParserConfiguration.java:435-441`）。配置必须保持 `storeTokens = true`、`attributeComments = true`。

### Alternative

1. `Node.toString()`
2. `LexicalPreservingPrinter.print(node)`
3. `javaparser-core-serialization` 输出 JSON 再还原

### Why Not

- **`toString()`**：见上，与行号不一致，破坏 Citation 与 Evaluation。
- **`LexicalPreservingPrinter`**：它能保留原始词法，但它是为"修改 AST 后只改动那一处、其余原样保留"这一**写场景**设计的。DevContext 是只读检索系统，引入它是过度设计。（R-JAVA §15.3）
- **`javaparser-core-serialization`**：它序列化的是完整 AST 节点树，而 DevContext 需要的是**扁平的 Chunk 列表**（每个 Chunk 带 metadata）。结构不匹配，用它只是把"抽字段"这一步推后。（R-JAVA §15.4）

### Status

`FROZEN`（FREEZE §4.2 / §5.6 的 Content 原则）

---

## ADR-004 Markdown Chunk 策略

### Decision

采用 **Heading-aware Chunk**：优先依据 `#` / `##` / `###` 的文档结构切分，不默认使用统一固定字符数切分。

超长 Section **先标记 `oversized`**，P1 再考虑二次切分。

### Context

Markdown 的自然语义单元是"章节"，不是"字符数"。RAGFlow 用 14 个 chunker（`parser_id` → `rag/app/*.py`）证明了一个结论：**不存在一种 Chunk Size 能适用于所有文档类型**——问答对、表格行、幻灯片、法律条款的"语义完整单元"根本不是同一个东西。它的通用路径看起来有 `chunk_token_size = 512`，但那只是 General 类型的默认值，不是全局方案。（R-RAG §2.1、§2.2）

### Reason

1. **结构优先是成熟系统的共同选择。** Continue 的 `markdownChunker`（`core/indexing/chunk/markdown.ts:61-147`）就是：整篇不超限则整体一个 Chunk；否则按 hLevel 递归切分；递归时**子 Chunk 重新拼上父 header**。这正好对应 DevContext 的 `heading_path` 字段。（R-CONT §3.5）
2. **`heading_path` 比 Continue 的 `title`/`fragment` 更结构化。** Continue 把标题清洗后塞进 `otherMetadata`；DevContext 用 `heading_path` 保存完整层级（`购票系统 / 事务设计 / 为什么缩小事务边界`），既可用于检索，也可用于 Citation 展示。
3. **DEV 的文档语料结构清晰。** 实测文档目录（`docs/`）是按模块和主题组织的，标题层级规整，Heading 边界是可靠的切分依据。

### Alternative

1. 统一固定字符数 / token 数切分
2. Markdown → 纯文本后按句子切
3. Heading-aware + 超长 Section 立即二次切分（P0 就做）

### Why Not

- **固定长度**：会切断章节，使 Chunk 失去标题上下文。这与 ADR-002 中"代码不能乱切"是同一类错误。
- **纯文本 + 句子切分**：丢弃文档结构，`heading_path` 无法生成，Citation 退化为"某文件某行"，用户难以验证。
- **P0 就做二次切分**：PLAN §26 明确"先标记 oversized，P1 再考虑"。P0 优先保证边界正确与链路完整，而非极端情况的召回质量。

### Status

`FROZEN`（FREEZE §5.6）

---

## ADR-005 存储选型

### Decision

**PostgreSQL + pgvector**，单一存储同时承担 Metadata、全文检索、向量检索、评测数据。

### Context

一个 Context Retrieval 系统天然需要多种"索引"：Metadata 过滤、Keyword 检索、Vector 检索，以及评测结果存储。直觉方案是为每类需求引入专门的系统。

### Reason

DEF §12 给出了核心理由：一套数据库即可承载 Metadata / Vector / Keyword Search / Evaluation Data，避免在 10～15 天内同时维护 MySQL + Milvus + Elasticsearch + Redis 等多套基础设施。

**Continue 的反例恰好说明了这一点。** Continue 同时用 SQLite FTS5 + LanceDB（R-CONT §4、§5），但那是**本地 IDE 插件**的必然选择：无服务端、离线优先、单用户、要避免任何独立进程 → 嵌入式引擎是唯一合理选择。DevContext 是服务端系统，且 PostgreSQL 已经能同时提供 FTS 与 vector，引入第二个存储引擎只会增加运维面，不带来检索质量提升（R-CONT §10.3）。

**一个具体收益：Chunk 与两种索引在同一行上。** Continue 的 FTS 复用 `chunks` 表，但向量索引反而用 `maxEmbeddingChunkSize` **独立重新切分**（`LanceDbIndex.ts:171-196`），导致同一份代码存在两套不对齐的 Chunk。DevContext 用单表后，`chunk_id` 是 RRF 融合时的天然对齐键——这是"单表双索引"相对"每 Index 一套存储"最实际的收益（R-CONT §2.4、§11）。

### Alternative

1. MySQL + Milvus / Qdrant / Chroma
2. Elasticsearch（FTS）+ 独立向量库
3. SQLite FTS5 + LanceDB（照搬 Continue）
4. 纯内存（numpy 暴力检索）

### Why Not

- **MySQL + 独立向量库**：两套存储 = 两套部署、两份一致性、两处备份。P0 规模（约 236 个 Java 文件、数十个 md）完全不需要分布式向量库。
- **Elasticsearch**：FTS 能力过剩（BM25 调参、分片、集群），而 DevContext 的全文检索需求是"标识符精确匹配 + 少量字段加权"，PostgreSQL FTS 足够。FREEZE §5.7 明确禁止替换。
- **SQLite + LanceDB**：这是"本地 IDE 单机"的解法，与 DevContext 的服务端定位不匹配；且需要自行实现两套存储的一致性。
- **纯内存**：无法持久化、无法用 SQL 做 FTS、无法承载评测数据。

### Status

`FROZEN`（FREEZE §5.7）

---

## ADR-006 Keyword Retrieval 实现

### Decision

**PostgreSQL Full-Text Search**（`tsvector` + GIN 索引 + `ts_rank`）。

`pg_trgm` 列为 P1 候选，用于代码 Identifier / CamelCase / 子串检索。

### Context

代码项目里存在大量**精确标识符**查询：`RDelayedQueue`、`PurchaseTicketReqDTO`、`TicketServiceImpl`、`purchaseTicket`、`@Transactional`。这类查询要的不是"意思像不像"，而是"哪个 Chunk 真的出现了这个字符串"。

RAGFlow 官方文档在讲检索参数时直接点名了这一场景（`search_settings.md:55`）：

> Decrease the vector weight: Gives greater priority to full-text matching. This is suitable for **exact matches involving proper nouns, product models, identifiers, fixed terminology**, and similar content.

而 Continue 的官方文档在描述 `@Codebase` 原始设计时写明，代码检索是 "**a combination of embeddings-based retrieval and keyword search**"——两者是互补而非替代关系。（R-CONT §4）

### Reason

1. **不引入第二套引擎。** PostgreSQL 已在技术栈内（ADR-005），FTS 是它的内建能力。引入 Elasticsearch 只为做 keyword retrieval 是明显的过度投入。
2. **字段加权可以直接用 `setweight` 复现。** RAGFlow 用 `query_fields` 的 boost 语法表达"哪些字段更重要"：`important_kwd^30 / question_tks^20 / title_tks^10 / content_ltks^2`（`rag/nlp/query.py:32-40`）。DevContext 可以用 A/B/C 三档表达同样的分层：

```sql
to_tsvector('simple',
    setweight(to_tsvector(coalesce(method_name,'')), 'A') ||
    setweight(to_tsvector(coalesce(class_name,'')),  'A') ||
    setweight(to_tsvector(coalesce(annotations,'')), 'B') ||
    setweight(to_tsvector(coalesce(signature,'')),   'B') ||
    setweight(to_tsvector(coalesce(content,'')),     'C')
)
```

PostgreSQL 只有 A/B/C/D 四档，比 RAGFlow 的连续权重粗，但对本项目的规模足够。（R-RAG §3.2）

3. **DevContext 的 ts_rank 严格优于 RAGFlow 的 term 相似度。** RAGFlow 最终排序用的不是 BM25，而是自算的"query 词覆盖度"`s / q`——**没有 IDF、没有文档长度归一化**（R-RAG §5.4）。PostgreSQL 的 `ts_rank` 是带 IDF 的 BM25 类分数，更成熟。
4. **不需要 RAGFlow 的第二套加权。** RAGFlow 在召回侧（引擎 boost）与精排侧（token 列表重复 `important*5/question*6/title*2`）各做了一套字段加权，因为它两套打分实现不同。DevContext 只有一套 `tsvector`，字段加权只需做一次。（R-RAG §5.5）

**必须配 `simple` 字典，不能用默认的语言字典。** 默认分词器会把 `purchaseTicket` 切成 `purchase` + `ticket`，或用词干化破坏标识符。这一点 RAGFlow **给不出参考**——它的 tokenizer 是为自然语言设计的（R-RAG §5.9）。

### Alternative

1. `pg_trgm`（P0 就上）
2. Elasticsearch / OpenSearch
3. RAGFlow 式的应用层自研 tokenizer + 空格分析器
4. 简单 `LIKE '%keyword%'`

### Why Not

- **P0 就上 `pg_trgm`**：先用 FTS 跑通、观察真实失败案例，再决定是否需要 trigram。PLAN §40 明确要求"不要 Day 4 就立刻扩展"，FREEZE §5.8 将 `pg_trgm` 列为 P1。**先建立 Baseline 再优化**——否则无法量化 trigram 带来的收益。
- **Elasticsearch**：见 ADR-005。
- **自研 tokenizer**：RAGFlow 这样做是为了让 6+ 个 doc engine（ES / Infinity / OceanBase / SeekDB / SereneDB / GaussDB）行为一致并支持中日韩分词。DevContext 只有一个存储，承担这个维护成本没有收益。（R-RAG §9.2）
- **`LIKE`**：无法排序（没有相关性分数）、无法加权、全表扫描。

### Status

`FROZEN`（FREEZE §5.8）

---

## ADR-007 多路检索结果融合

### Decision

**RRF（Reciprocal Rank Fusion）**，`score(d) = Σ 1/(k + rank_i(d))`，k 起步取 60。

明确**不采用** raw score weighted sum。

### Context

拿到两条排序之后，必须回答"最终给 LLM 哪几个"。最直觉的办法是直接比较分数，但：

```text
Vector similarity = 0.86        （[0,1] 的余弦相似度）
PostgreSQL ts_rank = 7.2        （另一个评分空间）
```

`7.2 > 0.86` 不代表后者更相关。两者不是同一种评分体系，直接比较没有意义。

### Reason

**RRF 用"排名"而不是"分数"融合，因此不需要归一化。** 它相信一个信号：**多个 Retriever 一致认为某个结果重要，这件事本身就是强信号**。

例如 `purchaseTicket` 的 METHOD Chunk 在 Keyword 排第 1、Vector 排第 3；而某个文档 Chunk 在 Keyword 排第 20、Vector 排第 1。前者在两路都表现不错，RRF 会把它排到更高。

**RAGFlow 的反例为这个选择提供了最直接的论据。** RAGFlow 用的是 `weighted_sum`（`build_fusion_expr`，`rag/nlp/search.py:37-44`）：

```python
term_similarity_weight = 1 - vector_similarity_weight   # 默认 0.7
return FusionExpr("weighted_sum", topn,
                  {"weights": f"{term_similarity_weight:g},{vector_similarity_weight:g}"})
```

它**之所以能**加权求和，是因为它主动消除了分数空间不一致——靠三种手段：自算与余弦同尺度的 term 分、在 Infinity 引擎内做逐路归一化、以及在纯 term 检索时把阈值置零。最后一条的源码注释是最有力的证据（`rag/nlp/search.py:850-851`）：

> `# When vector_similarity_weight is 0, similarity_threshold is not meaningful for term-only scores.`

**RAGFlow 自己承认：同一个相似度阈值不能同时适用于两种分数空间。** 这正是"两个 score 不可直接比较"的另一种表述。RAGFlow 的应对是"额外做归一化让两者可比"；DevContext 的应对是"用排名融合，从而不需要归一化"。（R-RAG §5.2）

**取舍必须写清楚：**

| | RAGFlow（weighted sum） | DevContext（RRF） |
|---|---|---|
| 需要什么 | 可连续调节的向量权重（产品需要用户拖参数） | 可解释、可归因的融合结论（学习/评测项目） |
| 代价 | 必须做归一化，且归一化方式影响阈值语义 | 放弃连续调权 |
| 参数量 | 1 个连续权重 + 阈值 | 1 个 k |

**这不是"RRF 更先进"，而是目标不同导致工程选择不同。**

### Alternative

1. raw score weighted sum
2. 按配额切片拼接（Continue 的做法：1/4 最近编辑 + 1/4 FTS + 1/2 embedding，`NoRerankerRetrievalPipeline.ts:12-19`）
3. 只取两路交集
4. 学习式融合（LTR）

### Why Not

- **weighted sum**：需要先解决归一化，而归一化会破坏绝对阈值的语义；且 P0 只有两路召回，引入连续权重会让"哪一路起作用"难以归因。FREEZE §5.10 明确禁止改用 weighted sum。
- **配额拼接**：Continue 的方案解决了"每路都要有代表"，但**没有解决"哪条更相关"**——不同来源的相对排序没有被融合，只是拼接后按位置去重（`retrieval/util.ts:4-12`）。Continue 也没有做 RRF 或分数归一化。（R-CONT §8.2）
- **只取交集**：会显著降低 Recall——两路各自的长尾命中会被丢弃，而这恰恰是混合检索的价值所在。
- **LTR**：需要大量标注数据训练，30 条 Query 的规模完全不支持；且会失去可解释性。

### Status

`FROZEN`（FREEZE §5.10）

---

## ADR-008 Evaluation 独立于生成

### Decision

Retriever 必须**单独** Benchmark，不与 LLM Generation 混在一起评测。

指标：`Recall@3` / `Recall@5` / `MRR` / `Latency`。评测调用**生产同一条** Retriever 代码路径，且**不按相似度阈值预过滤**。

### Context

"最终答案不好"是一个无法定位问题的描述。它可能是：

```text
A. Retriever 根本没找到正确 Chunk          → 检索侧问题
B. Retriever 找到了，但排在第 20，没进 Context → 排序侧问题
C. 正确内容进了 Context，但 LLM 理解错了     → 生成侧问题
```

只看最终 Answer，这三类无法区分。

### Reason

1. **RAGFlow 官方给出了同样的分层诊断逻辑**（`retrieval_testing.md:16`）：

> If the correct chunk can already be recalled but the final answer is still unsatisfactory, further check the model, prompt, or application configuration. **If the target chunk is not recalled, continue checking document parsing, chunks, metadata, and retrieval parameters.**

并给出排查顺序：`document parsing → chunk → metadata → retrieval parameters`。DevContext 的失败案例分析应按同一顺序组织。（R-RAG §7.1）

2. **评测必须调用生产 Retriever。** RAGFlow 的离线 benchmark（`rag/benchmark.py:52-66`）调用的是 `settings.retriever.retrieval`，即线上同一个函数。如果评测另写一条简化路径，测出来的数字不代表线上行为。（R-RAG §7.4）

3. **评测前必须用阈值 0 取完整排名。** 如果正确 Chunk 排在第 4，但因 `similarity < 0.5` 被过滤，你无法区分"搜索本身没找到"和"阈值把它删了"。RAGFlow benchmark 传 `similarity_threshold=0.0` 正是为此。PLAN §51 也要求 `threshold = 0`。（R-RAG §13.8）

4. **Continue 的空白反证了这一层的重要性。** 在 Continue 的仓库快照中未找到检索质量评测代码（grep `recall@` / `MRR` 无结果），官方文档也没有给出"混合检索相对纯向量提升多少"的量化对比。DEV 自建 Benchmark 正是要补上这个缺口。（R-CONT §8.7）

**一个必须写进 README 的诚实说明**：RAGFlow 的 Retrieval Test 在产品层面**不产出量化指标**（人工目测），量化评测在另一个独立离线脚本里，且面向公开数据集（MS MARCO / TriviaQA / MIRACL）计算 nDCG@10 / MAP@5 / MRR@10。DevContext 的目标不同——不是"在公开榜单上表现如何"，而是"**这次改动有没有让 my12306 上的检索变好**"。因此需要自己的 30 条带 Ground Truth 的问题，而不是 nDCG@10。（R-RAG §7.4、§9.10）

### Alternative

1. 只做端到端问答评测（LLM-as-Judge）
2. 照搬 RAGFlow 的公开数据集 benchmark（nDCG@10 / MAP@5）
3. 纯人工目测（RAGFlow Retrieval Test 的形态）
4. 不做评测，凭主观感受

### Why Not

- **端到端 LLM-as-Judge**：把检索误差与生成误差混在一起，无法归因；且 DEF §23 明确"不要求第一版实现复杂 LLM-as-Judge"。
- **公开数据集 benchmark**：衡量的不是本项目要证明的东西。DevContext 的假设是"结构化 Chunk + 混合检索对**这个真实 Java 项目**有效"，只能用这个项目的数据测。
- **纯人工目测**：可作为辅助调试手段（值得保留），但不能作为"Hybrid 比 Vector Only 更好"的证据。
- **不评测**：DEF §21 明确"本项目不得在没有 Evaluation Dataset 的情况下宣称 Hybrid Search improves accuracy"。

### Status

`FROZEN`（FREEZE §5.11、DEF §21）

---

## ADR-009 SymbolSolver

### Decision

**P0 不做。** 仅引入 `javaparser-core`，不引入 `javaparser-symbol-solver-core`。

归属 P2。

### Context

`JavaParser` 的 AST 回答"代码长什么样"；`SymbolSolver` 回答"这个名字指向哪个声明"。例如：

```java
TicketService ticketService;
ticketService.purchaseTicket();
```

AST 能看到存在一个方法调用 `purchaseTicket`，但不一定知道它最终绑定到哪个实现。

### Reason

1. **前置配置成本高。** SymbolSolver 必须配置 TypeSolver 才能工作：JDK 反射、项目源码目录、依赖 jar、classloader……对一个 Maven 多模块项目（my12306 有 5 个模块），要正确解析跨模块引用需要把所有模块源码路径与依赖 jar 都喂进去。这与 10～15 天 MVP 约束直接冲突。（R-JAVA §11.3）
2. **引入额外依赖。** `javaparser-symbol-solver-core` 依赖 javassist / guava / checker-qual，而 `javaparser-core` 是零依赖的。ADR-001 选择 core 的"零依赖打包"优势会被破坏。
3. **额外错误面。** 解析失败时抛 `UnsolvedSymbolException`。在一个必然存在解析不全文件的真实仓库里，需要大量 try/catch 兜底。
4. **P0 的检索质量不依赖它。** P0 需要证明的是"结构感知的 Chunk 比朴素文本 Chunk 更适合代码检索"——这一点只用 AST 就能验证。DEF §11 给出的演进路径（V1 AST Chunk → V2 Symbol Resolution → V3 Reference → V4 Call Graph）也把 SymbolSolver 放在 V2。

**一个可选的折中（P1）**：只配 `ReflectionTypeSolver`（仅解析 JDK 类型，不解析项目内类型），成本低，可解决 `List` / `Optional` 这类 JDK 类型的部分问题。仍不是 P0 必需。

### Alternative

1. P0 就引入 `javaparser-symbol-solver-core` + `CombinedTypeSolver`
2. 用 `tree-sitter` + 启发式名称匹配代替真正的符号解析
3. 用 LLM 推测调用关系

### Why Not

- **完整 TypeSolver**：成本与收益不匹配（见上）。
- **启发式名称匹配**：会产生大量错误引用，比"不解析"更糟——错误的关系图会误导检索与回答。
- **LLM 推测**：不确定、不可复现，且与"避免无法验证的模型生成"（DEF §20）冲突。

### Status

`FROZEN`（FREEZE §6 Non-goals、§5.4；DEF §11）

---

## ADR-010 Reranker

### Decision

**P0 不做。P1 根据 Benchmark 结果决定是否引入。**

### Context

第一阶段检索（FTS / Vector）便宜但对 Query-Chunk 关系的理解有限；Cross-Encoder 可以同时看 `Question + Chunk`，判断更准，但每个候选都要跑一次模型，无法对全库使用。

### Reason

**核心是实验顺序，不是技术能力。** 如果一开始就加入 Reranker，最终指标变好时你无法判断是 RRF 起作用还是 Reranker 起作用——这会破坏 Evaluation 的可解释性。正确的顺序是：

```text
Baseline 1   Vector Only
Baseline 2   Keyword Only
Experiment   Keyword + Vector + RRF
P1           + Reranker
```

每一步都能量化归因。（R-RAG §6.2、§13.2）

**触发条件明确：** 只有当 Baseline 数据显示"正确 Chunk 进入了 Top-N（召回足够）但掉出 Top-K（排序不足）"时，Rerank 才有明确的优化目标。如果失败原因在召回侧（根本没找到），Rerank 无效。

### Alternative

1. P0 就上 Cross-Encoder Rerank
2. 用 LLM 做精排
3. 用规则精排（符号命中提权）

### Why Not

- **P0 就上**：破坏归因（见上）；且 FREEZE §6 明确列为 P1 候选。
- **LLM 精排**：延迟与成本高，且引入 LLM 不确定性；P0 的 Evaluation 目标是"检索能力"而非"生成能力"。
- **规则精排**：这本质上属于"字段加权"，已经包含在 ADR-006 的 `tsvector` 权重里，不需要单独一层。

### Status

`DEFERRED`（FREEZE §6）

---

## ADR-011 LangGraph 接入时机

### Decision

LangGraph **不是第一开发阶段**。必须在以下全部可运行之后才接入：

```text
Parser
Storage
Keyword Retriever
Vector Retriever
RRF
Evaluation
```

### Context

LangGraph 的价值在于编排**决策、状态、重试、循环**。如果底层还没有可编排的东西，引入它只会把问题复杂化——后期出问题时无法判断是 Parser / Chunk / Retriever / Router / Prompt / LLM 哪一层出错。

### Reason

1. **先做 Retrieval，再做 Agent。** PLAN §4.1 明确规定了顺序：`Parsing → Chunking → Storage → Retrieval → Evaluation → Generation → Agentic Routing`。反过来（`LangGraph → Agent → 再回来补 Retriever`）会让故障定位失效。
2. **简单链路根本不需要 LangGraph。** 如果只是 `Question → Embedding → Search → Answer`，普通函数调用就够了。真正需要 LangGraph 是出现了 `Query Type / Routing / 多 Retriever / Context Evaluation / Retry / Query Rewrite / State` 之后。（R-RAG §43）
3. **Continue 的架构反过来印证了 Router 的价值。** Continue 的检索 pipeline 是**固定四路召回 + 配额拼接**，问题类型不影响召回策略。DevContext 的 DOC / CODE / MIXED 路由正是对"不同问题需要不同召回策略"的回答。（R-CONT §8.6）

**一个附带的重要判断**：Continue 官方已把 `@Codebase`（基于 embedding 的代码检索）标记为 deprecated，方向转向"Agent 用 grep/glob/read 工具自己探索"（`docs/reference/deprecated-codebase.mdx`）。DevContext 明确不做 Coding Agent（Non-goals），因此**不采用工具检索作为主路径**。但这个事实带来两点输入：

1. 不应宣称"索引检索是业界唯一正确答案"——把 Continue 的转向作为诚实的对照写进 README / 面试材料，反而是加分项；
2. LangGraph State 中应保留"检索充分性评估"节点，这与 Agent 迭代式检索在精神上一致，但用受控的 Retry（≤2）实现，成本可控。

### Alternative

1. 第一步就搭 LangGraph 骨架
2. 完全不用 LangGraph，用普通函数编排
3. 用 LCEL 代替 LangGraph

### Why Not

- **先搭 LangGraph**：违反 PLAN §4.1，且此时没有可编排的对象。
- **完全不用**：P0 需要 `Routing + State + Retry + Loop`，这正是 LangGraph 的适用场景；用裸函数实现这些会让状态管理散落各处。
- **LCEL**：适合固定 Pipeline；一旦出现条件分支与循环，LCEL 表达力不足。DEF §6 已说明这一分工。

### Status

`FROZEN`（FREEZE §5.12）

---

## ADR-012 Repository 根目录模型

### Decision

Repository 定义从"单一根目录"扩展为**支持两个独立根**：

```text
code_root   代码根（.java）
doc_root    文档根（.md）
```

二者同属一个逻辑 Repository（共享 `repository_id`），但**物理路径可以不同**。

### Context

DEF §7 / §8 的隐含假设是"一个 Java Repository 同时包含 Java 与 Markdown"，因此 P0 只需"导入一个 Java Repository"（DEF §27-1）。

**本轮实测发现该假设不成立**（`00-p0-scope.md` §6.2）：

```text
D:\Java-learning\12306Project\
├── 12306\my12306\   ← 236 个 .java，0 个 .md
└── docs\            ← 84 个 .md（其中约 30 个为第三方前端库 README）
```

代码与文档是**兄弟目录**，不在同一根下。如果按 DEF 的字面实现（单 `--repo` 参数），`DOC` 类与 `MIXED` 类检索将完全没有语料——而 MIXED 恰恰是 DEF §4 定义的"项目最重要的应用场景"。

### Reason

1. **不修改任何已冻结决策。** 代码仍走 JavaParser、文档仍走 Heading-aware，两者仍进同一张 `knowledge_chunk` 表。变化的只是"从哪里发现文件"这一步。
2. **改动面最小。** Scanner 接受两个路径参数，写 `file_path` 时统一相对各自根记录（并在 chunk 上保留 `source_root` 类型标记），后续 Chunk / 索引 / 检索逻辑完全不变。
3. **MIXED 是 P0 的核心场景，必须有文档语料。** DEF §22 的 Benchmark 配额中 MIXED 占比最高，没有 doc_root 就无法构造 MIXED 的 Ground Truth。
4. **现实中多根是常态。** 许多工程项目的设计文档并不放在代码仓库内（放在 wiki、独立 docs 仓、或上级目录）。支持双根反而更接近真实场景。

### Alternative

1. 只索引 `12306/my12306`，放弃 Markdown 与 MIXED（即放弃 P0 核心场景）
2. 把 `docs/` 复制/软链进 `12306/my12306` 后再索引（**会修改 my12306，违反 FREEZE §12**）
3. 以 `12306Project/` 为唯一根，靠忽略规则排除 `参考项目` 等无关目录
4. 支持任意数量的根目录列表

### Why Not

- **放弃 Markdown**：直接摧毁 P0 的 MIXED 场景与"代码 + 设计知识混合检索"的项目定位（DEF §4）。
- **复制/软链进代码仓**：FREEZE §12 明确禁止"修改 my12306"，且会污染被索引的仓库。
- **以父目录为唯一根**：技术可行，但会把 `datasource/`、`tools/`（含 RocketMQ 发行包）、`worktrees/` 等大量无关内容纳入扫描范围，忽略规则会变得复杂且脆弱；同时把"代码仓"与"文档仓"的边界模糊掉。
- **任意多根列表**：P0 只需要两个根，过早抽象会增加配置与校验的复杂度。

### Status

`PROPOSED`

> **需要人工确认。** 这是本轮唯一由实测触发的新增架构决策，`FREEZE` 未覆盖此情形。若开发者认为应改为方案 3（父目录单一根），需在 M1 之前确认，因为它直接决定 Scanner 的参数形态与忽略规则。

---

## 13. Architecture Risks

> FREEZE §13.4 允许在认为 Frozen Decision 存在严重问题时记录为 Architecture Risk，并可提供证据。**以下不是要修改冻结决策，而是记录已知风险。**

### Risk 01：PostgreSQL FTS 对 CamelCase 标识符的天然弱点

**风险描述**：P0 采用的 PostgreSQL FTS（`simple` 字典）会把 `purchaseTicket`、`RDelayedQueue`、`PurchaseTicketReqDTO` 这类 CamelCase 标识符当作**一个整体 token**（`simple` 不做词干化，这是好事），但**做不到子串匹配**——搜 `Ticket` 无法命中 `TicketServiceImpl`、`purchaseTicket`。

**为什么这是 P0 的实质风险**：DEV 的 Symbol Query 恰好以这类标识符为主（DEF §5.3、R-RAG §5.9）。Continue 的解法是 FTS5 用 **trigram 分词器**（`FullTextSearchCodebaseIndex.ts:34`），专门为了这类子串匹配，代价是索引体积更大（R-CONT §4）。

**RAGFlow 给不出参考**：它的自研 tokenizer 是为自然语言设计的，对代码标识符并不适配（R-RAG §5.9 明确说明这一点）。

**当前处理**：仍按 FREEZE §5.8 用 FTS 跑通 P0，接受这个弱点，把失败案例记录下来，由数据决定是否在 P1 引入 `pg_trgm`。

**建议**：在 Benchmark 中**专门设计一组子串类 Query**（例如"搜索包含 Ticket 的类"、"哪些方法名含 purchase"），以便尽早量化这个弱点的影响面。若这组 Query 的 Recall 明显低于其他组，`pg_trgm` 应提升为 P1 的第一优先项。

**Severity**：`MEDIUM`（不影响 P0 链路可行性，但影响 P0 主场景之一的指标下限）

---

### Risk 02：超长方法的 Embedding 截断会损失召回

**风险描述**：V1 对超长方法的处理是"整块作为一个 Chunk + 标记 `oversized` + embedding 前截断"（R-JAVA §13.1）。截断会丢失方法体后半部分的语义，导致相关查询命中率下降。

**为什么这样选择**：替代方案（P1 的语句级二次切分）会产生多个指向同一方法、行号范围重叠的 Chunk，使 RRF 融合与 Evaluation 都变复杂；且 JavaParser 不提供"只打印节点的一部分"的 API，无法复用 Continue 的"结构保真折叠"（`code.ts:110-171`）。

**缓解因素**：实测中 my12306 最大的 Java 文件是 `OrderServiceImpl.java`（386 行），最大的方法远未达到需要截断的规模。**这个风险在 P0 语料上大概率不会触发。**

**建议**：M2 阶段在 `ingestion_report.json` 中统计 `oversized` Chunk 数量与被截断的 token 数。若为 0，则该项无需进入 P1。

**Severity**：`LOW`

---

### Risk 03：文档语料含大量第三方前端库 Markdown

**风险描述**：`docs/5-后续开发规划/baseline/results/` 下包含 `sbadmin2` / `bootstrap` / `flot` / `metisMenu` 等前端库的 README（约 30 个 `.md`）。若被索引，会：

- 污染 DOC 检索结果（这些文件与项目设计完全无关）；
- 拉低 DOC / MIXED Query 的 Recall（无关 Chunk 挤占 Top-K）；
- 浪费 Embedding 调用。

**当前处理**：忽略规则必须显式覆盖这些路径。这属于 PLAN §21"需要排除的目录"的延伸——PLAN 只列了 `target/` / `.git/` / `.idea/` / `node_modules/` / `generated/`，没有覆盖"文档目录内的第三方前端库"。

**建议**：忽略规则不要只按目录名硬编码，而应支持"文档根下按路径前缀排除"（例如 `docs/5-后续开发规划/baseline/results/**`），并在 `ingestion_report.json` 中记录被排除的文件清单，便于复核。

**Severity**：`MEDIUM`（若忽略失败会直接污染评测结果）

---

### Risk 04：语料规模较小，指标结论的统计效力有限

**风险描述**：语料为 236 个 Java 文件（约 1.2 万行）+ 约 54 个有效 md。30 条 Query 的样本量下，单个 Query 的结果变化就会带来约 3.3 个百分点的 Recall 波动。

**影响**：`Hybrid vs Vector Only` 若差距在 1～2 条 Query 以内，不能据此断言"Hybrid 更好"。

**建议**：Benchmark 报告除总体指标外，必须给出**逐条 Query 的命中情况与失败分类**，让结论建立在可追溯的个例上，而不是只报一个汇总百分比。这与 DEF §24"不得提前编造实验结果"、§35"如何用实验数据证明优化有效"的要求一致。

**Severity**：`LOW`（不影响可行性，但影响结论表述的严谨性）

---

## 14. Open Questions / Conflicts

> 本节按 FREEZE §11 的要求，记录现有资料之间存在的冲突、重复定义、术语不一致。
> **不修改原文。** 冲突只被记录与建议，取舍由开发者确认。

---

## Conflict 01

### Source A

`项目定义` §13（DEF）：`chunk_type` 取值定义为

```text
DOCUMENT_SECTION
CLASS
METHOD
```

### Source B

`初步开发计划` §8（PLAN）：`chunk_type` 取值定义为

```text
SECTION
CLASS
METHOD
CONSTRUCTOR
```

`开发前任务冻结` §5.5（FREEZE）也要求 `CONSTRUCTOR` 独立 Chunk。

### Conflict

两处对同一字段的**取值集合与命名**都不一致：

1. 命名：`DOCUMENT_SECTION` vs `SECTION`
2. 取值：DEF 的列表**缺少 `CONSTRUCTOR`**，而 FREEZE §5.5 与 PLAN §8 都要求构造器独立成 Chunk

如果按 DEF 的字面实现，构造器 Chunk 将没有合法的 `chunk_type` 值可取。

### Suggested Resolution

以 **DEF 的 `DOCUMENT_SECTION` 命名**为准（语义更明确、与 `source_type = DOCUMENT` 对齐），但**取值集合补齐 `CONSTRUCTOR`**：

```text
DOCUMENT_SECTION
CLASS
METHOD
CONSTRUCTOR
```

在 `02-tech-stack-todo.md` 的 Schema 冻结项中一并确认。该决定应在 M3（建表）之前完成。

### Severity

`MEDIUM`（影响数据库 Schema，属于阻塞性细节，但不影响架构方向）

---

## Conflict 02

### Source A

`项目定义` §13（DEF）：`knowledge_chunk` 字段列表为

```text
id / repository_id / source_type / chunk_type / file_path / module /
package_name / class_name / method_name / heading_path / content /
content_hash / start_line / end_line / embedding / created_at / updated_at
```

### Source B

`初步开发计划` §6（PLAN）：在 DEF 基础上**增加**了

```text
title / signature / annotations / javadoc
```

`JavaParser调研` §8（R-JAVA）进一步要求：`signature`（用 `getDeclarationAsString()`）、`annotations`（字符串数组而非布尔标记）、`javadoc`（独立字段，不并入 `content`）都必须存在，否则检索质量与 Citation 都会受影响。

### Conflict

DEF 的字段集**不足以支撑** R-JAVA 论证过的检索设计：

- 没有 `signature` → FTS 无法对参数类型做加权（搜 `PurchaseTicketReqDTO` 会漏）
- 没有 `annotations` → 无法做"带 `@Transactional` 的方法提权"
- 没有 `javadoc` → 丢失对语义检索最有价值的自然语言描述（且 R-JAVA §13.4 指出中文 Javadoc 可能显著提升语义召回）
- 没有 `title` → DOC 侧缺少轻量字段

### Suggested Resolution

以 **PLAN §6 的字段集为准**（DEF 的列表视为早期草稿），并按 R-JAVA 的字段归属建议补充：

- `annotations` 存**字符串数组**（而非 `isTransactional` 等布尔标记），以便将来新增注解决策时不需要改 Schema
- `javadoc` 存**独立字段**，**不并入 `content`**——因为 Javadoc 不在 Method Range 内，并入会导致 `content` 与 `start_line` 不一致
- 另需考虑 `is_oversized`（超长方法标记，见 Risk 02）与 `source_root`（代码根 / 文档根标记，见 ADR-012）

完整字段清单在 M3 之前于 `02-tech-stack-todo.md` 冻结。

### Severity

`MEDIUM`（阻塞建表，且直接影响检索质量上限）

---

## Conflict 03

### Source A

`项目定义` §22（DEF）：Evaluation Dataset 目标

```text
30 ~ 50 questions
建议 DOC 10~15 / CODE 10~15 / MIXED 15~20
其中 MIXED 应占较高比例
```

同文档 §34 又写"至少 30 条测试问题"。

### Source B

`初步开发计划` §42（PLAN）：

```text
30 Questions
构成：10 CODE / 10 DOC / 10 MIXED
```

`开发前任务冻结` §5.11（FREEZE）："第一版 Benchmark：约 30 条问题"。

### Conflict

三处的规模与**配额结构**都不一致：

| 来源 | 总数 | 配额 |
|---|---|---|
| DEF §22 | 30～50 | DOC 10-15 / CODE 10-15 / MIXED 15-20（MIXED 占比最高） |
| DEF §34 | ≥30 | 未指定 |
| PLAN §42 | 30 | 10/10/10（均等） |
| FREEZE §5.11 | 约 30 | 仅要求覆盖 CODE / DOC / MIXED |

关键在于 **MIXED 的占比**：DEF 强调"MIXED 是项目最重要的应用场景"并要求其占较高比例，而 PLAN 的均等配额（10/10/10）做不到这一点。这会直接影响评测结论——如果 MIXED 只有 10 条，那么"混合检索对 MIXED 场景有效"这一核心命题的样本量就非常薄弱。

### Suggested Resolution

P0 采用 **≥30 条**为下限、以 **MIXED 优先**为配额原则：

```text
CODE    8 ~ 10
DOC     8 ~ 10
MIXED   12 ~ 15
合计    30 ~ 35
```

理由：DEF 是项目最高层约束，其"MIXED 占比应较高"的规定应优先于 PLAN 的示意性均等配额；同时 PLAN 的 30 条下限被满足。若时间紧张，可先完成 30 条，但**不应削减 MIXED 的数量**去补 CODE / DOC。

另需注意：`docs/` 中已有真实的设计分析与实验报告（`00-p0-scope.md` §6.3），MIXED 的 Ground Truth 可以直接从真实文档构造，不存在"凑不出来"的问题。

### Severity

`MEDIUM`（不影响链路可行性，但直接影响核心命题的证据强度）

---

## Conflict 04

### Source A

`项目定义` §7（DEF）的 MVP 架构图中，`RRF` 之后存在一个节点：

```text
[Optional Rerank]
```

同文档 §24 又列出第 4 组实验：

```text
如果开发周期允许：
D: Hybrid + Rerank
```

### Source B

`开发前任务冻结` §6（FREEZE）：

```text
Cross-Encoder Rerank：
P1 候选
不是 P0 必做
```

`RAGFlow调研` §6.2 / §13（R-RAG）：Rerank 归 P1，且"Rerank 与 RRF 的整合方式"被列为 P1 最需要想清楚的设计点。

### Conflict

DEF 把 Rerank 同时描述为"架构图中的可选节点"和"条件允许时的第 4 组实验"，读起来像是 P0 的边缘范围；FREEZE 则明确将其划为 P1。这个差异会让实现者不确定"要不要为 Rerank 预留结构"。

### Suggested Resolution

以 **FREEZE 为准：Rerank 属于 P1，不进 P0 范围**。

但吸收 DEF 的一个有价值的部分：**Benchmark 与报告结构应预留 Rerank 的位置**——即指标对照表预先设计为可容纳第 4 行（`Hybrid + Rerank`），Retriever 接口设计时允许在 RRF 之后插入一个可选阶段，但 P0 不实现该阶段。

理由（ADR-010 已论证）：P0 就引入 Rerank 会破坏"是 RRF 起作用还是 Rerank 起作用"的归因能力。预留接口的成本远低于事后重构。

### Severity

`LOW`（措辞与阶段归属问题，架构方向一致）

---

## Conflict 05

### Source A

`初步开发计划` §71（PLAN）：

```text
V1 暂时不要循环
第一版先完成：DAG
也就是没有：Retry / Rewrite / Loop
先保证流程稳定
```

### Source B

同文档 §72～§75（PLAN）：

```text
阶段十六：Context Evaluation + Retry
预计：Day 10
现在才开始真正进入 Agentic Retrieval
Retry 最多 2 次
```

`项目定义` §19（DEF）：`MAX_RETRY = 2`。
`开发前任务冻结` §7.2（FREEZE）：P0 Features 包含 `Limited Retry`。

### Conflict

PLAN 内部对 "V1" 一词的使用不一致：

- §71 的 "V1" 指**LangGraph 的第一版**（DAG，无循环）
- §72～§75 的 "V1"（以及 DEF/FREEZE 的 "P0"）指**整个项目的第一版**（包含 Retry）

两者其实不矛盾（先做 DAG 形态的编排，再加循环），但同一个词 "V1" 被赋予了两种范围，容易让实现者误读为"P0 最终不做 Retry"。

### Suggested Resolution

**统一术语，区分两个阶段名：**

```text
M11  LangGraph Basic Workflow   → DAG 形态（无循环）
M12  Agentic Retry              → 加入 Context Evaluation + Rewrite + Retry ≤ 2
```

即：PLAN §71 描述的是 **M11 的验收范围**，PLAN §72～§75 描述的是 **M12 的验收范围**。两者是同一里程碑序列的先后两步，P0 范围包含 M11 与 M12（与 FREEZE §7.2 / §9 一致）。

`03-development-milestones.md` 已按此方式拆分，不再使用 "V1" 指代 LangGraph 的第一版。

### Severity

`LOW`（术语歧义，M11/M12 拆分后即消除）

---

## Conflict 06

### Source A

`项目定义` §7 / §8 / §27-1（DEF）：假设"一个 Java Repository 同时包含 Java 与 Markdown"，P0 功能为"导入一个 Java Repository"。

`初步开发计划` §21（PLAN）：`Repository Scanner` 第一版只处理 `*.java` 与 `*.md`，忽略 `target/` 等。

### Source B

**实测的仓库结构**（`00-p0-scope.md` §6.2）：

```text
D:\Java-learning\12306Project\
├── 12306\my12306\   ← 236 个 .java，0 个 .md
└── docs\            ← 84 个 .md
```

代码与文档**不在同一根目录**。

### Conflict

DEF / PLAN 描述的"单仓库根"模型在真实语料上不成立。若按字面实现单 `--repo` 参数，DOC 与 MIXED 类检索会完全没有语料，而 MIXED 是 DEF §4 定义的核心场景。

这不是资料之间的矛盾，而是**资料与现实的偏差**——属于本轮工作发现的最重要的一条。

### Suggested Resolution

采用 **ADR-012（支持 code_root + doc_root 双根）**，并注意两个附带问题：

1. **`file_path` 的相对基准**：双根下应统一记录为相对各自根的路径，或用 `source_root` 字段区分，避免两条不同根的路径在同一列里语义冲突。
2. **忽略规则必须扩展**：除 PLAN §21 列出的 `target/` / `.git/` / `.idea/` / `node_modules/` / `generated/` 外，还需排除文档根下的第三方前端库（见 Risk 03）。

**该决议需要开发者确认**（ADR-012 状态为 `PROPOSED`），因为方案 3（以 `12306Project/` 为单一根 + 复杂忽略规则）也是可行的，只是取舍不同。此确认应在 M1 开始前完成，因为它决定 Scanner 的参数形态。

### Severity

`HIGH`（若不处理，P0 的核心场景 MIXED 无法评测）

---

## Conflict 07

### Source A

`项目定义` §28（DEF）：P1 可选功能列表包含

```text
Git Incremental Index
```

`初步开发计划` §23（PLAN）：P0 保留 `content_hash` 字段，但

```text
第一版允许：删除当前 repository chunks → 全量重新解析 → 重新入库
以后 P1 再实现：hash unchanged → skip
```

### Source B

`Continue调研` §7.7（R-CONT）：官方文档 `docs/guides/custom-code-rag.mdx` 明确建议

> we highly recommend first building and testing the pipeline before attempting this. Unless your codebase is being entirely rewritten frequently, an incremental refresh of the index is likely to be sufficient

并给出 P1 的四级演进路径（content_hash 比对 → 文件消失则删除 → compute/del/addTag 三态 → mtime 短路）。

### Conflict

**此处三方结论一致（都归 P1），不存在实质冲突。** 记录在此是为了澄清一个容易被误读的点：

DEF 使用的是 "**V1 不做增量**"，而 P1 列表中的措辞是 "Git Incremental Index" —— 后者暗示基于 Git 的变更检测（`git diff`），而 R-CONT 的演进路径是基于 **content hash 比对**，两者是不同机制。若实现者按"Git Incremental Index"的字面理解去做，会引入对 Git 的依赖，与"全量重建 + hash 比对"的简单路径不同。

### Suggested Resolution

明确 P1 的增量索引采用 **content hash 比对**路径（与 Continue 一致），**不引入 Git 依赖**：

```text
1. content_hash 未变的文件 → skip（最小改动，覆盖绝大部分收益）
2. 文件消失 → 删除其 Chunk
3. （可选）compute / del / addTag 三态，用于仓库移动/复制场景
```

理由：基于 hash 的方案不依赖版本控制、实现更简单、且对"非 Git 管理"的场景同样适用。DEF §32 的 "Git Incremental Index" 措辞应理解为"增量索引"这一能力，而非"必须用 Git 实现"。

### Severity

`LOW`（归属无争议，仅措辞可能误导实现方式）

---

## 15. Conflict 汇总

| 编号 | 主题 | Severity | 需在何时确认 |
|---|---|---|---|
| 01 | `chunk_type` 命名与取值（缺 `CONSTRUCTOR`） | MEDIUM | M3 建表前 |
| 02 | `knowledge_chunk` 字段集不足（缺 signature / annotations / javadoc / title） | MEDIUM | M3 建表前 |
| 03 | Benchmark 规模与 MIXED 配额 | MEDIUM | M6 之前 |
| 04 | Rerank 的阶段归属 | LOW | M7 之前（接口预留） |
| 05 | "V1" 术语歧义（LangGraph DAG vs 全项目） | LOW | 已在 M11/M12 拆分中解决 |
| 06 | Repository 根目录模型（代码/文档不同根） | **HIGH** | **M1 开始前** |
| 07 | 增量索引的机制（hash vs Git） | LOW | M3 建表前（字段预留） |

**其中 Conflict 06 是唯一需要在 M1 之前确认的 HIGH 项。**

---

## 16. ADR 状态汇总

| ADR | 主题 | 结论 | Status |
|---|---|---|---|
| 001 | Java 解析器选型 | JavaParser（仅 `javaparser-core`） | FROZEN |
| 002 | Java Chunk 粒度 | METHOD 主体 + CLASS 摘要 + CONSTRUCTOR | FROZEN |
| 003 | Java content 来源 | Range + 原始文件切片 | FROZEN |
| 004 | Markdown Chunk 策略 | Heading-aware | FROZEN |
| 005 | 存储 | PostgreSQL + pgvector | FROZEN |
| 006 | Keyword Retrieval | PostgreSQL FTS（`pg_trgm` 归 P1） | FROZEN |
| 007 | 多路融合 | RRF（非 weighted sum） | FROZEN |
| 008 | Evaluation | 独立于 Generation，复用生产 Retriever | FROZEN |
| 009 | SymbolSolver | P0 不做（P2） | FROZEN |
| 010 | Reranker | P0 不做（P1） | DEFERRED |
| 011 | LangGraph 时机 | Retrieval Pipeline 稳定后接入 | FROZEN |
| 012 | Repository 根目录模型 | 支持 code_root + doc_root 双根 | `PROPOSED` |
