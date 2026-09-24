# RAGFlow 调研

> 本文是《开源项目调研.md》中 **RAGFlow 部分（第 7 章）** 的独立交付物。
> 调研对象：`infiniflow/ragflow`，仓库快照位于 `参考项目/ragflow-main`（`pyproject.toml` version `0.27.2`）。
> 调研范围：仅 **Chunking / Full-text Retrieval / Vector Retrieval / Hybrid Retrieval / Rerank / Retrieval Evaluation**。
> **不在范围内**：UI、Docker 部署、用户系统、Agent 平台、MCP、Knowledge Graph、多模态、OCR 复杂实现。

---

## 0. 阅读约定与调研范围

**引用格式**：`相对仓库根目录的路径 : 行号`。行号来自本次快照实际读取的内容，可回查。

**与调研需求章节的对应关系**（便于后续合并进《开源项目调研.md》）：

| 调研需求章节 | 本文位置 |
| --- | --- |
| 7.1 为什么研究 RAGFlow | 第 1 节 |
| 7.2 Chunk 设计（重点问题 A，第十七节） | 第 2 节 |
| 7.3 Full-text Retrieval（重点问题 B 之一，第十八节） | 第 3 节 |
| 7.4 Vector Retrieval（重点问题 B 之二） | 第 4 节 |
| 7.5 Hybrid Retrieval（重点问题 C，第十九节） | 第 5 节 |
| 7.6 Rerank（重点问题 D，第二十节） | 第 6 节 |
| 7.7 Retrieval Test（重点问题 E，第二十一节） | 第 7 节 |
| 7.8 对 DevContext 的启发 | 第 8 节 |
| 7.9 不采用的复杂能力 | 第 9 节 |
| （六段式结论：借鉴 / 不借鉴 / 简化 / P1） | 第 10 / 11 / 12 / 13 节 |
| （明确决定） | 第 14 节 |

**一个必须先说明的前提**：本次快照 0.27.2 正处于 **Python → Go 的迁移过程中**。`AGENTS.md` 的 "Current stack" 段写明后端是 Python 3.13+ / Quart，同时

> **Go**: the repository also has a substantial Go module for servers, ingestion, parser/runtime, CLI, and supporting services.

因此同一套检索逻辑存在**两套实现**：`rag/nlp/search.py`（Python 参考实现）与 `internal/service/nlp/retrieval.go` / `reranker.go`（Go 移植版）。Go 侧的注释会显式标注对应关系，例如 `internal/service/nlp/retrieval.go:626`：

> `// Matches rag/nlp/search.py:331 — every backend but Infinity takes this fixed pair`

**本文以哪一侧为准**：两套实现的算法一致（Go 是移植），本文引用时优先给出 Python 侧（作为参考实现），并在 Go 侧有额外信息（如归一化细节、引擎差异）时补充。

---

## 1. 为什么研究 RAGFlow（7.1）

DevContext 需要回答的是"工程化 RAG 的检索管线长什么样"：Chunk 怎么切、全文与向量怎么分工、多路结果怎么融合、Rerank 放在哪里、以及**为什么可以独立于生成来测检索**。

RAGFlow 是这类问题的一个完整工程样本，而且它把上面每一项都**做成了可配置的参数**（相似度阈值、向量权重、Rerank 候选数、Top），因此可以从源码里读出"参数默认值是多少、调大调小会发生什么"。

**两个必须如实说明的差异：**

1. **RAGFlow 定位是通用文档 RAG，不是 code RAG。** 它的 chunk 单位是"段落/章节/表格行/幻灯片"，没有类、方法、AST 的概念。它的全文检索基于自研 tokenizer + 空格分析器，不是为标识符设计的。所以 DevContext 从 RAGFlow 借鉴的是**检索管线的结构与参数**，不是检索内容本身。

2. **RAGFlow 的检索评测分两套，且都不直接服务"我的项目"**（详见第 7 节）。这一点对 DevContext 尤其重要——它是 DevContext 必须自建 Benchmark 的直接理由。

---

## 2. Chunk 设计（7.2）

### 2.1 核心事实：Chunk 策略由"解析方法"决定

RAGFlow 的 chunk 方法不是全局配置，而是**每个数据集选择一个 `parser_id`**，`parser_id` 直接就是 `chunk_method`（`internal/service/chunk/chunk.go:1002`）。

官方文档 `docs/guides/dataset/configuration.md:35-44` 列出了内置解析方法及其 chunk 语义：

| 解析方法 | 官方对 chunk 边界的描述（原文摘要） |
| --- | --- |
| **General** | 通用方法，适用多数常规文档；按配置的 chunking 规则创建 chunk |
| **Q&A** | 面向问答对组织的数据；**每对 Q&A 作为一个独立 chunk** |
| **Manual** | 面向有清晰层级章节结构的 PDF（产品手册、操作指南）；**按章节结构**切分 |
| **Table** | 面向 XLSX / CSV 等结构化表格；**每行通常作为一个独立 chunk** |
| **Paper** | 面向 PDF 论文/研究报告；按**摘要、章节、小节**等结构元素切分 |
| **Laws** | 面向法律文档；按**法律文档的结构特征**识别 chunk 边界 |
| **Presentation** | 面向 PDF / PPTX 演示文稿；**每页或每张幻灯片**作为一个独立 chunk |
| **One** | **整篇文档作为一个 chunk**；适用于较短文档且需要保留完整上下文 |
| **Tag** | 用于创建标签集，为其他数据集的 chunk 和 query 提供标签，**不直接参与 RAG 检索** |

源码侧对应 `rag/app/` 下 14 个独立 chunker 模块：`naive.py`（General）、`qa.py`、`manual.py`、`table.py`、`paper.py`、`laws.py`、`presentation.py`、`one.py`、`tag.py`，另有 `book.py`、`email.py`、`audio.py`、`picture.py`、`resume.py`。

**这就是"为什么没有一种 Chunk Size 适用于所有文档"的工程答案**：RAGFlow 不是调参，而是**换 chunker**。问答对、表格行、幻灯片、法律条款的"语义完整单元"根本不是同一个东西，用同一个字符数或 token 数去切，必然在至少一半的文档类型上破坏语义。

### 2.2 通用 chunker 的实际参数

对 General 路径，实际生效的是 `TokenChunker`（`rag/flow/chunker/token_chunker.py:50-59`）：

```python
class TokenChunkerParam(ProcessParamBase):
    def __init__(self):
        self.delimiter_mode = "delimiter"
        self.chunk_token_size = 512
        self.delimiters = list(DEFAULT_DELIMITER)
        self.overlapped_percent = 0
        self.children_delimiters = []
        self.table_context_size = 0
        self.image_context_size = 0
```

| 参数 | 默认值 | 含义（对应官方文档 `configuration.md:72-75`） |
| --- | --- | --- |
| `chunk_token_size` | `512` | "Recommended chunk size" |
| `delimiters` | `DEFAULT_DELIMITER` | "Delimiter for text" |
| `overlapped_percent` | `0` | "Overlapped percent (%)" |
| `children_delimiters` | `[]` | "Child chunk are used for retrieval" |
| `delimiter_mode` | `"delimiter"` | `"delimiter"` 或 `"one"`（后者等于整篇一个 chunk） |

`DEFAULT_DELIMITER`（`rag/nlp/delim.py`）：

```python
DEFAULT_DELIMITER = "\n!?;。；！？"
```

即"换行 + 中英文句末标点"。它的定义处有一段很有价值的注释（`delim.py` 模块 docstring 与常量上方注释），说明这个常量之所以被抽出来，是因为**历史上六个实现各自 copy 了不同的默认值**（txt/markdown 用完整的 8 字符集，docx/image/email 漏掉了 ASCII 分号 `;`，`rag/app/book.py` 保留了自己的中文专用集合），由此产生 issue #18562。

**这对 DevContext 的含义**：chunk 边界的"分隔符默认值"必须**集中定义在一处**，否则会在不同 parser 之间悄悄漂移。

### 2.3 分隔符的解析规则

`rag/nlp/delim.py` 用一段形式化语法定义 `parser_config.delimiter` 字段（模块 docstring）：

```text
delimiter_field := token*
token           := backtick_wrapped | bare_char
backtick_wrapped := "`" bare_char+ "`"
bare_char        := any single Unicode character except "`"
```

语义要点（`parse_delimiter_field`）：
- 反引号包裹的整段算作**一个多字符分隔符**；
- 反引号外的**每个字符各自是一个单字符分隔符**；
- 两者合并、去重，并按**长度降序**排序（保证 `##` 先于 `#` 匹配）；
- CRLF / 单独 CR 在解析前统一为 LF，使 Windows 行尾文档与 Unix 得到相同的切分结果；
- **大小写敏感**，不使用 `re.I`。

### 2.4 父子 chunk（Parent-Child）

这是 RAGFlow chunk 设计里最值得注意的一个机制，对应官方文档的 "Child chunk are used for retrieval"。

实现 `_split_chunk_docs_by_children`（`rag/flow/chunker/token_chunker.py:364-386`）：

```python
def _split_chunk_docs_by_children(chunks, pattern):
    # Apply the secondary children_delimiters split to text chunks only.
    if not pattern:
        return chunks

    docs = []
    for chunk in chunks:
        if chunk.get("doc_type_kwd", "text") != "text":
            docs.append(chunk)
            continue

        split_texts = _split_text_by_pattern(chunk.get("text", ""), pattern)

        mom = chunk.get("text", "").removeprefix("\n")
        for text in split_texts:
            if not text.strip():
                continue
            child = deepcopy(chunk)
            child["mom"] = mom          # ← 子 chunk 携带完整父文本
            child["text"] = text
            docs.append(child)

    return docs
```

**机制**：先用主分隔符 + `chunk_token_size` 切出父 chunk，再用 `children_delimiters` 在同一段文本上做**二次切分**，每个子 chunk 复制父 chunk 的所有元数据，并额外携带 `mom` 字段 = 父 chunk 全文。

**为什么这样做**：这解决了 chunk 尺寸的根本矛盾——

- chunk 太大 → embedding 是整段的语义平均，细粒度问题匹配不准；
- chunk 太小 → 检索命中后上下文不足，LLM 无法回答。

RAGFlow 的答案是**用子 chunk 做检索（匹配精度高），用 `mom` 提供上下文（回答完整）**。官方文档 `configuration.md:75` 的表述是："This is suitable for long documents where fine-grained recall needs to be improved."

**代价**：同一段文本被存了两遍（父 + 子），索引体积和 embedding 成本都会上升。所以它是**默认关闭**的（`children_delimiters = []`）。

### 2.5 内容增强（Content Enhancement）

官方文档 `configuration.md:87-88` 描述了两项 LLM 参与的内容增强：

| 功能 | 官方描述 | 对检索的作用 |
| --- | --- | --- |
| **Auto-keyword** | 为 chunk 自动生成若干关键词，补充 chunk 的语义信息 | 生成 `important_kwd` 字段 |
| **Auto-question** | 基于 chunk 内容自动生成若干问题，补充可能匹配该 chunk 的查询表达 | 生成 `question_tks` 字段 |

这两项直接连到检索权重（见 3.2 与 5.4 节）：`important_kwd` 在全文检索里权重 **30**、在 term 相似度里字段重复 **5** 次；`question_tks` 权重 **20**、重复 **6** 次。

**这是一个值得注意的设计取向**：当"用户可能怎么问"和"文档实际怎么写"存在表达鸿沟时，RAGFlow 选择**在入库时用 LLM 把文档改写/扩展出更多可匹配的表达**，而不是只靠 embedding 弥合。

**对 DevContext 的直接映射**：DevContext 的 Java 代码 chunk 天然有"符号表达"（`class_name` / `method_name` / `annotations`），这相当于**免费的、确定性的 keyword**，不需要 LLM 生成。JavaParser 已经能提供这些字段，把它们的权重调高即可获得 RAGFlow 用 Auto-keyword 换来的效果。这是一个可以省掉一次 LLM 调用的机会。

### 2.6 表格列模式（Column Mode）

对 Table 解析方法，RAGFlow 允许逐列指定用途（官方文档 `configuration.md:164-166`）：

| 模式 | 含义 |
| --- | --- |
| **Indexing** | 列内容进入 chunk 文本，参与向量检索和全文检索 |
| **Metadata** | 列只作为元数据保存，**不进 chunk 文本**，但可作过滤字段缩小检索范围 |
| **Both** | 既进 chunk 文本参与检索，也作为元数据用于过滤 |

官方文档还解释了这样做的目的（`configuration.md:175`）：

> In this way, fields that do not need to participate in content retrieval can be prevented from entering chunk text, while these fields are still preserved as retrieval filter conditions.

**DevContext Mapping（重要）**：这个"字段级三元划分"（只索引 / 只过滤 / 两者）正好对应 DevContext 的 code chunk 元数据处理决策。例如：

- `content`、`class_name`、`method_name`、`signature`、`annotations` → **Both**（进正文 + 建索引 + 可过滤）
- `file_path`、`module` → **Indexing + Metadata**（用于路径加权与目录过滤）
- `start_line`、`end_line`、`content_hash`、`repository_id` → **Metadata only**（**绝不能进 chunk 文本**）

最后一条尤其重要：如果把 `start_line` / `content_hash` 这类数字塞进 chunk 正文，它们会污染 embedding（数字 token 没有语义）并在全文检索里产生噪声。RAGFlow 的 Column Mode 给出了这个决策的现成框架。

### 2.7 结论：DevContext 是否应该照搬"多 chunker"

**部分照搬，形式不同。**

RAGFlow 用 `parser_id` → 独立 chunker 模块的方式实现"不同数据用不同策略"。DevContext 需要的是同样的**按数据类型分派**，但只有两个分支：

```text
.md   → Markdown Heading-aware Chunker
.java → JavaParser AST-aware Chunker
```

不需要 14 个模块，也不需要 `parser_id` 这种运行时选择的抽象——**由文件扩展名决定**即可。参照 RAGFlow 的经验，关键是把"分隔符/边界规则的默认值"集中定义（对应 2.2 的 issue #18562 教训），而不是散落在各 parser 里。

但 RAGFlow 的 `chunk_token_size = 512` + `DEFAULT_DELIMITER` 这套机制**对 Java 完全不适用**：Java 的语义边界是方法/类，不是标点。这正是 DevContext 必须用 JavaParser 而不是文本分隔符的原因。

---

## 3. Full-text Retrieval（7.3）

### 3.1 关键架构决策：RAGFlow 自己做分词

RAGFlow 的全文检索**不依赖搜索引擎的语言分析器**。它在入库时用自己的 tokenizer 把文本切成 token 存成空格分隔的字段，并让引擎用 **whitespace analyzer** 处理（`internal/engine/elasticsearch/chunk.go:2955` 等多处配置 `"analyzer": "whitespace"`）。

涉及的字段（`rag/nlp/__init__.py:428-429`）：

```python
d["content_ltks"] = rag_tokenizer.tokenize(t)
d["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(d["content_ltks"])
```

即两级 token：粗粒度 `*_ltks` 与细粒度 `*_sm_ltks`（中文再切分、长英文词再拆）。

**为什么这样做（判断）**：RAGFlow 面向中英混排、需要中日韩分词与同义词，而不同搜索引擎（ES / Infinity / GaussDB / OceanBase）的分词器能力不一致。把分词收到应用层，可以让**多个引擎产生一致的检索行为**——这正是它在 0.27.2 同时维护 5+ 个 doc engine 的前提。

**代价**：失去了引擎原生的 BM25 词频统计与语言分析器能力。这也解释了 5.3 节要讲的现象——RAGFlow 最终排序用的 term 相似度是**自己算的**，不是引擎的 BM25 分数。

### 3.2 字段加权：query_fields

全文检索的字段与权重集中定义在 `rag/nlp/query.py:32-40`：

```python
self.query_fields = [
    "title_tks^10",
    "title_sm_tks^5",
    "important_kwd^30",
    "important_tks^20",
    "question_tks^20",
    "content_ltks^2",
    "content_sm_ltks",
]
```

即：**关键词字段权重 30、问题字段 20、标题 10、正文 2、细粒度正文 1**。

**对 DevContext 的直接映射（高价值）**：这类字段加权完全可以用 PostgreSQL FTS 复现。DevContext 可定义：

```sql
to_tsvector('simple',
    setweight(to_tsvector(coalesce(method_name,'')), 'A') ||   -- 最高
    setweight(to_tsvector(coalesce(class_name,'')),  'A') ||
    setweight(to_tsvector(coalesce(annotations,'')), 'B') ||
    setweight(to_tsvector(coalesce(signature,'')),   'B') ||
    setweight(to_tsvector(coalesce(content,'')),     'C')      -- 最低
)
```

PostgreSQL 的 `setweight` 只有 A/B/C/D 四档，比 RAGFlow 的连续权重粗，但对 DevContext 的规模足够。这比 Continue 的"路径权重 10 倍"（单档）更接近 RAGFlow 的多字段思路。

### 3.3 查询侧：term 加权 + 同义词 + boost 语法

`FulltextQueryer.question()`（`rag/nlp/query.py:42-231`）做了一串查询改写：

1. **中英之间补空格**（`add_space_between_eng_zh`）；
2. **清洗**：`tradi2simp`（繁转简）、`strQ2B`（全角转半角）、小写、剔除搜索引擎的转义字符集（源码注释指出这是为适配 Infinity 的 lexer，否则会产生解析错误）；
3. **term 加权**：`self.tw.weights(tks)` 给出每个词的权重（`rag/nlp/term_weight.py`）；
4. **同义词扩展**：`self.syn.lookup(tk)` 查同义词（Redis 支撑），并以降权形式加入：`'"{}"^{:.4f}'.format(s, w / 4.0)` —— **同义词权重是被查词的 1/4**；
5. **构造带 boost 的查询串**：

```python
q = ["({}^{:.4f}".format(tk, w) + " {})".format(syn) for (tk, w), syn in zip(tks_w, syns) ...]
```

形如 `(purchase^1.0 (buy acquire))`。

6. **相邻词提升（bigram）**：对相邻的两词追加一个短语查询，权重取两者较大值 × 2：

```python
q.append('"%s %s"^%.4f' % (tks_w[i-1][0], tks_w[i][0], max(tks_w[i-1][1], tks_w[i][1]) * 2))
```

即 `"purchase ticket"^2.0`。

7. **`minimum_should_match`**：中文路径返回 `{"minimum_should_match": min(3, round(len(keywords) / 10)), ...}`（`:231`），英文路径用传入的 `min_match`。

**DevContext Mapping**：这套查询改写里，**第 6 条（相邻词 bigram 提升）对代码检索可能特别有用**：`purchaseTicket`、`RDelayedQueue`、`TicketService` 这类标识符在分词后会裂成多个 token，相邻合起来提升能让完整标识符的匹配排在前面。PostgreSQL FTS 里可以用 `<->`（phrase）或 `<2>`（近邻）操作符实现同类效果。

第 4 条（同义词权重 1/4）在 DevContext 的价值要打折：Java 标识符的同义词意义不大（`purchaseTicket` 没有同义词），但**自然语言问题的同义词**有意义（"超时关闭" vs "超时取消"）。可以作为 P1。

### 3.4 多引擎并存

`internal/engine/` 下同时存在多个后端目录。其中真正注册为 DocEngine 的类型（`internal/engine/engine.go:33-37`）是：

```go
EngineElasticsearch EngineType = "elasticsearch"
EngineInfinity      EngineType = "infinity"
EngineOceanBase     EngineType = "oceanbase"
EngineSeekDB        EngineType = "seekdb"
EngineSereneDB      EngineType = "serenedb"
```

Python 侧另有一个 GaussDB 分支（`common/settings.py:170-173` 的 `DOC_ENGINE_INFINITY / OCEANBASE / GAUSSDB / SERENEDB`）。

**即检索后端至少 6 条分支**（ES / Infinity / OceanBase / SeekDB / SereneDB / GaussDB）。`internal/engine/` 下的 `clickhouse`、`kvrocks`、`nats` 目录不是 DocEngine——前两者是别的用途的存储，`nats` 是消息中间件。

而检索路径里针对不同引擎有不同分支（`rag/nlp/search.py:803-840`）：

```python
if settings.DOC_ENGINE_INFINITY:
    # Don't need rerank here since Infinity normalizes each way score before fusion.
elif settings.DOC_ENGINE_OCEANBASE or settings.DOC_ENGINE_SERENEDB:
    # OceanBase still returns chunk vectors in the result; use the historical local rerank
elif settings.DOC_ENGINE_GAUSSDB:
    # GaussDB computes fusion and PageRank in SQL
else:
    # ES path: ask ES for the clean cosine score via a second KNN-only call
```

**这是"不照搬"清单里的一条**：DevContext 只用一个引擎（PostgreSQL），不应该为多引擎抽象付出代价。但这段分支的存在说明了一件事——**"融合发生在哪里"会随引擎能力而变**（有的引擎内做、有的应用层做），所以 DevContext 应明确"融合在应用层做"（RRF 在 Python 侧），避免依赖数据库特定能力。

---

## 4. Vector Retrieval（7.4）

向量检索通过 `MatchDenseExpr` 下推到引擎（`rag/nlp/search.py:543-550`）：

```python
matchDense = MatchDenseExpr(
    f"q_{dim}_vec",        # 列名带维度：q_1024_vec
    sres.query_vector,
    "float",
    "cosine",              # 距离度量
    len(sres.ids),
    {"similarity": 0.0},
)
```

关键参数（官方文档 `http_api_reference.md:2527-2530`）：

| 参数 | 默认值 | 官方说明 |
| --- | --- | --- |
| `knn_top_k` | `1024` | The number of chunks engaged in vector cosine computation |
| `knn_num_candidates` | `max(2048, knn_top_k)` | The number of approximate nearest-neighbor candidates；**仅对 Elasticsearch 生效** |

**值得注意的两点：**

1. **两个参数分离**：`knn_num_candidates` 是 ANN 索引层面的候选数（精度/速度权衡），`knn_top_k` 是真正做余弦计算的条数。这是典型的 HNSW/IVF 调参方式，pgvector 也有对应参数（`hnsw.ef_search`）。

2. **列名带维度**（`q_{dim}_vec`）：RAGFlow 用列名编码 embedding 维度，这样切换 embedding 模型时不会与旧维度的向量冲突。**这一点与 DevContext 相关**：如果 DevContext 允许换 embedding 模型，需要考虑"维度变化时如何重建索引"——不过 DevContext V1 可以固定一个模型，暂不处理。

3. **向量不回传应用层**：`rag/nlp/search.py:537-539` 的注释说明了设计意图：

> We rely on ES to do the vector math so the chunk vectors never leave the engine.

ES 路径下，向量相似度由 ES 在第二次 KNN 查询里算好返回（`_knn_scores`），而不是把 chunk 向量搬回 Python 再算余弦。这是**性能优化**：避免传输大量向量。

---

## 5. Hybrid Retrieval（7.5）

### 5.1 结论先行：RAGFlow 用的是加权求和，不是 RRF

**这是本次调研最重要的发现之一。**

融合表达式构造（`rag/nlp/search.py:37-44`）：

```python
def build_fusion_expr(topn: int, vector_similarity_weight: float = 0.3) -> FusionExpr:
    """Build the Infinity weighted-sum expression from the vector weight."""
    term_similarity_weight = 1 - vector_similarity_weight
    return FusionExpr(
        "weighted_sum",
        topn,
        {"weights": f"{term_similarity_weight:g},{vector_similarity_weight:g}"},
    )
```

方法名就是 `"weighted_sum"`，权重是 `(1 - vector_weight, vector_weight)`。

官方文档 `docs/guides/search/search_settings.md:43-48` 也明确了这个语义：

> Sets the relative weights of vector similarity and full-text search in hybrid retrieval. For example, a value of `0.3` means: Vector = `0.3`, Full-text = `0.7`

**默认权重：全文 0.7 / 向量 0.3**（`vector_similarity_weight` 默认 0.3）。

### 5.2 为什么 RAGFlow 能用加权求和（而 DevContext 不能）

这是理解 RRF 价值的关键。RAGFlow 之所以能直接加权求和，是因为它**主动消除了分数空间不一致的问题**——用三种手段：

**手段一：把全文侧换成自己算的、与向量同尺度的分数。**

最终排序用的不是 BM25 原始分，而是 `rerank()` 里自算的 `tsim`（见 5.4），它与余弦相似度一样落在 `[0, 1]`。

**手段二：在引擎内先做归一化。**

`rag/nlp/search.py:803-804`：

```python
if settings.DOC_ENGINE_INFINITY:
    # Don't need rerank here since Infinity normalizes each way score before fusion.
```

Infinity 引擎在加权融合前对两路分数各自归一化。

**手段三：只在"混合"时才有阈值，纯 term 检索时阈值置零。**

`rag/nlp/search.py:850-851`：

```python
# When vector_similarity_weight is 0, similarity_threshold is not meaningful for term-only scores.
post_threshold = 0.0 if vector_similarity_weight <= 0 else similarity_threshold
```

**这段注释是 DevContext 采用 RRF 的直接论据。** RAGFlow 自己承认：**同一个相似度阈值不能同时适用于两种分数空间**，所以在纯 term 检索时必须把阈值关掉。这正是"vector similarity = 0.86 与 BM25 score = 7.2 不可直接比较"的另一种表述。

RAGFlow 的选择是"额外做归一化来让两者可比"；DevContext 的选择是"用排名而不是分数来融合（RRF）"，从而**不需要归一化**。两条路都成立，选择依据是：

- RAGFlow 需要**可调权重**（用户可以在 UI 上拖动"向量权重"），所以必须让两路分数同尺度；
- DevContext 需要**可解释、可评测的融合**，且只有两路召回，RRF 参数更少（只有一个 k），结论更容易归因。

这也解释了为什么 RAGFlow 的"向量权重"参数在 DevContext 里**没有对应物**——这正是 RRF 的取舍：放弃连续调权，换来免归一化和可解释性。

### 5.3 双阶段召回：首轮近纯向量，二轮才用用户权重

这是 RAGFlow 一个容易被忽略但很关键的实现细节。

`internal/service/nlp/retrieval.go:640-643`：

```go
// esFusionWeights is the pair the reference gives every non-Infinity backend
// (rag/nlp/search.py:331). 0.001 rather than 0 keeps a lexical leg for engines
// that reject a zero weight.
const esFusionWeights = "0.001,1"
```

以及 `:626-630` 的注释：

```go
// Matches rag/nlp/search.py:331 — every backend but Infinity takes this fixed
// pair, so the first search is a vector recall pass and the caller's
// vector_similarity_weight is applied afterwards, by RerankWithKNN
// (tkWeight = 1-vw, vtWeight = vw). Feeding it in here instead ranks the
// window by BM25 and cuts a different top-N than the reference does.
```

**机制**：

```text
第 1 步：首轮召回，融合权重固定为 (0.001, 1)
         → 效果上接近"纯向量召回"，term 只保留极小的兜底权重
         → 目的是拿到 64 条候选（rerank_candidates_count）
第 2 步：对这批候选，用用户配置的权重重新计算
         tkWeight = 1 - vector_similarity_weight, vtWeight = vector_similarity_weight
         sim = tkWeight * tsim + vtWeight * vsim
```

**为什么这么做**：注释说得很直白——如果把用户权重直接用在第一轮，窗口会被 BM25 排序，切出来的 top-N 与参考实现不一致。

**DevContext Mapping**：这其实是一个"**召回与排序分离**"的实例，和 Continue 的"索引计划与执行分离"是同一类思想，也和 DevContext 的架构吻合（召回 → 融合 → 可选精排 → Context Builder）。DevContext 的 RRF 天然就是这个结构：**FTS 与 vector 各取 top-N 召回，再用 RRF 融合排名**，不存在"用最终权重去影响召回窗口"的问题。

### 5.4 term 相似度的真实算法（不是 BM25）

`rerank()` 的最终公式（`rag/nlp/search.py:631-662`）：

```python
sim, tksim, vtsim = self.qryr.hybrid_similarity(sres.query_vector, ins_embd, keywords, ins_tw, tkweight, vtweight)
return sim + rank_fea, tksim, vtsim
```

而 `hybrid_similarity`（`rag/nlp/query.py:170-178`）：

```python
def hybrid_similarity(self, avec, bvecs, atks, btkss, tkweight=0.3, vtweight=0.7):
    sims = cosine_similarity([avec], bvecs)
    tksim = self.token_similarity(atks, btkss)
    if np.sum(sims[0]) == 0:
        return np.array(tksim), tksim, sims[0]      # ← 向量全零时降级为纯 term
    return np.array(sims[0]) * vtweight + np.array(tksim) * tkweight, tksim, sims[0]
```

注意两点：
1. **`cosine × vtweight + tksim × tkweight`** —— 线性加权，两个分量都在 `[0,1]`。
2. **向量全零时降级为纯 term 排序**（`if np.sum(sims[0]) == 0`）。这是"embedding 服务不可用/未配置"时的兜底路径。

term 相似度本身（`rag/nlp/query.py:180-197` / Go 侧 `internal/service/nlp/reranker.go:427-497`）：

```python
def token_similarity(self, atks, btkss):
    def to_dict(tks):
        d = defaultdict(int)
        wts = self.tw.weights(tks, preprocess=False)
        for i, (t, c) in enumerate(wts):
            d[t] += c * 0.4                                   # 一元词：权重 × 0.4
            if i + 1 < len(wts):
                _t, _c = wts[i + 1]
                d[t + _t] += max(c, _c) * 0.6                 # 相邻二元词：× 0.6
        return d
```

```python
# tokenDictSimilarity（Go 侧）：
s = 1e-9
for t in keys:
    if t in dtwt:
        s += qtwt[t]          # 只累加"在文档里出现过的" query 权重
        matchCount++
q = 1e-9
for t in keys:
    q += qtwt[t]              # query 全部权重
return s / q
```

**三个关键性质：**

1. **一元词 0.4 / 相邻二元词 0.6** —— 相邻词组合比单词更重要。这与 3.3 节的 bigram boost 是同一个思路的两种实现（一个作用于召回，一个作用于精排）。
2. **`s / q` 是"query 覆盖率"，不是"文档相关度"**。分母是 query 的权重总和，所以一个包含全部 query 词的长文档得 1.0，一个只包含一半的短文档得 0.5——**文档长度与冗余不进入分母**。这与 BM25 的文档长度归一化、TF 饱和是**根本不同**的度量。
3. **没有 IDF**。所有 query 词等权（除了 term_weight 给的权重），罕见词和常见词不加区分。

**这对 DevContext 的启示**：RAGFlow 的 term 相似度是一个**轻量的、可解释的词覆盖度量**，不是 BM25。DevContext 用 PostgreSQL FTS（`ts_rank` / `ts_rank_cd`）会得到真正带 IDF 的 BM25 类分数——这其实是比 RAGFlow 更成熟的全文打分。但**这也再次说明两路分数不可比**：`ts_rank` 与余弦相似度不在同一空间，所以 DevContext 用 RRF 是对的。

### 5.5 字段重复加权

`rerank()` 里构造文档 token 列表时（`rag/nlp/search.py:649-655`）：

```python
content_ltks = list(OrderedDict.fromkeys(sres.field[i].get(cfield, "").split()))
title_tks = [...]
question_tks = [...]
important_kwd = [...]
tks = content_ltks + title_tks * 2 + important_kwd * 5 + question_tks * 6
```

**用列表重复实现字段加权**：`important_kwd` 重复 5 次、`question_tks` 重复 6 次、`title_tks` 重复 2 次、正文 1 次。因为 term 相似度是对 token 列表求和，重复即等于加权。

**这是与 3.2 节 `query_fields` 字段加权并列的第二套加权机制**：一套作用于引擎侧召回（`^30` 这种 boost 语法），一套作用于应用侧精排（列表重复）。两者的权重顺序一致（重要词 > 问题 > 标题 > 正文）。

**DevContext Mapping**：如果 DevContext 的 term 侧使用 PostgreSQL FTS，那么字段加权只需在 `tsvector` 的 `setweight` 里做一次（见 3.2），**不需要第二套重复加权**。这是简化点——RAGFlow 需要两套是因为它的召回（引擎）与精排（应用层自算）用了两套不同的打分实现。

### 5.6 第三路分数：RankFeature（PageRank + Tag）

除了 term 与 vector，RAGFlow 还有第三类加项，直接加到最终相似度上（`rag/nlp/search.py:501-531`）：

```python
def _rank_feature_scores(self, query_rfea, search_res):
    pageranks = np.array([search_res.field[chunk_id].get(PAGERANK_FLD, 0) for chunk_id in search_res.ids], dtype=float)
    return self._tag_feature_scores(query_rfea, search_res) + pageranks
```

而 `_tag_feature_scores` 的返回被乘以 10（`:526`）：`return np.array(rank_fea, dtype=float) * 10.0`。

默认调用参数里 `rank_feature: dict | None = {PAGERANK_FLD: 10}`（`rag/nlp/search.py:722`）。

官方文档 `configuration.md:22-23` 说明这两个：

- **PageRank**：数据集级分数，"During retrieval, this score is added to the hybrid similarity score of matched chunks in the dataset, increasing their ranking weight."（用于让某个数据集的内容在多数据集检索时优先）
- **Tag sets**：为 chunk 和 query 自动关联标签，"improving retrieval accuracy with tag information"

**判断**：这是"检索之外的知识"（图的中心性、人工标签）作为加项进入排序。DevContext V1 **不做**——没有图、没有人工标签，而且这类加项会干扰 RRF 的纯排名融合。但它提示了一个方向：如果 DevContext 将来想表达"某个类是整个项目的核心入口，应该优先返回"，可以作为一个 P2 的静态权重项。

### 5.7 相似度阈值与 min_match 的联动

`rag/nlp/search.py:773`、`internal/service/nlp/retrieval.go:645-654`：

```python
min_match = vector_similarity_weight < 0.8
```

```go
// minMatch mirrors Python's `min_match = vector_similarity_weight < 0.8`
// (rag/nlp/search.py:773): a search that is almost entirely vector-weighted asks
// the text leg for nothing in particular, so its keyword query matches on any term
// instead of a share of them (0.3 first, 0.1 on the looser retry).
func minMatch(vectorSimilarityWeight *float64, withMatch float64) float64 {
	if vectorSimilarityWeight != nil && *vectorSimilarityWeight >= 0.8 {
		return 0.0
	}
	return withMatch
}
```

**逻辑**：当向量权重 ≥ 0.8 时（几乎纯语义检索），把 `minimum_should_match` 设为 0，即"全文侧匹配任意一个词即可"，不要求覆盖某个比例的 query 词。

**原因**：既然排序主要由向量决定，全文侧的任务只是**扩大候选集**而不是精确筛选，所以应该放宽而不是收紧。这是一个"参数之间不该独立调节"的例子——`minimum_should_match` 应该由向量权重推导，而不是单独暴露给用户。

**DevContext Mapping**：这是对 LangGraph Router 的一个额外论据。DevContext 的 DOC / CODE / MIXED 路由本质上就是在决定"哪一路应该放宽、哪一路应该收紧"，而这个决定**不应该暴露给用户去调**。RAGFlow 在这里用了一条硬编码规则来隐式决定，DevContext 用 Router 显式决定，是同一件事的两种粒度。

### 5.8 为什么生产 RAG 不只依赖向量检索（回答重点问题 C）

综合以上，RAGFlow 的答案可以归纳为四条：

1. **向量检索对精确匹配不稳定。** 官方文档 `search_settings.md:55` 给出了最直接的应用指导：

   > Decrease the vector weight: Gives greater priority to full-text matching. This is suitable for **exact matches involving proper nouns, product models, identifiers, fixed terminology**, and similar content.

   这句话直接点名了 **identifiers**——正是 DevContext 的 `RDelayedQueue` / `PurchaseTicketReqDTO` / `TicketService` 这类 Symbol Query。

2. **工程上需要可调的权重。** 不同语料的最佳配比不同（代码 vs 散文 vs 法律），所以 RAGFlow 不固定权重，而是暴露 `vector_similarity_weight` 让用户按语料调。

3. **全文侧提供可解释的匹配依据。** `tsim` 与 `vsim` 是分开返回并展示的（`ranks["chunks"]` 里同时给出 `"similarity"` / `"vector_similarity"` / `"term_similarity"`，`rag/nlp/search.py:891-893`）。用户能看到"这条是靠关键词命中的还是靠语义命中的"，这对调参和排查至关重要。

4. **降级路径。** 向量不可用时（全零）自动退化为纯 term 排序（`query.py:176-177`），保证系统仍可用。

### 5.9 DevContext 的对应答案（Symbol vs Semantic）

结合 RAGFlow 的官方指导，DevContext 的两路分工可以定下来：

| 查询类型 | 例子 | 主要依赖 | 原因 |
| --- | --- | --- | --- |
| **Identifier / Symbol** | `RDelayedQueue`、`purchaseTicket`、`TicketServiceImpl`、`@FeignClient`、`application.yml` 里的 key | **全文检索**（且需子串/前缀能力） | 标识符是字面量，语义 embedding 对"看起来像随机字符串"的 token 匹配不稳定；且标识符的子串匹配（`Ticket` → `TicketServiceImpl`）只有全文能做 |
| **Natural language（Why / How）** | "系统如何实现订单超时关闭"、"为什么 Feign 要移出事务" | **向量检索** | 与代码/文档用词不一致时，只有语义匹配能命中 |
| **MIXED** | "为什么把 Feign 移出事务，改了哪些代码" | **两者 + RRF** | 设计文档靠语义、代码靠符号，必须两路都进 |

**一个必须补的工程细节**：RAGFlow 的全文检索靠自研 tokenizer 处理标识符，DevContext 用 PostgreSQL 默认分词器会**把 `purchaseTicket` 切成 `purchase` + `ticket`** 或干脆当作一个未知词。所以 DevContext 需要：

- 用 `simple` 字典（不做词干化，避免破坏标识符）；
- 或额外开 `pg_trgm` 做子串匹配（这是 Continue 用 trigram 分词器的原因，也是 Continue 调研里已经列为 P1 的项）。

这一点 RAGFlow 是**给不出参考的**——它的 tokenizer 是为自然语言设计的，对代码标识符并不适配。

---

## 6. Rerank（7.6）

### 6.1 完整流程

官方文档 `search_settings.md:61-68` 定义：

> **Rerank candidates** — Sets the number of candidate chunks that enter the reranking stage. The system first retrieves candidate content from the knowledge bases and then determines which results to display from among those candidates. ... For example, a value of `100` means that up to 100 candidate chunks are selected for subsequent processing.

实际默认值 `rerank_candidates_count = 64`（`http_api_reference.md:2531-2532`），并且文档明确约束："It must be at least `page` multiplied by `page_size`."

流程（`rag/nlp/search.py:750-801`）：

```text
Query
  ↓
首轮召回（近纯向量，取 rerank_candidates_count = 64 条）
  ↓
【可选】Rerank 模型精排
   有 rerank_mdl → rerank_by_model()  → 模型打分
   无 rerank_mdl → 按引擎分支用 tsim/vsim 加权
  ↓
相似度阈值过滤（post_threshold = 0.2）
  ↓
排序 → 分页 → 返回 TopK
```

### 6.2 关键设计：Rerank 结果仍然参与"混合"，不是直接替换排序

`rerank_by_model`（`rag/nlp/search.py:664-703`）的最后一步：

```python
tksim = self.qryr.token_similarity(keywords, ins_tw)
vtsim, _ = rerank_mdl.similarity(query, rerank_docs)
rank_fea = self._rank_feature_scores(rank_feature, sres)

return tkweight * np.array(tksim) + vtweight * vtsim + rank_fea, tksim, vtsim
```

注意 `tkweight, vtweight` 此时仍是 `term_similarity_weight, vector_similarity_weight` = `0.7, 0.3`（调用处 `:793-801`）。

**所以 rerank 模型的作用不是"重新排序"，而是替换掉 vsim 这一项。** 即：

```text
无 rerank:  sim = 0.7 × 词覆盖度 + 0.3 × 余弦相似度        + rank_fea
有 rerank:  sim = 0.7 × 词覆盖度 + 0.3 × 交叉编码器分数    + rank_fea
```

Go 侧代码注释把这个意图写得很清楚（`internal/service/nlp/reranker.go:192-197`）：

```go
// Combine token similarity with model similarity
// Model similarity is treated as vector similarity component
sim = make([]float64, len(insTw))
for i := range tsim {
    sim[i] = tkWeight*tsim[i] + vtWeight*modelSim[i]
}
```

**这与 Continue 的做法不同，也是一个值得注意的取舍。** Continue 的 `RerankerRetrievalPipeline`（`core/context/retrieval/pipelines/RerankerRetrievalPipeline.ts:88-124`）是**直接用 rerank 分数排序**，不做混合。而 RAGFlow 保留了 term 侧 0.7 的权重。

**取舍分析**：

| 方案 | 优点 | 缺点 |
| --- | --- | --- |
| Rerank 分数直接排序（Continue） | 精排模型能力被充分利用 | 交叉编码器可能漏掉精确标识符匹配；模型不可用时无兜底 |
| Rerank 分数只替换向量项（RAGFlow） | 保留关键词精确匹配的贡献；rerank 模型质量差时不会毁掉整体 | 精排能力被稀释（只占 0.3 权重） |

**DevContext 的判断**：RAGFlow 的做法对 DevContext 更有参考价值，因为 DevContext 的查询里有一大批 Identifier Query——如果让交叉编码器完全接管排序，`RDLayedQueue` 这类精确匹配可能被语义模型打低分。但 DevContext 用 RRF，融合是"排名层"的，rerank 应该作为一个**独立的第三路**参与 RRF（或作为 RRF 之后的最终精排），而不是替换某一路的分数。这一点需要在 P1 实现时明确。

### 6.3 一个真实的工程坑：reranker 的分数量纲不一致

`internal/service/nlp/reranker.go:182-190` 的注释：

```go
// Reranker drivers do not agree on a score scale: Cohere/Jina/Voyage emit
// calibrated [0, 1] relevance scores, but NVIDIA returns raw, often
// negative logits. The hybrid blend below (tkWeight * tksim + vtWeight *
// modelSim) lives on a fixed [0, 1] scale, so an un-normalized logit
// weighted by vtWeight=0.7 can sink a relevant chunk below pure keyword
// matches and dominate the blend.
```

对应的归一化实现 `NormalizeRerankScores`（`reranker.go:225-267`）：

```text
若已是 [0,1]          → 原样返回（保留绝对量级，让 similarity_threshold 仍有意义）
若极差 < 1e-3（无区分度）→ 逐元素 clamp 到 [0,1]（避免除以接近 0 的极差）
否则                  → min-max 归一到 [0,1]
```

**这是一条极有价值的实战经验**：不同 rerank 供应商的分数不可比，必须归一化；但**不能无脑归一化**——对已经标定好的供应商（Cohere/Jina/Voyage），min-max 会破坏 `similarity_threshold` 的绝对语义。

**DevContext P1 实现 rerank 时必须处理这一点。**

### 6.4 第二个真实的工程坑：喂给 reranker 的必须是自然文本

`superseded` 于 token 化文本。源码注释（`rag/nlp/search.py:683-691`，Go 侧 `reranker.go:150-156` 同）：

> Feed the reranker the natural chunk text (markup preserved), not the tokenized `content_ltks`. **Neural rerankers score stemmed / accent-split tokens far lower, which collapses relevance scores and forces an artificially low similarity_threshold.**

并且注释还指出 `remove_redundant_spaces()` 是 ASCII 导向的，会把多语言文本弄坏（`"sécurité des données"` → `"sécuritédes données"`），所以只对缺 `content_with_weight` 的兜底路径使用。

**DevContext Mapping**：这条直接适用。DevContext 的 chunk 有两个版本——原始 `content`（保留格式）与用于全文索引的 token 化版本。**喂给 reranker 的必须是原始 `content`**，否则分数会被压低，进而迫使阈值调得极低，最终让阈值失去过滤作用。

### 6.5 分页与 rerank 的冲突（一个设计约束）

`rag/nlp/search.py:730-748` 的 docstring 讲清了为什么：

> Pagination is neither efficient nor reliable for this retrieval when rerank is enabled because the system must:
> - Retrieve more rerank candidates than the requested page_size.
> - Rerank those records to calculate similarity scores.
> - Filter out records below than the similarity threshold.
> When requesting page 2, the system must still process all candidates needed for page 1 ... Moreover, when `rerank_candidates_count` expands into the next retrieval window, new records are added to the candidate set and the entire set is reranked. That meant the previous returned pages might not be the same as the current returned pages, which is not acceptable for pagination.

于是代码里直接硬性拒绝：

```python
if rerank_mdl is not None and page != 1:
    raise Exception(f"Pagination is not supported when rerank_mdl is specified. Please set page=1 to retrieve the top {page_size} results.")
```

**判断**：DevContext 的评测是"取 Top-K 后比对 ground truth"，天然不需要分页，不会遇到这个问题。但这段分析本身值得记录——它说明了 **rerank 与分页/流式返回在设计上不兼容**，如果 DevContext 将来做 UI/流式输出，需要在"全量精排后一次性返回"和"不精排以支持分页"之间二选一。

---

## 7. Retrieval Test（7.7）

### 7.1 RAGFlow 的官方立场（直接回答重点问题 E）

`docs/guides/dataset/retrieval_testing.md:14-16`：

> **Retrieval Testing** is used to verify whether a dataset can recall the expected chunks based on user queries. After documents complete parsing, it is recommended to use typical questions in **Retrieval Testing** to test retrieval results first. Confirm whether the recalled chunks are correct, whether the content is complete, and whether the ranking is reasonable **before using the dataset in Chat, Search, or Agent**.

最关键的诊断逻辑在同一文档 `:16`：

> Retrieval testing can also help troubleshoot Q&A result problems. **If the correct chunk can already be recalled but the final answer is still unsatisfactory, further check the model, prompt, or application configuration. If the target chunk is not recalled, continue checking document parsing, chunks, metadata, and retrieval parameters.**

**这就是"为什么 Retrieval 应该独立于 LLM Generation 进行测试"的官方答案。** 它的价值不是"多一个功能"，而是**故障定位的分层**：

```text
答案不好
  ├─ 检索不到正确 chunk  → 问题在【检索侧】：
  │                        文档解析 → chunk → 元数据 → 检索参数
  └─ 检索到了但答案不好  → 问题在【生成侧】：模型 / prompt / 应用配置
```

如果只测"问题 → 最终答案"，这两类故障会被混在一起，无法定位。RAGFlow 把检索单独做成一个可反复运行的测试界面，就是为了在进入生成之前先把检索这一层锁定。

官方还给出了排查顺序（`retrieval_testing.md:66`）：

> Debugging suggestion: when retrieval results are unsatisfactory, troubleshoot in the order of **document parsing -> chunk -> metadata -> retrieval parameters**. First confirm that the knowledge content itself has correctly entered the dataset, then adjust retrieval parameters.

### 7.2 可配置的检索参数面板

官方文档 `retrieval_testing.md:24-29` 列出了测试页面上可调、且**可反复运行对比**的参数：

| 参数 | 默认值 | 官方说明 |
| --- | --- | --- |
| Similarity threshold | `0.2` | 低于阈值的候选被过滤；调高收窄召回、调低扩大召回 |
| Vector similarity weight | `0.3` | 向量相似度权重，另一部分是全文/关键词相似度（即 0.7） |
| Rerank model | 无 | 选中后 rerank 结果参与综合分数；会增加延迟 |
| Cross-language search | 无 | 跨语言检索 |
| Metadata | 无 | 按元数据条件限制检索范围 |
| Top | — | 返回候选结果的最大数量 |

并且明确了一条**重要的语义边界**（`retrieval_testing.md:31`）：

> Parameter changes in **Retrieval Testing** are only used for the current test and are **not automatically synchronized to Chat Assistant or Agent**. After suitable parameters are determined, configure the corresponding parameters in the application that actually uses the dataset.

即：**检索测试是一个"沙盒"，调参不会污染生产配置。** 这个隔离设计很重要——否则调参过程本身会影响线上行为。

### 7.3 独立测试的接口形态

检索测试本身是一个独立的 HTTP 端点（`docs/references/http_api_reference.md:2441-2473`）：

```http
POST /api/v1/retrieval
```

请求体（节选，完整列表见文档 `:2455-2473`）：

```json
{
  "question": "...",
  "dataset_ids": ["..."],
  "document_ids": ["..."],
  "page": 1,
  "page_size": 30,
  "similarity_threshold": 0.2,
  "vector_similarity_weight": 0.3,
  "knn_top_k": 1024,
  "knn_num_candidates": 2048,
  "rerank_candidates_count": 64,
  "rerank_id": "...",
  "keyword": false,
  "highlight": false,
  "cross_languages": [],
  "metadata_condition": { "logic": "and", "conditions": [...] }
}
```

返回中包含每条 chunk 的 `similarity` / `vector_similarity` / `term_similarity`（`rag/nlp/search.py:891-893`）。

**注意这个端点的输入只有 query，没有"期望答案"**。它是"给我看检索结果"，不是"给我算指标"。

### 7.4 必须指出的限制：Retrieval Test 不产出量化指标

这是本次调研中**对 DevContext 最重要的一条负面发现**。

**在 UI / HTTP API 层面，RAGFlow 的 Retrieval Test 没有任何 ground truth 概念，也不计算 Recall@K / MRR / nDCG。** 它只展示"召回了哪些 chunk、相似度多少、来自哪个文档"（`retrieval_testing.md:41`），由人工判断。官方文档也明确把判断标准写成人工检查清单（`retrieval_testing.md:45-51`）：

> - Whether the target chunk is successfully recalled.
> - Whether the recalled chunk comes from the correct document.
> - Whether the chunk content is related to the query and contains the information needed to answer the question.
> - Whether highly relevant content appears near the top.
> - Whether many chunks unrelated to the query are recalled.
> - Whether required content is excluded by metadata or other filter conditions.

**RAGFlow 确实有量化评测，但在另一个地方**：`rag/benchmark.py`（293 行），一个独立的离线脚本，使用 `ranx` 库：

```python
from ranx import evaluate
from ranx import Qrels, Run
```

计算 `ndcg@10`、`map@5`、`mrr@10`（`rag/benchmark.py:231`、`:238`、`:259`），覆盖三个公开数据集：MS MARCO v1.1、TriviaQA、MIRACL（18 种语言）。

关键实现（`rag/benchmark.py:52-66`）：

```python
def _get_retrieval(self, qrels):
    time.sleep(20)
    run = defaultdict(dict)
    for query in list(qrels.keys()):
        ranks = asyncio.run(settings.retriever.retrieval(
            query, self.embd_mdl, self.tenant_id, [self.kb.id], 1, 30, 0.0, self.vector_similarity_weight))
        ...
        for c in ranks["chunks"]:
            run[query][c["chunk_id"]] = c["similarity"]
    return run
```

注意 `similarity_threshold` 传 `0.0`（取全部结果供打分），`page_size` 传 `30`。它调用的是与线上完全相同的 `retriever.retrieval`——**评测与线上共用同一条检索路径**，这一点是好的。

**所以准确的结论是：**

| 层 | 是否独立于生成 | 是否量化 | 面向什么 |
| --- | --- | --- | --- |
| Retrieval Test（UI + `/api/v1/retrieval`） | ✅ 是 | ❌ 否，人工目测 | 日常调参、故障定位 |
| `rag/benchmark.py`（离线脚本 + ranx） | ✅ 是 | ✅ 是（nDCG@10 / MAP@5 / MRR@10） | 公开基准数据集上的横向对比 |

**DevContext 必须同时具备这两层，而且 benchmark 层的目标不同。** RAGFlow 的 benchmark 是**在公开数据集上证明系统整体能力**（nDCG@10 是排行榜指标），而 DevContext 需要的是**在自己的 my12306 仓库上证明这次改动有没有让检索变好**——这是一个更小、更具体的目标，需要 Recall@3 / Recall@5 / MRR + 自己的 30 条带 ground truth 的问题。

**这正是 DevContext 定义文档第 21 节把 Evaluation 列为强制模块的理由，RAGFlow 提供了这个判断的实证支持：** 一个成熟的检索系统，也会把"人工目测调参"和"离线量化评测"分成两件事，而且**日常调参用的工具不产出指标**。DevContext 不能只做前者。

### 7.5 补充：`insert_citations` 的做法

检索还有一个与 citation 相关的用途值得记录。`Dealer.insert_citations`（`rag/nlp/search.py:422-499`）在**生成答案之后**，把答案里的引用标记回填成 chunk id：

```python
res += f" [ID:{c}]"
```

它的工作机制是用 embedding 计算"答案片段"与 chunk 的相似度来定位引用来源（依赖 `fetch_chunk_vectors`，`rag/nlp/search.py:566-602`）。

**DevContext 的差异**：DevContext 的 citation 不需要反向定位——因为 JavaParser 已经给出了 chunk 的 `start_line` / `end_line`，citation 是**确定的**（`TicketService.java Lines 120-188`），不需要事后用相似度猜。这是 DevContext 相对通用文档 RAG 的一个结构性优势：**代码和 markdown 都有天然的行号/章节锚点**。这一点在 JavaParser 调研中已确认可行。

---

## 8. 对 DevContext 的启发（7.8）

**1. Chunk 边界应由内容类型决定，且默认值必须集中定义。**
RAGFlow 用 14 个 chunker 证明"没有万能 chunk size"。DevContext 只需要两个分支（Markdown / Java），但必须像 `DEFAULT_DELIMITER` 一样，把边界规则的默认值集中在一处，避免不同 parser 之间漂移（issue #18562 的教训）。

**2. 字段加权是低成本高收益的手段。**
`query_fields` 用 `title^10 / important^30 / question^20 / content^2` 一次性把"哪些字段更重要"表达清楚。DevContext 的 `method_name` / `class_name` / `annotations` / `signature` / `content` 应该有同样的分层加权。

**3. 召回与排序分离。**
RAGFlow 首轮用 `(0.001, 1)` 的近纯向量召回 64 条，二轮才用用户权重重排。这与 DevContext "FTS/Vector 各取 top-N → RRF → 可选 rerank" 的结构同构，验证了这个架构方向。

**4. 融合权重应该可以被推翻，而不是被调参。**
RAGFlow 用归一化换来了可调权重；DevContext 用 RRF 换来免归一化与可解释性。**RAGFlow 自己承认"纯 term 检索时相似度阈值无意义"（`search.py:850-851`），这恰恰证明了跨分数空间的比较是脆弱的。** 这条注释应记入 DevContext 的架构文档，作为选 RRF 的依据。

**5. 阈值与权重不该独立暴露。**
`min_match = vector_similarity_weight < 0.8` 说明"全文侧该收多紧"应由向量权重推导。DevContext 用 LangGraph Router 显式决定这件事，比暴露参数更好。

**6. 把"不参与检索的字段"挡在 chunk 文本之外。**
Table Column Mode 的 Indexing / Metadata / Both 三元划分，直接给出了 DevContext 处理 `start_line` / `content_hash` 这类字段的决策框架——它们必须是 metadata only。

**7. 检索与评测必须解耦，且评测要用生产同一条检索路径。**
`/api/v1/retrieval` 独立于 Chat/Agent；`rag/benchmark.py` 调用的是 `settings.retriever.retrieval`（线上同一个函数）。DevContext 的 Evaluation 也必须调用生产 `Retriever`，而不是另写一条简化路径，否则测出来的数字不代表线上。

**8. 评测用阈值 0 取全量、再算指标。**
`rag/benchmark.py:58` 传 `similarity_threshold=0.0` 以拿到完整排名供指标计算，`page_size=30`。DevContext 算 Recall@K 时也应如此——**阈值过滤会掩盖召回问题**，应该先取全量排名再截断到 K。

**9. rerank 之前先解决分数量纲。**
不同 rerank 供应商的分数不可比（NVIDIA 是负 logits），且**不能无脑归一化**（会破坏已标定供应商的绝对语义）。DevContext P1 接入 rerank 时必须处理。

**10. rerank 输入必须用原始文本，不能用索引后的 token。**
神经网络 reranker 对词干化/断词的输入打分会显著偏低，会迫使阈值下调到失去意义。DevContext 的 chunk 同时有原始 `content` 和索引化 token，喂 reranker 时必须选前者。

**11. 检索结果应展示分解分数。**
返回里同时给 `similarity` / `vector_similarity` / `term_similarity`，让用户看到"这条是靠关键词还是靠语义命中的"。DevContext 的检索结果应给出 `fts_rank` / `vector_rank` / `rrf_score` 三项，这对调试和面试讲解都有用。

**12. 故障定位要有分层顺序。**
"解析 → chunk → 元数据 → 检索参数" 这个顺序是 RAGFlow 官方推荐的排查路径，DevContext 的评测报告可以按同样顺序组织失败案例分析。

---

## 9. 不采用的复杂能力（7.9）

**1. 不采用"多 chunker 模块 + parser_id 运行时选择"。**
14 个 chunker 对应的是 14 种文档结构。DevContext 只有 `.md` 和 `.java` 两种，按扩展名分派即可，不需要注册表抽象。

**2. 不采用自研 tokenizer + 空格分析器。**
RAGFlow 这样做是为了让 5+ 个 doc engine 行为一致，并支持中日韩分词。DevContext 只有一个存储（PostgreSQL），应该直接用 PostgreSQL 的分词能力 + `pg_trgm`，不引入自研 tokenizer 和它的维护成本。

**3. 不采用加权求和融合，改用 RRF。**
见 5.2 节的分析。RAGFlow 需要连续调权；DevContext 需要可解释、可评测的融合。

**4. 不采用 RankFeature（PageRank + Tag）。**
DevContext V1 没有图结构、没有人工标签。引入会干扰 RRF 的纯排名融合，且属于"很高级但对 Retrieval Quality 帮助不明确"的项（违反调研需求第二十四节的决策原则）。

**5. 不采用父子 chunk（`children_delimiters` + `mom`）。**
机制本身有价值（细粒度召回 + 粗粒度上下文），但代价是同一段文本存两遍、embedding 成本翻倍。**DevContext V1 不需要**，因为 Java 的 chunk 粒度已经天然是"方法"——方法本身就是既精确又完整的语义单元，不存在 RAGFlow 面对的"段落太长/太短"问题。如果 P1 引入，应该是"方法 chunk + 类摘要 chunk"的父子关系，而不是把方法再切碎。

**6. 不采用 LLM 内容增强（Auto-keyword / Auto-question）。**
这两个功能的价值在于弥补"文档表达"与"用户提问"之间的鸿沟。DevContext 的 Java chunk 自带确定性的符号字段（`class_name` / `method_name` / `annotations` / `signature`），把它们的检索权重调高即可达到类似效果，**不必付出一次 LLM 调用**。

**7. 不采用 Cross-language search。**
DevContext 的语料是中文文档 + 英文代码，但不需要"用英文查询检索中文 chunk"这种跨语言能力，V1 不做。

**8. 不采用多 doc engine 抽象。**
`internal/engine/` 下有至少 6 条 DocEngine 分支（ES / Infinity / OceanBase / SeekDB / SereneDB / GaussDB），检索代码里针对引擎类型分支。DevContext 只有一个存储，不需要这层抽象，也不需要把融合下推到数据库。

**9. 不采用 `use_kg`（知识图谱多跳检索）。**
DevContext 定义文档第 29 节明确把 GraphRAG 列入 Non-goals。

**10. 不采用 RAGFlow 的 benchmark 形态（公开数据集 + nDCG/MAP）。**
`rag/benchmark.py` 面向 MS MARCO / TriviaQA / MIRACL，目标是证明"系统在这些公开榜单上表现如何"。DevContext 的目标是"**这次改动有没有让 my12306 上的检索变好**"，需要的是自己的 30 条问题 + Recall@3/5 + MRR，不是 nDCG@10。方向不同，不应照搬脚本形态。

---

## 10. DevContext 借鉴什么

**1. 按内容类型分派 chunk 策略**（不是调参，是换策略）。
**2. 字段加权检索**（`query_fields` 的多档权重思路）。
**3. 召回与排序分离的两阶段结构**（首轮召回候选，二轮精排）。
**4. 检索侧与生成侧解耦的测试界面**（Retrieval Test 的产品形态与故障分层逻辑）。
**5. 元数据的三元用途划分**（Indexing / Metadata / Both）。
**6. 检索结果暴露分解分数**（similarity / vector / term 三项）。
**7. 评测调用生产同一条检索路径**（`benchmark.py` 调用 `settings.retriever.retrieval`）。
**8. 评测时用阈值 0 取全量排名**，避免阈值掩盖召回问题。
**9. reranker 分数量纲归一化的工程经验**（且区分"已标定"与"未标定"供应商）。
**10. reranker 输入用原始文本而非索引 token。**
**11. 全文侧"该收多紧"由向量权重推导，而不是独立暴露**（`min_match` 相对 DevContext Router 的启发）。
**12. 召回失败/向量不可用时的降级路径**（向量全零 → 纯 term 排序）。

---

## 11. DevContext 不借鉴什么

| RAGFlow 的做法 | DevContext 的替代 | 理由 |
| --- | --- | --- |
| 14 个 chunker + `parser_id` | 按扩展名分派 2 个 chunker | 只有 md / java |
| 自研 tokenizer + whitespace analyzer | PostgreSQL `simple` 分词 + `pg_trgm` | 单存储，不需要多引擎一致性 |
| **`weighted_sum` 加权求和融合** | **RRF** | 见 5.2；RAGFlow 需可调权重，DevContext 需可解释与可评测 |
| RankFeature（PageRank + Tag）加项 | 不做 | 无图、无标签；干扰 RRF |
| 父子 chunk（`mom`） | V1 不做 | Java 方法本身已是精确且完整的单元 |
| Auto-keyword / Auto-question（LLM 增强） | 用确定的符号字段代替 | 省一次 LLM 调用 |
| 6+ 条 doc engine 分支 | 只用 PostgreSQL | 无必要复杂度 |
| Cross-language search | 不做 | 场景不需要 |
| `use_kg` / `toc_enhance` | 不做 | Non-goals |
| 公开数据集 benchmark（nDCG@10） | 自建 30 题 Benchmark（Recall@K / MRR） | 目标不同：证明改动有效性，不是打榜 |
| 分页 | 不做 | 评测场景取 TopK 即可，且分页与 rerank 冲突 |

---

## 12. DevContext 如何简化实现

| RAGFlow 机制 | 源码位置 | DevContext 简化方案 |
| --- | --- | --- |
| `parser_id` → 14 个 chunker 模块 | `rag/app/*.py` | 按扩展名分派：`.md` → Heading-aware，`.java` → JavaParser AST |
| `chunk_token_size = 512` + `DEFAULT_DELIMITER` | `rag/flow/chunker/token_chunker.py:50-59` | Markdown 用标题层级；Java 用方法边界。**都不按字符数切** |
| `children_delimiters` + `mom` 父子 chunk | `token_chunker.py:364-386` | V1 不做。P1 可做"方法 chunk + 类摘要 chunk" |
| LLM Auto-keyword / Auto-question | 官方文档 `configuration.md:87-88` | 用 JavaParser 提取的 `annotations` / `signature` / `method_name` 代替 |
| 表格列模式 Indexing / Metadata / Both | `configuration.md:164-166` | 直接映射：`content` 类 → Both；`file_path` → Indexing+Metadata；`start_line`/`content_hash` → **Metadata only** |
| `query_fields` 多字段 boost 语法 | `rag/nlp/query.py:32-40` | PostgreSQL `setweight`（A/B/C 三档）区分 method/class/annotation/signature/content |
| 自研 tokenizer + whitespace analyzer | `rag/nlp/__init__.py:428-429` | PostgreSQL `simple` 配置（不词干化）+ P1 的 `pg_trgm` |
| term 加权 + 同义词 + bigram boost 查询改写 | `rag/nlp/query.py:42-231` | V1 用 `websearch_to_tsquery` / `plainto_tsquery`；bigram 用 `<->` 近邻作为 P1 |
| `weighted_sum` 融合（`build_fusion_expr`） | `rag/nlp/search.py:37-44` | **RRF**：`score = Σ 1/(60 + rank_i)` |
| 首轮 `esFusionWeights = "0.001,1"` 近纯向量召回 | `internal/service/nlp/retrieval.go:643` | RRF 天然是"各路各取 top-N 再融合"，结构同构 |
| 应用层自算 term 相似度（`tokenDictSimilarity`，无 IDF） | `rag/nlp/query.py:180-197` | 用 PostgreSQL FTS 的 `ts_rank`（**带 IDF，比 RAGFlow 更成熟**） |
| 第二套字段重复加权（`title*2 + important*5 + question*6`） | `rag/nlp/search.py:649-655` | **不需要**——字段加权只在 `tsvector` 里做一次 |
| RankFeature 加项（PageRank + Tag × 10） | `rag/nlp/search.py:501-531` | 不做 |
| `similarity_threshold` + `vector_similarity_weight` 双参数 | `search_settings.md:30-55` | **不暴露**——由 LangGraph Router 决定 DOC/CODE/MIXED，RRF 无阈值 |
| rerank 分数替换 vsim 再混合（`tkWeight*tsim + vtWeight*modelSim`） | `reranker.go:192-197` | P1：rerank 作为独立一路进 RRF，或 RRF 之后的最终精排 |
| `NormalizeRerankScores` 供应商分数归一化 | `reranker.go:225-267` | P1 直接借鉴（区分已标定/未标定供应商） |
| reranker 输入用自然文本 | `search.py:683-693` | P1 借鉴：喂原始 `content`，不喂 token 化版本 |
| `insert_citations` 事后相似度定位引用 | `search.py:422-499` | **不需要**——JavaParser 提供精确 `start_line`/`end_line`，citation 是确定的 |
| Retrieval Test UI + `/api/v1/retrieval` | `retrieval_testing.md`、`http_api_reference.md:2441` | 借鉴**产品形态与故障分层逻辑**，但输出必须带 ground truth 比对 |
| `rag/benchmark.py`（ranx + nDCG/MAP） | `rag/benchmark.py` | 借鉴**离线脚本 + 阈值 0 取全量 + 调用生产 retriever**，但指标换成 Recall@3/5 + MRR，数据集换成自建 30 题 |
| 多 doc engine 分支 | `rag/nlp/search.py:803-840` | 只用 PostgreSQL |

**一句话总结简化方向**：RAGFlow 的复杂度来自"通用文档 + 多引擎 + 多语言 + 连续调权"。DevContext 去掉这四个前提后，chunker 从 14 个变 2 个，融合从加权求和变 RRF，全文打分从自研 term 相似度变 PostgreSQL `ts_rank`，评测从公开榜单变自建 30 题。

---

## 13. 哪些功能放 P1 / P2

**P1（成本低、与 Retrieval Quality 或 Evaluation 直接相关）**

1. **`pg_trgm` 子串匹配**。这是 DevContext 全文侧最关键的一项——PostgreSQL 默认分词器会把 `purchaseTicket` 切开或当作未知词，而 Identifier Query 是 DevContext 的主力场景。RAGFlow 靠自研 tokenizer 规避，DevContext 用 `pg_trgm` 规避。
2. **Cross-encoder Rerank**，按 RAGFlow 的方式接入：候选集（对应 `rerank_candidates_count`）、分数量纲归一化（区分已标定/未标定供应商）、喂原始 `content`。
3. **Rerank 与 RRF 的整合方式确定**（作为独立第三路进 RRF，或 RRF 之后的最终精排）——这是 P1 最需要想清楚的设计点（见 6.2）。
4. **检索结果暴露分解分数**（`fts_rank` / `vector_rank` / `rrf_score`），用于调试与失败分析。
5. **失败案例的分层定位**：按"解析 → chunk → 元数据 → 检索参数"组织评测报告。

**P2（有价值但成本或场景暂不匹配）**

6. **NGram / phrase 近邻检索**（对应 RAGFlow 的 bigram boost `"a b"^2.0`），用于提升多词标识符的匹配优先级。
7. **"方法 chunk + 类摘要 chunk"的父子关系**（RAGFlow 父子 chunk 的 DevContext 化），用于 MIXED 场景的类级上下文补全。
8. **同义词扩展**（对应 3.3 节，权重 1/4），主要服务中文自然语言问题。
9. **静态重要度权重**（对应 RAGFlow 的 PageRank 加项），例如"核心 Service 类提权"——但要注意不能干扰 RRF。

**明确不做**

- 自研 tokenizer、多引擎抽象、LLM 内容增强、GraphRAG / `use_kg`、跨语言检索、公开数据集 benchmark、分页。

---

## 14. 结论速览

| DevContext 问题 | RAGFlow 的答案 | 证据 | DevContext 决定 |
| --- | --- | --- | --- |
| 不同数据类型是否用不同 Chunk 策略 | 是，`parser_id` 选择 15 种 chunker | `rag/app/*.py`、`configuration.md:35-44` | 部分采纳：按扩展名分派 2 种 |
| 通用 chunk 的默认参数 | `chunk_token_size=512`、`DEFAULT_DELIMITER="\n!?;。；！？"`、`overlapped_percent=0` | `token_chunker.py:50-59`、`delim.py` | 不适用于 Java（语义边界是方法） |
| 是否有层级 chunk | 有，`children_delimiters` → 子 chunk 带 `mom` 父文本 | `token_chunker.py:364-386` | V1 不做；P2 用"方法+类摘要"替代 |
| 全文检索如何做 | 自研 tokenizer + whitespace analyzer + 字段 boost | `rag/nlp/__init__.py:428`、`query.py:32-40` | 用 PostgreSQL `simple` + `setweight` + `pg_trgm`（P1） |
| 字段加权 | `title^10 / important^30 / question^20 / content^2` | `query.py:32-40` | 采纳思路，改 A/B/C 三档 |
| 向量检索参数 | `knn_top_k=1024`、`knn_num_candidates=2048`、cosine | `http_api_reference.md:2527-2530` | pgvector HNSW，参数对应 `ef_search` |
| Full-text 擅长什么 | 精确匹配：专有名词、型号、**标识符**、固定术语 | `search_settings.md:55` | 采纳：Symbol Query 主力 |
| Vector 擅长什么 | 语义相似（用户措辞与原文不同时） | `search_settings.md:50-54` | 采纳：自然语言 Why/How 主力 |
| **多路结果如何融合** | **`weighted_sum`**：`term×0.7 + vector×0.3`（可调） | `search.py:37-44`、`search_settings.md:43-48` | **RRF** |
| 为什么加权求和可行 | 靠三处归一化（自算同尺度 term 分、Infinity 引擎归一化、纯 term 时关阈值） | `search.py:803`、`:850-851` | RRF 免归一化，且可解释 |
| 是否有第三路分数 | 有：RankFeature = PageRank + Tag×10 | `search.py:501-531` | 不做 |
| Rerank 流程 | 召回候选（64）→ reranker → 阈值过滤 → TopK | `search_settings.md:61-68`、`http_api_reference.md:2531` | P1 采纳 |
| Rerank 分数如何使用 | **只替换 vsim 项，仍与 term 加权混合**（0.7/0.3） | `reranker.go:192-197`、`search.py:703` | P1：作为独立一路进 RRF |
| Rerank 工程坑 | 供应商分数量纲不一致（NVIDIA 负 logits），需归一化但不可无脑归一化 | `reranker.go:182-267` | P1 借鉴 |
| Rerank 输入 | 必须用自然文本，token 化文本会压低分数 | `search.py:683-693` | P1 借鉴 |
| 是否独立测试 Retrieval | **是**，官方明确要求先进 Retrieval Testing 再进 Chat/Agent | `retrieval_testing.md:14-16` | **采纳，且强制** |
| Retrieval Test 是否量化 | **不量化**，人工目测 | `retrieval_testing.md:41-51` | DevContext 必须补量化 |
| 是否有量化评测 | 有，但**在独立离线脚本**（`rag/benchmark.py` + ranx，nDCG@10/MAP@5/MRR@10，公开数据集） | `rag/benchmark.py:31-32, 231` | 借鉴形式（共用生产 retriever、阈值 0），指标与数据换成自建 |
| Citation 如何产生 | 生成后用相似度回填 `[ID:x]` | `search.py:422-499` | 不需要：JavaParser 提供精确行号 |
| 该不该照搬 | — | — | 借鉴管线结构与评测分层；不借鉴自研 tokenizer、加权求和、多引擎、LLM 增强、GraphRAG |

**关键源码索引（后续开发回查用）**

```text
# Chunking
rag/flow/chunker/token_chunker.py:50-59       TokenChunkerParam 默认参数
rag/flow/chunker/token_chunker.py:364-386     _split_chunk_docs_by_children（父子 chunk + mom）
rag/flow/chunker/token_chunker.py:389-553     TokenChunker._invoke（主流程）
rag/nlp/delim.py                              DEFAULT_DELIMITER / parse_delimiter_field / 规则 docstring
rag/app/                                     14 个 chunker：naive / qa / manual / table / paper / laws / presentation / one / tag / book / email / audio / picture / resume

# Full-text
rag/nlp/query.py:32-40                        query_fields 字段加权
rag/nlp/query.py:42-231                       question() 查询改写（term 加权 / 同义词 / boost / bigram）
rag/nlp/query.py:170-197                      hybrid_similarity / token_similarity
rag/nlp/term_weight.py                        词权重
rag/nlp/synonym.py                            同义词
rag/nlp/__init__.py:428-429                   content_ltks / content_sm_ltks（两级 token）

# Retrieval & Fusion
rag/nlp/search.py:37-44                       build_fusion_expr（weighted_sum）
rag/nlp/search.py:422-499                     insert_citations
rag/nlp/search.py:501-531                     _rank_feature_scores / _tag_feature_scores
rag/nlp/search.py:533-564                     _knn_scores（ES 侧算余弦，向量不回传）
rag/nlp/search.py:604-629                     rerank_with_knn
rag/nlp/search.py:631-662                     rerank（本地余弦 hybrid）
rag/nlp/search.py:664-703                     rerank_by_model（rerank 分数替换 vsim）
rag/nlp/search.py:708-928                     retrieval() 主流程（参数、双阶段、阈值、分页）
internal/service/nlp/retrieval.go:605-654     Go 侧 fusion expr / esFusionWeights / minMatch
internal/service/nlp/reranker.go:55-89        Rerank 分派
internal/service/nlp/reranker.go:92-205       RerankByModel（混合公式）
internal/service/nlp/reranker.go:225-267      NormalizeRerankScores（供应商量纲）
internal/service/nlp/reranker.go:271-343      RerankStandard
internal/service/nlp/reranker.go:411-497      TokenSimilarity / tokensToDict / tokenDictSimilarity

# Evaluation
rag/benchmark.py:31-32, 52-66, 225-260        ranx + nDCG@10 / MAP@5 / MRR@10
docs/guides/dataset/retrieval_testing.md      Retrieval Test 官方说明（故障分层、调参、隔离）
docs/guides/search/search_settings.md         检索参数官方说明（阈值、向量权重、rerank 候选）
docs/guides/dataset/configuration.md          解析方法列表、chunk 参数、列模式
docs/references/http_api_reference.md:2441    POST /api/v1/retrieval 参数表

# 声明式配置
internal/service/chunk/chunk.go:1002          parser_id → chunk_method
common/doc_store/doc_store_base.py:58-70      MatchTextExpr / MatchDenseExpr
```

---

## 15. 当前调研无法确认的事项

按调研需求第三十四节要求，以下无法从源码/官方资料确定，**不做推测**：

1. **`vector_similarity_weight = 0.3` 与 `similarity_threshold = 0.2` 这两个默认值的标定依据。** 源码与官方文档都只给出数值与调参方向，未给出实验记录或推导过程。本文未找到 RAGFlow 自己在公开数据集上对这些默认值做的消融实验。

2. **`rag/benchmark.py` 在 MS MARCO / TriviaQA / MIRACL 上的实际得分。** 仓库中只有脚本，没有任何结果文件或报告（`save_results` 输出到运行时指定的路径，仓库内不存在）。因此**不能引用任何 RAGFlow 的检索质量数字**。

3. **Python 与 Go 两套检索实现是否已在所有部署形态下完全对齐。** `AGENTS.md` 表明迁移进行中，Go 侧注释多处标注"Matches rag/nlp/search.py:xxx"，说明 Yes 在主动对齐，但本次未验证两者在全部引擎组合下的行为一致性。

4. **`weighted_sum` 中两路分数的具体归一化方式（Infinity 侧）。** 源码注释只说 "Infinity normalizes each way score before fusion"，未给出是 min-max、z-score 还是其他方式（归一化实现应在 Infinity 引擎内部，不在 Python 侧）。

5. **RAGFlow 的 Retrieval Test 是否曾有过量化指标版本。** 当前 0.27.2 的 UI/API 只做人工目测；是否有过或计划有指标化版本，仓库中无依据。

6. **`answer` 与检索的延迟/成本数据。** 官方文档只说 rerank "may increase retrieval latency"，未给出具体量级。

7. **本文对"RAGFlow 定位为通用文档 RAG、不适配代码标识符"的判断依据。** 这是基于它的 tokenizer（`rag/nlp/rag_tokenizer.py`，面向自然语言）与 `query_fields` 的字段设计推断的；本次**未实际用代码标识符测试过 RAGFlow 的检索行为**。
