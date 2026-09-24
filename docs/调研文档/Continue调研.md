# Continue 代码索引与 Context Retrieval 调研

> 本文是《开源项目调研.md》中 **Continue 部分（第 5 章）** 的独立交付物。
> 调研对象：`continuedev/continue`，仓库快照位于 `参考项目/continue-main`（`core/package.json` version `1.1.0`）。
> 调研范围：**仅 Codebase Indexing / Context Retrieval**。IDE Plugin、UI、Chat、Model Provider、Agent UI、Prompt、IDE integration 均不在范围内。

---

## 0. 阅读约定

**引用格式**：`相对仓库根目录的路径 : 行号`。所有行号均来自本次快照实际读取的内容，可直接回查。

**本文的分析格式**（对应调研需求第二十六节）：

```text
Problem → Solution → Reason → Trade-off → DevContext Mapping
```

**术语对照**（Continue 用词 → DevContext 用词）：

| Continue | DevContext | 含义 |
| --- | --- | --- |
| `artifact` / `artifactId` | Index 类型 | 一套具体的物化索引（chunks / sqliteFts / codeSnippets / vectordb::model） |
| `tag` (`IndexTag`) | `repository_id` | 索引命名空间：目录 + 分支 + artifactId |
| `cacheKey` | `content_hash` | 文件内容 sha256 |
| `Chunk` | `knowledge_chunk` | 索引的最小检索单元 |
| `RefreshIndexResults` | 索引差异计划 | compute / del / addTag / removeTag 四类动作 |

---

## 1. 为什么研究 Continue

DevContext 要回答的第一个问题是：

> 一个代码 Repository 应该如何建立可检索 Index？是否应该存在独立 Chunk 层？Chunk 是否应该和 Vector DB 解耦？

Continue 是目前少数**把这个问题完整实现并且源码可读**的工程系统。它的索引部分（`core/indexing/`）与 IDE 部分（`extensions/`）在代码上基本分离，可以只读前者就完整还原"Repository → 可检索 Context"的全部阶段。这正是 DevContext 需要参考的层级结构。

同时 Continue 有一个必须正视的事实（见第 8.6 节）：**官方已经把 `@Codebase`（基于 embeddings 的代码检索）标记为 deprecated**，方向转向让 Agent 用 grep/glob/read 工具自己探索。这个转向本身就是对 DevContext 技术路线的重要输入。

---

## 2. Codebase Index 总体架构

### 2.1 核心抽象：CodebaseIndex

Continue 没有"一个索引"，而是把索引抽象成可插拔的 artifact。

`core/indexing/types.ts:16-25`

```ts
export interface CodebaseIndex {
  artifactId: string;              // 索引类型标识
  relativeExpectedTime: number;    // 相对耗时，用于进度条加权
  update(
    tag: IndexTag,
    results: RefreshIndexResults,  // 要做什么（差异计划）
    markComplete: MarkCompleteCallback,
    repoName: string | undefined,
  ): AsyncGenerator<IndexingProgressUpdate>;
}
```

关键设计：**`update()` 不负责判断"什么变了"**。它只接收一份已经算好的差异计划 `RefreshIndexResults`：

`core/indexing/types.ts:27-39`

```ts
export type PathAndCacheKey = { path: string; cacheKey: string };

export type RefreshIndexResults = {
  compute: PathAndCacheKey[];    // 需要从头计算
  del: PathAndCacheKey[];        // 需要彻底删除
  addTag: PathAndCacheKey[];     // 内容已算过，只需挂上新 tag
  removeTag: PathAndCacheKey[];  // 只需摘掉 tag
};

export type RefreshIndex = (tag: IndexTag) => Promise<RefreshIndexResults>;
```

这是**索引计划与索引执行分离**。差异计算只做一次（`refreshIndex.ts`），四个 artifact 各自消费同一份计划。

### 2.2 实际存在的 artifact

注册表位于 `core/indexing/CodebaseIndexer.ts:176-195`：

| artifactId | 实现类 | 文件 | relativeExpectedTime |
| --- | --- | --- | --- |
| `chunks` | `ChunkCodebaseIndex` | `core/indexing/chunk/ChunkCodebaseIndex.ts` | 1 |
| `codeSnippets` | `CodeSnippetsCodebaseIndex` | `core/indexing/CodeSnippetsIndex.ts` | 1 |
| `sqliteFts` | `FullTextSearchCodebaseIndex` | `core/indexing/FullTextSearchCodebaseIndex.ts` | 0.2 |
| `vectordb::<embeddingId>` | `LanceDbIndex` | `core/indexing/LanceDbIndex.ts` | 13 |

`vectordb::<embeddingId>` 这个命名很关键：**artifactId 里带 embedding 模型 ID**，意味着换 embedding 模型等价于换一个 artifact，旧索引不冲突、可独立重建。

`LanceDbIndex.ts:44-47`

```ts
relativeExpectedTime: number = 13;
get artifactId(): string {
  return `vectordb::${this.embeddingsProvider?.embeddingId}`;
}
```

### 2.3 完整数据流

```text
workspace
  ↓ walkDirAsync(directory, ide)                core/indexing/walkDir.ts
  ↓ .gitignore / .continueignore / 默认忽略       core/indexing/shouldIgnore.ts, ignore.ts
fileStats = { path → { size, lastModified } }
  ↓
getComputeDeleteAddRemove(tag, fileStats, readFile, repoName)   refreshIndex.ts:395
  ↓ 产出 [RefreshIndexResults, lastUpdated, markComplete]
for each CodebaseIndex in [chunk, codeSnippets, sqliteFts, vectordb]:
    batchRefreshIndexResults(results)          // filesPerBatch = 200
    index.update(tag, subResult, markComplete, repoName)
  ↓
SQLite: ~/.continue/index/index.sqlite
LanceDB: ~/.continue/index/lancedb/
```

批处理的原因写在源码注释里（`CodebaseIndexer.ts:49-54`）：

> We batch for two reasons: `- To limit memory usage for indexes that perform computations locally, e.g. FTS` `- To make as few requests as possible to the embeddings providers`

### 2.4 为什么不是 Repository → Embedding 一步？（重点问题 A）

**Problem**
不同检索需求需要不同物化形式：符号精确匹配、自然语言语义、符号清单、可引用行号。任一单一物化形式都无法同时满足。

**Solution**
三层解耦：

```text
① Repository 层    文件发现与过滤（walkDir + ignore）
        ↓
② Chunk 层         chunks 表：结构化内容 + 行号（path, idx, startLine, endLine, content）
        ↓
③ Index 层         每个 artifactId 一套物化视图，消费同一份 chunks 或自行重建
```

**Reason**（三条源码依据）

1. **复用**：`FullTextSearchCodebaseIndex` 不自己切分，直接读 `chunks` 表。
   `FullTextSearchCodebaseIndex.ts:59-62`
   ```ts
   const chunks = await db.all(
     "SELECT * FROM chunks WHERE path = ? AND cacheKey = ?",
     [item.path, item.cacheKey],
   );
   ```
   而且在 tag 关联时显式借用 chunks 的 artifactId：`FullTextSearchCodebaseIndex.ts:183-188`
   ```ts
   // Notice that the "chunks" artifactId is used because of linking between tables
   ```
2. **独立重建**：artifact 化后，换 embedding 模型只重建 `vectordb::<newModel>`，chunks / FTS 不受影响。
3. **差异只算一次**：`getComputeDeleteAddRemove` 被调用一次，四类动作分发给全部 artifact；`addTag` 的存在正是因为"内容已被算过"（第 7 节）。

**Trade-off**
- 多了一层存储与管理（chunks 表 + 各 artifact 自己的表）；
- FTS 与 chunks 表**产生耦合**（外键 `chunkId REFERENCES chunks(id)`），不是完全解耦；
- Vector 索引反而**不复用** chunks 表，用 `embeddingsProvider.maxEmbeddingChunkSize` 独立重新切分（`LanceDbIndex.ts:171-196`）。所以"Chunk 与 Vector DB 解耦"在 Continue 里是**部分解耦**：FTS 复用，Vector 重算。

**DevContext Mapping**
采用"Chunk 层 + 多 Index"的分层思路（与 DevContext 定义文档第 7 节一致），但**简化为单表双索引**：

```text
Repository → Parser(Java/Markdown) → knowledge_chunk 单表
                                        ↓            ↓
                                  tsvector(GIN)   embedding(vector)
                                  Keyword Search   Vector Search
```

不照搬"一个 artifact 一套存储"的做法，因为 DevContext 只有 2 个检索通道（FTS + vector），用 PostgreSQL 一张表就能同时承载，不必引入 artifact 抽象层。同时**不重复 Continue 的"两套 chunk"问题**：DevContext 的 keyword 与 vector 必须复用同一份 chunk（同一 `chunk_id`），否则 RRF 融合时无法对齐结果——这恰恰是 Continue 结构里最容易出问题的地方。

---

## 3. Code Chunk 设计（重点问题 B）

### 3.1 Chunk 数据结构

`core/index.d.ts:37-49`

```ts
export interface ChunkWithoutID {
  content: string;
  startLine: number;
  endLine: number;
  signature?: string;                          // 只有 codeSnippets 索引会填
  otherMetadata?: { [key: string]: any };      // 只有 markdown 会填 fragment / title
}

export interface Chunk extends ChunkWithoutID {
  digest: string;     // = 文件内容的 sha256（cacheKey）
  filepath: string;
  index: number;      // 该 chunk 在文件内的序号
}
```

存储 schema `core/indexing/chunk/ChunkCodebaseIndex.ts:156-174`：

```sql
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cacheKey TEXT NOT NULL,
  path TEXT NOT NULL,
  idx INTEGER NOT NULL,
  startLine INTEGER NOT NULL,
  endLine INTEGER NOT NULL,
  content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunk_tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tag TEXT NOT NULL,
  chunkId INTEGER NOT NULL,
  UNIQUE (tag, chunkId)
);
```

### 3.2 分派逻辑

`core/indexing/chunk/chunk.ts:16-52` 决定用哪种 chunker：

```text
若 扩展名 在 supportedLanguages 且 不属于 NON_CODE_EXTENSIONS:
      尝试 codeChunker（tree-sitter AST）
      失败 → 回退 basicChunker
否则 → basicChunker
```

`NON_CODE_EXTENSIONS = ["css","html","htm","json","toml","yaml","yml"]`，注释解释为这些文件没有 codeChunker 期望的类/函数结构。

Java 在支持列表内（`core/util/treeSitter.ts:94` → `java: LanguageName.JAVA`）。

### 3.3 codeChunker：结构化优先，超限再折叠

`core/indexing/chunk/code.ts:246-263` 入口；核心策略在 `:213-244`：

```text
maybeYieldChunk(node):
  若 node 是 root，或 node.type 属于 {class_definition, class_declaration, impl_item,
                                       function_definition, function_declaration,
                                       function_item, method_declaration}
    且 tokenCount(node.text) < maxChunkSize
  → 直接产出整块（原子 Chunk）

否则若 node.type 有 collapsedConstructor：
  → 产出"折叠版"（保留结构轮廓）
  → 然后仍然递归子节点，让子结构在别处以完整形式出现
```

折叠策略（`:110-171`）：

- 函数：`signature + "{ ... }"`，即保留签名、折叠函数体；
- 类：保留类头，把内部函数整体折叠；
- 若折叠后仍超限，按降级顺序依次尝试：
  `类头 + 函数签名体` → `函数签名体` → `签名首行 + 折叠体` → `折叠体`。

**这是"AST-aware chunking"的一个具体工程答案**：不是按字符切，而是按语法节点决定边界；节点过大时不是硬切，而是**先做结构保真的降级折叠**。

### 3.4 basicChunker：token 预算内的行堆积

`core/indexing/chunk/basic.ts:4-47`。按行累计 token，超出 `maxChunkSize - 5` 就切块；单行超过 `maxChunkSize` 时**跳过该行**（不产出）。

### 3.5 markdownChunker：heading-aware 递归切分

`core/indexing/chunk/markdown.ts:61-147`。对应 DevContext 的 "Heading-aware Chunk"：

```text
若整篇 token <= maxChunkSize  → 整体一个 chunk（保留 header 元数据）
否则按 hLevel 级标题切分 → 递归 hLevel+1
若 hLevel > 4 仍超限        → 退回 basicChunker（token 级切分）
```

递归时**子 chunk 会重新拼上父 header**（`:136`）：

```ts
content: `${section.header}\n${chunk.content}`,
```

并保留元数据（`:139-143`）：

```ts
otherMetadata: {
  fragment: chunk.otherMetadata?.fragment || cleanFragment(section.header),
  title:    chunk.otherMetadata?.title    || cleanHeader(section.header),
}
```

`cleanFragment` / `cleanHeader`（`:6-55`）把标题清洗成可检索的形式：去 Markdown 语法字符、去链接、转小写、空格转连字符（fragment 形态）。这相当于一个轻量的"anchor / heading_path 规范化"。

### 3.6 一个必须指出的关键差异

**Continue 的 RAG chunk 不携带任何符号元数据。**

`chunks` 表只有 `path / idx / startLine / endLine / content`。没有 `class_name`、`method_name`、`package`、`annotations`。

Continue 里确实存在符号信息，但在**另一个索引**里：`CodeSnippetsCodebaseIndex`。

`core/indexing/CodeSnippetsIndex.ts:43-53`

```sql
CREATE TABLE IF NOT EXISTS code_snippets (
  id INTEGER PRIMARY KEY, path TEXT NOT NULL, cacheKey TEXT NOT NULL,
  content TEXT NOT NULL,
  title TEXT NOT NULL,        -- 符号名，如 purchaseTicket
  signature TEXT,             -- 签名，如 public void purchaseTicket(...)
  startLine INTEGER NOT NULL, endLine INTEGER NOT NULL
);
```

它用 tree-sitter query 提取（`:182-209`），Java 的 query 在 `extensions/vscode/tree-sitter/code-snippet-queries/java.scm`：

```scheme
(class_declaration name: (_) @name.definition.class interfaces: (_) @interfaces) @definition.class
(method_declaration type: (_) @return_type name: (_) @name.definition.method parameters: (_) @parameters) @definition.method
(interface_declaration name: (_) @name.definition.interface) @definition.interface
```

而这个索引的用途是 **repo map / 符号清单**（`CodeSnippetsIndex.ts:395-443` `getPathsAndSignatures` 返回 `{ 文件路径 → [signature...] }`），以及 `@Code` 上下文提供者的候选列表（`CodeContextProvider` 的 `dependsOnIndexing: ["chunk","codeSnippets"]`）。

**结论**：Continue 中"用于 RAG 的 chunk"和"符号元数据"是**两套并存但不 join 的数据**。

**DevContext Mapping**
这是 DevContext 必须**主动偏离** Continue 的地方。DevContext 的核心场景是 DOC / CODE / MIXED 检索，其中 MIXED 需要"符号 → 代码位置 → 设计文档"的联动。如果 chunk 不带 `class_name` / `method_name` / `annotations`，就无法：

- 对符号字段做结构化加权（keyword 检索里 `method_name` 应比正文权重高）；
- 生成 `TicketService.java Lines 120-188` 这种可验证 citation；
- 在 RRF 融合后按符号去重 / 打分。

所以 DevContext 的 `CodeChunk V1 Schema` 在 Continue 基础上**必须加字段**（见第 9 节）。

---

## 4. Full-text Index（重点问题 C 之一）

> **官方依据（Q3 的直接答案）**：`docs/reference/deprecated-codebase.mdx` 在描述 `@Codebase` 原始设计时写明：
>
> > Continue indexes your codebase so that it can later automatically pull in the most relevant context from throughout your workspace. **This is done via a combination of embeddings-based retrieval and keyword search.** By default, all embeddings are calculated locally using `transformers.js` and stored locally in `~/.continue/index`.
>
> 这是"为什么同时存在 Full Text Search 与 Vector Search"的官方说明：二者是**互补**而非替代关系。

`core/indexing/FullTextSearchCodebaseIndex.ts`

| 项 | 值 | 位置 |
| --- | --- | --- |
| artifactId | `sqliteFts` | `:26` |
| 引擎 | SQLite FTS5 虚拟表 | `:31-35` |
| 分词器 | `trigram` | `:34` |
| 字段权重 | 路径权重 10.0 | `:28` |
| 排序 | `bm25`，路径加权 | `:166` |
| 阈值 | `rank <= -2.5` | `:124-127` + `core/util/parameters.ts` |

建表（`:31-45`）：

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(path, content, tokenize = 'trigram');

CREATE TABLE IF NOT EXISTS fts_metadata (
  id INTEGER PRIMARY KEY, path TEXT NOT NULL, cacheKey TEXT NOT NULL,
  chunkId INTEGER NOT NULL,
  FOREIGN KEY (chunkId) REFERENCES chunks (id),
  FOREIGN KEY (id) REFERENCES fts (rowid)
);
```

检索查询（`:157-169`）：

```sql
SELECT fts_metadata.chunkId, fts_metadata.path, fts.content, rank
FROM fts
JOIN fts_metadata ON fts.rowid = fts_metadata.id
JOIN chunk_tags ON fts_metadata.chunkId = chunk_tags.chunkId
WHERE fts MATCH ?
  AND chunk_tags.tag IN (...)
  AND fts_metadata.path IN (...)      -- 可选目录过滤
ORDER BY bm25(fts, 10.0)
LIMIT ?
```

**两个值得注意的实现选择：**

1. **trigram 分词器而不是默认 unicode61。** trigram 让 FTS 能做**子串匹配**，这对代码标识符至关重要：搜 `Ticket` 能命中 `TicketServiceImpl`、`purchaseTicket`。代价是索引体积更大。
2. **`bm25(fts, 10.0)`：`path` 列权重 10 倍。** 命中文件路径的查询（例如问题里出现文件名）排序会更靠前。这是一个很轻量的"字段加权"实现。

查询侧还要做一次 trigram 构造（`core/context/retrieval/pipelines/BaseRetrievalPipeline.ts:98-139`）：用 `wink-nlp` 做 stemming、去停用词、取 3-gram，再用 ` OR ` 连接成 FTS 查询串。也就是说**发到 FTS 的不是原始自然语言，而是清洗后的 trigram 集合**。

注意 SQLite FTS5 的 `bm25()` 返回**负值，越小越相关**，所以 `rank <= -2.5` 是"过滤掉弱匹配"。

---

## 5. Vector Index（重点问题 C 之二）

`core/indexing/LanceDbIndex.ts`

| 项 | 值 | 位置 |
| --- | --- | --- |
| 引擎 | LanceDB（嵌入式向量库） | `:67` 动态 import |
| artifactId | `vectordb::<embeddingId>` | `:44-47` |
| 表名 | `tagToString(tag)` 净化后 | `:84-86` |
| 向量旁路缓存 | SQLite `lance_db_cache` | `:88-98` |
| chunk 大小 | `embeddingsProvider.maxEmbeddingChunkSize` | `:183` |

`lance_db_cache` 建表（`:88-98`）：

```sql
CREATE TABLE IF NOT EXISTS lance_db_cache (
  uuid TEXT PRIMARY KEY, cacheKey TEXT NOT NULL, path TEXT NOT NULL,
  artifact_id TEXT NOT NULL, vector TEXT NOT NULL,
  startLine INTEGER NOT NULL, endLine INTEGER NOT NULL, contents TEXT NOT NULL
);
```

设计要点：

- 向量**双写**：向量本体进 LanceDB，同时以 JSON 形式留一份在 SQLite 旁路缓存。之所以这么做，从 `:237-252` 的 `parseVector` 注释可以推断是为了容错与复用（历史索引里存在向量 JSON 缺外层 `[]` 的 bug，旁路缓存让这些索引仍能工作）。
- `retrieve()`（`:430-494`）：先把 query 用 `basicChunker` 切到 embedding 模型上限，取**第一个 chunk** 作为查询向量；对每个 tag 检索后合并，按 `_distance` 升序取 top-n；最后回 SQLite 取回完整 chunk。
- 目录过滤时把 limit 放大到 300（`:421-425`），即"目录内先取 300 再截断到 n"。

---

## 6. 多 Index 的关系

### 6.1 依赖声明式

上下文提供者声明自己依赖哪些索引（`core/index.d.ts:182-195`）：

```ts
export type ContextIndexingType = "chunk" | "embeddings" | "fullTextSearch" | "codeSnippets";

export interface ContextProviderDescription {
  // ...
  dependsOnIndexing?: ContextIndexingType[];
}
```

`core/context/providers/CodebaseContextProvider.ts:10-18`

```ts
dependsOnIndexing: ["embeddings", "fullTextSearch", "chunk"],
```

`CodebaseIndexer.getIndexesToBuild()`（`CodebaseIndexer.ts:169-206`）取所有 provider 的 `dependsOnIndexing` 并集，再按映射表实例化索引，且**串行构建**（注释：`not parallelizing to avoid race conditions in sqlite`）。

**意义**：是否索引某个 artifact，由"用户实际配置了哪些上下文提供者"决定，而不是无脑全建。

### 6.2 命名空间：IndexTag

`core/index.d.ts:802-804`

```ts
export interface IndexTag extends BranchAndDir { artifactId: string; }
```

序列化在 `core/indexing/utils.ts:29-54`，格式 `"{directory}::{branch}::{artifactId}"`，并对超长路径做哈希前缀截断（OS 文件名 255 字符限制）。

**这就是"Isolation 的最小单位"**：同一份代码在分支 A 和分支 B 上是两套 tag，互不干扰；同一 tag 下四个 artifact 各自独立。

### 6.3 跨 workspace 复用：global_cache

`core/indexing/refreshIndex.ts:359-369, 469-523`。`global_cache(cacheKey, dir, branch, artifactId)` 记录"某个文件 hash 已经在某些 tag 下被索引过"。这带来 `addTag` 语义：**文件内容没变但出现在新的 tag 下时，不重算，只挂 tag。**（本地场景收益一般，多 workspace / 远程缓存场景收益明显。）

### 6.4 DevContext Mapping

DevContext 不需要 artifact 抽象、不比分支隔离、不需要 global_cache。简化为 `repository_id` 一个命名空间维度即可。

但 **`addTag` 这个思想值得保留**：当代码只被移动/复制（内容 hash 不变）时，应当复用已有 embedding，而不是重新调用 embedding API。这是 P1 增量索引里成本最低、收益明确的一条。

---

## 7. Incremental Index（重点问题 D）

### 7.1 状态表

`core/indexing/refreshIndex.ts:24-88`

```sql
CREATE TABLE IF NOT EXISTS tag_catalog (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dir STRING NOT NULL, branch STRING NOT NULL, artifactId STRING NOT NULL,
  path STRING NOT NULL, cacheKey STRING NOT NULL, lastUpdated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS global_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cacheKey STRING NOT NULL, dir STRING NOT NULL, branch STRING NOT NULL,
  artifactId STRING NOT NULL
);
CREATE TABLE IF NOT EXISTS indexing_lock (
  id INTEGER PRIMARY KEY AUTOINCREMENT, locked BOOLEAN NOT NULL,
  timestamp INTEGER NOT NULL, dirs STRING NOT NULL
);
```

### 7.2 hash 算法

`refreshIndex.ts:371-375`

```ts
function calculateHash(fileContents: string): string {
  const hash = crypto.createHash("sha256");
  hash.update(fileContents);
  return hash.digest("hex");
}
```

**hash 的是文件全文，不是元数据。** 这个 hash 就是 `cacheKey`，贯穿所有表。

### 7.3 变更判定流程

`getAddRemoveForTag()`（`refreshIndex.ts:138-353`）：

```text
输入: currentFiles = { path → { size, lastModified } }（来自 IDE）

1. 丢弃 size > 5MB 的文件                              :135-157
2. 读 tag_catalog 中该 tag 已有记录                    :159
3. 按 path 分组，取每个 path 的 latest hash 与时间戳    :166-190
4. 对每个已知 path:
     文件已不存在        → remove（该 path 的所有历史版本）      :194-198
     文件存在:
       latest.lastUpdated < file.lastModified ?              :201
         是 → 重算 sha256                                    :203
                hash 变了  → updateNewVersion(新hash) + updateOldVersion(旧hash)   :204-211
                hash 没变  → updateLastUpdated（只更新时间，不动内容）              :212-220
         否 → 什么都不做（已经是最新的）                        :221-223
5. 剩下的 files 就是全新文件 → 并发读文件算 hash（并发度 10）  :230-239
```

注意第 4 步的短路：**先比 mtime，mtime 变了才重算 hash**。这是避免对大仓库每次全量读文件的关键优化。

并发度限制的注释（`:230-233`）值得一读：

> limit to only 10 concurrent file reads to avoid issues such as "too many file handles". A large number here does not improve throughput due to the nature of disk or network i/o

### 7.4 compute vs addTag

`getComputeDeleteAddRemove()`（`refreshIndex.ts:395-467`）：

```text
对每个待 add 的 {path, cacheKey}:
   若 global_cache 里已有该 (cacheKey, artifactId) → addTag   （不重算）
   否则                                            → compute

对每个待 remove 的 {path, cacheKey}:
   若该 cacheKey 还被 >1 个 tag 引用 → removeTag （只摘 tag）
   否则                             → del        （真正删除）
```

然后 `markComplete` 回调（`:254-332`）统一落地到 `tag_catalog`，并顺带更新 `global_cache`（`:441-465`）。

### 7.5 各 Index 如何消费

`ChunkCodebaseIndex.update()`（`core/indexing/chunk/ChunkCodebaseIndex.ts:31-154`）是四类动作的完整样板：

```text
compute  → packToChunks → insertChunks（事务批量插入）+ INSERT chunk_tags
addTag   → INSERT INTO chunk_tags (chunkId, tag) SELECT id, ? FROM chunks WHERE cacheKey = ?
removeTag→ DELETE FROM chunk_tags WHERE tag = ? AND chunkId IN (...)
del      → DELETE FROM chunks WHERE id = ?  + DELETE FROM chunk_tags WHERE chunkId = ?
```

注意 `addTag` 的 SQL：**完全靠 cacheKey 找回已算好的 chunks**，一行 SQL 完成，不重新解析文件。这就是 hash 作为全局主键的收益。

### 7.6 其它工程细节

- **文件级刷新**：`refreshFile()`（`CodebaseIndexer.ts:241-291`）针对单文件走同一套流程，用于保存文件后即时更新。
- **批处理**：`batchRefreshIndexResults()`（`:532-550`），`filesPerBatch = 200`，注释说明是为控制本地计算型索引（FTS）的内存与减少 embedding 请求次数。
- **并发写保护**：`IndexLock`（`refreshIndex.ts:546-588` + `CodebaseIndexer.ts:689-713`）。多窗口同时索引时，后到的窗口等待；若持有者超过 10 秒没更新时间戳则判定为陈旧并解锁。
- **错误处理**：`errorsRegexesToClearIndexesOn`（`:76-83`）。只有 `SQLITE_CONSTRAINT / SQLITE_CORRUPT / SQLITE_IOERR / SQLITE_FULL` 等结构性错误才建议清空索引重建（`shouldClearIndexes`）；`SQLITE_BUSY` 之类不重建。
- **单索引失败不阻断**：`indexFiles()`（`:613-632`）把某个 artifact 的错误收进 `warnings` 继续跑下一个 artifact。

### 7.7 结论：DevContext V1 全量重建是否可接受

**可接受，并且有官方依据。** `docs/guides/custom-code-rag.mdx`（Step 5）明确写道：

> In a perfect production version, you would want to build "automatic, incremental indexing" ... That said, we highly recommend first building and testing the pipeline before attempting this. Unless your codebase is being entirely rewritten frequently, an incremental refresh of the index is likely to be sufficient and reasonably cheap.

以及：

> you should probably run it by hand ... Because codebases are largely unchanged in short time frames, you won't want to re-index more than once a day.

**DevContext P1 演进路径**（按成本从低到高）：

1. 加 `content_hash` 列（sha256），入库前比对，未变则跳过 —— 最小改动即获得 Continue 80% 的增量收益；
2. 加"文件消失则删除其 chunk"，即 Continue 的 `del` 动作；
3. 加 `compute / del / addTag` 三态区分（内容已存在则只挂 `repository_id`），用于仓库移动/复制场景；
4. mtime 短路优化 —— 仅在仓库规模大到全量读文件成为瓶颈时才需要。

---

## 8. 查询期的 Retrieval 与 Context 组装

> 这一节只分析"多个 Index 的结果如何合成 Context"，不涉及 Chat / Prompt / UI。

### 8.1 两个 Pipeline

`core/context/retrieval/retrieval.ts:14-...` 按是否配置 reranker 选择：

```text
useReranking ? RerankerRetrievalPipeline : NoRerankerRetrievalPipeline
```

默认参数（`core/context/retrieval/retrieval.ts` + `core/util/parameters.ts`）：

```ts
RETRIEVAL_PARAMS = {
  rerankThreshold: 0.3, nFinal: 20, nRetrieve: 50,
  bm25Threshold: -2.5,
  nResultsToExpandWithEmbeddings: 5, nEmbeddingsExpandTo: 5,
};
// retrieval.ts: nFinal = min(25, contextLength / 512 / 2)
//               nRetrieve = useReranking ? 2 * nFinal : nFinal
```

### 8.2 四路召回 + 配额融合（Non-Reranker）

`NoRerankerRetrievalPipeline.ts:12-89`：

```text
// We give 1/4 weight to recently edited files, 1/4 to full text search,
// and the remaining 1/2 to embeddings
recentlyEditedNFinal = nFinal * 0.25;
ftsNFinal           = nFinal * 0.25;
embeddingsNFinal    = nFinal - 0.25*nFinal - 0.25*nFinal;   // = 0.5 * nFinal
```

四路来源：

| 来源 | 方法 | 性质 |
| --- | --- | --- |
| FTS | `retrieveFts` | keyword / symbol |
| Embeddings | `retrieveEmbeddings` | semantic |
| Recently edited files | `retrieveAndChunkRecentlyEditedFiles` | IDE 上下文（LRU + 打开的文件） |
| Repo map | `requestFilesFromRepoMap` | LLM 选文件 |
| （实验）Tool calling | `retrieveWithTools` | grep / glob / ls / readFile |

融合方式：**直接按配额切片后拼接**（`:69-74`），然后去重（`:85-88`）。

去重规则（`core/context/retrieval/util.ts:4-12`）：

```ts
a.filepath === b.filepath && a.startLine === b.startLine && a.endLine === b.endLine
```

**注意：Continue 在这里没有做 RRF，也没有做分数归一化。** 它是"配额分配 + 按位置去重"，不同来源的相对排序没有被融合。这是 DevContext 与 Continue 的一个重要分歧点（见第 9/10 节）。

### 8.3 Reranker 路径

`RerankerRetrievalPipeline.ts:88-124`：

```text
retrieve(nRetrieve=50)  →  rerankModel.rerank(query, chunks) → 按分数降序 → 取 nFinal
```

异常时降级为 `chunks.slice(0, nFinal)`（不做精排但不出错）。源码里还留有一段被注释掉的"用 top 结果再扩散 embedding 并二次 rerank"的实验代码（`:126-168`），以及 TODO：

```text
// Source: expansion with code graph
// Source: Open file exact match
// Source: Class/function name exact match
```

这几行 TODO 本身就是有价值的信号：**Continue 自己也认为"精确符号匹配"和"图扩展"应该进入召回，但尚未系统实现。**

### 8.4 Citation 格式

`core/context/retrieval/retrieval.ts`：

```ts
name: r.startLine === -1
  ? baseName
  : `${baseName} (${r.startLine + 1}-${r.endLine + 1})`,
content: `\`\`\`${relativePathOrBasename}\n${r.content}\n\`\`\``,
```

即 `TicketService.java (120-188)`（**1-based 展示**，内部 0-based）。

**DevContext Mapping**：这验证了 DevContext 定义文档第 20 节的 citation 形式 `TicketService.java Lines 120-188` 是可行且成本极低的——只要 chunk 保留 `startLine/endLine` 就能生成。而 `relativePathOrBasename` 的做法（优先相对路径，跨目录时退化为 basename）对 Java 多模块项目尤其合适。

### 8.5 Context 末尾的指令

`retrieval.ts` 会在结果前插入一段固定指令（`INSTRUCTIONS_BASE_ITEM`），大意是"只依据上述代码回答，尽量引用文件名，信息不足时说明"。无检索结果时，Agent 模式下返回"没有找到结果，试试其他工具"。

这说明一个工程细节：**Context 组装不仅要选内容，还要带"如何使用这些内容"的约束**，否则模型会引入外部记忆。DevContext 的 Context Builder 应保留这一点。

### 8.6 重要发现：@Codebase 已被官方标记为 deprecated

这是本次调研中最值得记录的发现之一。

`docs/reference/deprecated-codebase.mdx` 开头：

> **This feature is deprecated.** The `@Codebase` context provider has been deprecated in favor of a more integrated approach to codebase awareness.

`docs/guides/codebase-documentation-awareness.mdx` 给出迁移方向：

> 1. **Use built-in tools**: Agent mode can now use file exploration and search tools to understand your codebase
> 2. **Add rules**: Create `.continue/rules` files to provide context about your project structure
> 3. **Use MCP servers**: For external codebases, use DeepWiki MCP or custom MCP servers

也就是说：**Continue 把"索引 + 检索"退居为可选路径，主推"Agent 用工具自己探索"。**

但代码层面并未删除：

- 四个索引 artifact 全部保留（`CodebaseIndexer.ts:176-195`）；
- 检索 pipeline 全部保留（`core/context/retrieval/pipelines/*`）；
- 同时存在实验开关 `config.experimental?.codebaseToolCallingOnly`（`NoRerankerRetrievalPipeline.ts:60`、`RerankerRetrievalPipeline.ts:57`），为 true 时**用工具检索替代索引检索**。

**如何理解这个转向（本次调研的判断）：**

**Problem**
索引检索（embedding + FTS）是"一次性预计算 + 固定召回"，对"符号在哪"这类问题不够精确；而对"帮我实现 X"这类开放式任务，Agent 迭代式地 grep/read 往往更有效。

**Reason**
工具检索可以在多轮里根据中间结果修正查询，而一次性检索只有一次机会；且工具检索不需要维护索引一致性。

**Trade-off**
工具检索的延迟更高（多轮 LLM 调用）、token 成本更高、需要模型具备工具调用能力、无法离线；索引检索延迟低、可缓存、成本稳定。

**DevContext Mapping（关键取舍）**
DevContext 明确不做 Coding Agent（定义文档第 29/30 节 Non-goals），所以**不采用工具检索作为主路径**。但这个事实带来三点输入：

1. **不能宣称"索引检索在业界是唯一正确答案"。** 应该把 Continue 的转向作为一个诚实的对照写在 README / 面试材料里——这反而是一个加分项，说明调研到了最新动向。
2. **这正好解释了 DevContext 保留 LangGraph Router 的价值。** Continue 的 pipeline 是固定四路召回，问题类型不影响召回策略；DevContext 的 DOC / CODE / MIXED 路由正是对"不同问题需要不同召回策略"的回答。
3. **LangGraph State 里应保留一个"检索充分性"评估节点**，这与 Agent 迭代式检索在精神上一致，但用受控的 retry（MAX_RETRY=2）实现，成本可控。

### 8.7 一个必须诚实说明的空白

在本次下载的仓库快照中，**未找到 Continue 对检索质量的评测代码**（grep `recall@` / `MRR` / `reciprocal_rank` 无结果；仓库根目录 `eval/` 只有一个 `.gitignore`）。官方文档也没有给出"混合检索相对纯向量检索提升多少"的量化对比。

因此：

> **关于"Continue 的 Full-text + Vector 混合检索到底比纯向量检索提升了多少"，当前调研无法确认。**

这一点对 DevContext 反而是支撑：DevContext 定义文档第 21 节把 Evaluation 列为强制模块，正是要补上这个缺口。

---

## 9. DevContext 借鉴什么

**1. 分层：Repository → Chunk → 多 Index（而不是 Repository → Embedding）**
Continue 用三层结构证明了"独立 Chunk 层"的价值：可复用、可独立重建、差异只算一次。DevContext 采用同样的分层，但落在单库单表上（第 11 节）。

**2. Chunk 作为共享中间层**
FTS 直接读 `chunks` 表，不重新切分。DevContext 的 FTS 与 vector 必须共用同一份 `knowledge_chunk`，保证 RRF 能在 `chunk_id` 上对齐。**这是 Continue 结构里最值得抄的一条。**

**3. content hash（sha256 全文）作为贯穿索引的主键**
`digest = sha256(fileContents)` 同时充当 `cacheKey`、去重键、增量判断依据。DevContext 的 `knowledge_chunk.content_hash` 沿用这个设计。

**4. 结构化优先、超限降级的 chunk 策略**
Continue：AST 节点能整块保留就整块保留，超限先做"结构保真折叠"，最后才退回 token 级切分。
DevContext 对应：Java 方法整块保留 → 超限时在方法内部按 token 二次切分，但**必须保留 class / method / file / line 元数据**（调研需求第十四节的要求）。这个"降级链"的设计模式直接可用。

**5. Markdown 递归按标题切分，并保留父标题前缀**
markdownChunker 在递归时把父 header 拼回子 chunk（`:136`）。DevContext 的 `heading_path` 字段承担同样职责，且用 `heading_path` 存完整层级比 Continue 的 `otherMetadata.title/fragment` 更结构化。

**6. 字段加权（bm25 的 pathWeightMultiplier = 10.0）**
很轻量但有效。DevContext 的 PostgreSQL FTS 用 `setweight()` 实现同类效果：`file_path / class_name / method_name` 给 A 权重，`content` 给 B/C。

**7. 每个 chunk 保留 startLine / endLine → citation**
Continue 的 `TicketService.java (120-188)` 与 DevContext 定义文档第 20 节的 citation 形式一致，可行性已被验证。

**8. "索引计划 / 索引执行"分离**
`RefreshIndexResults{compute, del, addTag, removeTag}` 是一个可复用的抽象：**变更计算与变更落地解耦**。DevContext V1 可以只用 `compute/del` 两态，但接口设计上留出 `addTag` 的位置，便于 P1 演进。

**9. 检索结果的结构化去重键**
`(filepath, startLine, endLine)`。DevContext 由于共用 chunk，去重键可以直接用 `chunk_id`，更简单也更可靠。

**10. Context 组装时附带"使用约束"**
检索结果前插入"只依据以上内容回答、引用文件名、信息不足就说明"的指令，抑制模型编造。DevContext 的 Context Builder 应保留。

**11. 索引失败不阻断 + 结构性错误才重建**
`warnings` 收集 + `shouldClearIndexes` 白名单。DevContext 的 ingestion 流程可以照搬这个容错姿态。

---

## 10. DevContext 不借鉴什么

**1. 不采用 tree-sitter 多语言方案，改用 JavaParser**
Continue 为支持 20+ 语言，付出的是：wasm parser 加载、per-language `.scm` query、多语言 collapse 分支、以及"每个 chunker 都是弱类型语法节点字符串匹配"的代价（见 `code.ts` 里大量的 `node.type` 字符串判断）。
DevContext 是 **Java Only**，用 JavaParser 能拿到真正强类型的 AST：`MethodDeclaration`、`AnnotationExpr`、`Range`。这不是"JavaParser 更专业"，而是**语言范围收窄后，通用方案的复杂度完全没有必要承担**。

**2. 不采用"chunk 不带符号元数据"的设计**
Continue 的选择有其理由（多语言下"类/方法名"的通用抽取很难做对，所以它把符号信息拆到独立的 `codeSnippets` 索引，并只用于 repo map）。但 DevContext 的核心场景（CODE / MIXED 检索、符号加权、可验证 citation）**必须**在 chunk 上直接携带 `package_name / class_name / method_name / annotations`。这是 DevContext 与 Continue 最本质的差异。

**3. 不采用 SQLite FTS5 + LanceDB 双引擎**
Continue 是**本地 IDE 插件**：无服务端、离线优先、单用户、要避免任何独立进程 → 嵌入式引擎（SQLite / LanceDB）是唯一合理选择。
DevContext 是**服务端系统**，且 PostgreSQL 已能同时提供 FTS 与 vector（pgvector）。引入第二个存储引擎只会增加运维面，不带来检索质量提升。

**4. 不采用"配额式"融合，改用 RRF**
Continue 的 `1/4 recent + 1/4 FTS + 1/2 embedding` 是**按配额切片拼接**，不同来源的排序之间没有可比的统一分数，也没有跨来源的重排。它解决了"每路都要有代表"的问题，但没有解决"哪条更相关"的问题。
DevContext 采用 RRF：`score(d) = Σ 1/(k + rank_i(d))`，k 通常取 60。理由与 DevContext 定义文档第 15 节一致：vector similarity（0.86）与 BM25 分数（7.2，或 FTS5 的负值）不在同一评分空间，直接比较无意义；按**排名**融合才是可解释、可评测的做法。
需要说明的是：**这不是因为 RRF 在绝对意义上更好，而是因为 DevContext 只有两路召回（FTS + vector）且需要可评测的融合结论。** 两路加权的 RRF 参数量小、可解释；Continue 的四路配额则在"来源多样化"上有其合理性。

**5. 不采用 IDE 特有信号**
`recently edited files`（LRU 缓存 + 打开的文件）、`repo map`（LLM 选文件，本质是第二次 LLM 调用）、`codebaseToolCallingOnly` 的工具检索。DevContext 没有 IDE 上下文，也不是 Agent。

**6. 不采用 global_cache / 远程索引缓存**
`ContinueServerClient.getFromIndexCache`（`ChunkCodebaseIndex.ts:42-60`）与 `global_cache` 服务于"多用户 / 多云 workspace 共享索引"场景。DevContext 单仓库本地运行，不需要。

**7. 不采用 IndexTag 的多分支隔离**
`{directory}::{branch}::{artifactId}` 是为了在一个 IDE 里同时处理多个仓库和分支。DevContext 用 `repository_id` 一个维度即可。

**8. 不采用向量 JSON 旁路缓存**
`lance_db_cache` 存一份 `vector TEXT`（JSON 序列化）是为绕开 LanceDB 的历史 bug 与容错。pgvector 原生存储向量，不需要旁路表。

---

## 11. DevContext 如何简化实现

| Continue 机制 | 源码位置 | DevContext 简化方案 |
| --- | --- | --- |
| 4 个 artifactId + `IndexTag` 命名空间 | `CodebaseIndexer.ts:176-195`、`utils.ts:29-54` | 单表 `knowledge_chunk` + `repository_id`；FTS 与 vector 是同一行的两个索引 |
| `RefreshIndexResults{compute, del, addTag, removeTag}` | `types.ts:32-37` | V1 只保留 `compute / del`；用 `content_hash` 比对决定 |
| `chunks` 表（ChunkCodebaseIndex） | `ChunkCodebaseIndex.ts:156-174` | `knowledge_chunk` 表，`content_hash` 唯一键 |
| 独立 `fts` 虚拟表 + `fts_metadata` + `chunk_tags` 三表关联 | `FullTextSearchCodebaseIndex.ts:31-45` | 一张表的生成列 `tsvector` + GIN 索引，不需要关联表 |
| `bm25(fts, 10.0)` 路径加权 | `FullTextSearchCodebaseIndex.ts:166` | `to_tsvector(setweight(...))`，给 `file_path/class_name/method_name` A 权重 |
| trigram 分词 + wink-nlp 三元组查询改写 | `:34` + `BaseRetrievalPipeline.ts:98-139` | PostgreSQL `pg_trgm` 可做到子串匹配；V1 用 `websearch_to_tsquery` / `plainto_tsquery` 即可，trigram 作为 P1 优化 |
| LanceDB + `lance_db_cache` 双写 | `LanceDbIndex.ts:88-98` | `knowledge_chunk.embedding vector(N)` + HNSW/IVFFlat 索引，单一存储 |
| Vector 索引用 `maxEmbeddingChunkSize` **独立重新切分** | `LanceDbIndex.ts:171-196` | **不重复切分**，直接复用同一份 chunk（避免两套 chunk 不对齐，这是 DevContext 明确要避开的坑） |
| 四路配额融合（1/4 + 1/4 + 1/2） | `NoRerankerRetrievalPipeline.ts:12-19` | 两路 RRF：`score = Σ 1/(60 + rank_i)` |
| 去重键 `(filepath, startLine, endLine)` | `retrieval/util.ts:4-12` | 直接按 `chunk_id` 去重 |
| citation `basename (startLine+1-endLine+1)` | `retrieval.ts` | 一致：`TicketService.java Lines 120-188` |
| IndexLock 多窗口互斥 | `refreshIndex.ts:546-588` | 不需要（单进程 ingestion） |
| 远程索引缓存 | `ChunkCodebaseIndex.ts:42-60` | 不需要 |

**一句话总结简化方向**：Continue 的复杂度主要来自"本地 IDE + 多语言 + 多 workspace + 离线"。DevContext 去掉这三个前提后，"多 Index"退化为"单表双索引"，"增量索引"退化为"content_hash 比对"，"融合"从配额退化为明确的 RRF。

---

## 12. 哪些功能放 P1

按 DevContext 的决策优先级（Retrieval Quality > Evaluation > Agent Workflow > Engineering Completeness > UI），以及 Continue 提供的成本证据排序：

**P1（成本低、收益明确，建议做）**

1. **content_hash 增量索引**。Continue 用 sha256 + `tag_catalog` 实现，机制清晰、改动面小。P1 只需 `compute/del` 两态即可获得主要收益。
2. **Cross-encoder Rerank**。Continue 的 `RerankerRetrievalPipeline` 给出了标准的 `Recall → Candidate Set → Reranker → Final TopK` 结构，并带降级路径（rerank 失败则退回截断）。DevContext 可复用同样的"召回 50 → 精排 → 取 20"配置。
3. **`pg_trgm` 子串匹配**。Continue 的 FTS 用 trigram 分词正是为了标识符子串匹配（搜 `Ticket` 命中 `TicketServiceImpl`）。PostgreSQL 默认分词器做不到这一点，P1 建议补 `pg_trgm`。

**P2（有价值但成本或收益不匹配）**

4. **符号清单 / RepoMap 式的"结构总览"context**。Continue 的 `getPathsAndSignatures`（`CodeSnippetsIndex.ts:395-443`）产出 `{ 文件 → [签名] }`，是一种很省 token 的全局视图。DevContext 的 chunk 本身带符号，不需要额外索引；但"给 LLM 一份项目符号总览作为压缩 context"是可选的 P2 优化。
5. **索引计划抽象（compute/del/addTag 三态）**。V1 用不上，但接口留位。
6. **按文件/按目录过滤检索**（Continue 的 `filterPaths` / `filterDirectory`）。DevContext 若有"只在某模块内检索"的需求再补。

**明确不做（放在"不借鉴"清单里）**

- global_cache / 远程索引缓存 / IndexTag 多分支隔离 / 向量旁路缓存 / IDE 上下文信号 / 工具检索主路径。

---

## 13. Continue 部分结论速览

| DevContext 问题 | Continue 的做法 | DevContext 最终决定 |
| --- | --- | --- |
| Repository → Context 有哪些阶段 | walkDir → 差异计划 → Chunk 层 → 4 个 artifact | 采纳分层；简化为 Parser → Chunk 层 → 单表双索引 |
| 是否应有独立 Chunk 层 | 有（`chunks` 表），且被 FTS 复用 | **采纳**，作为 keyword 与 vector 的共同数据源 |
| Chunk 是否与 Vector DB 解耦 | 部分解耦：FTS 复用 chunks；Vector 用不同 maxChunkSize 重新切分 | **完全复用**同一份 chunk（不重复切分） |
| 是否需要多种 Index | 需要，4 种 artifact，各自独立重建 | 需要 2 种（FTS + vector），但落在同一张表 |
| 代码能否固定长度切分 | 不能：AST 优先，超限折叠，最后才退回 token 切分 | 采纳：方法整块 → 内部二次切分，保留元数据 |
| 结构化 Chunk 保留哪些 metadata | 只有 `path / startLine / endLine / content`（符号信息在另一个不 join 的索引里） | **必须扩展**：package / class / method / annotations / heading_path |
| Markdown 与 Java 是否同策略 | 不同：markdownChunker（heading 递归）vs codeChunker（AST） | 不同：Heading-aware vs AST-aware |
| 为何 FTS 与 Vector 并存 | `dependsOnIndexing: ["embeddings","fullTextSearch","chunk"]`，两路各自召回后合并 | 并存：symbol 精确 vs 语义；PostgreSQL FTS + pgvector |
| 多路结果如何融合 | 配额切片拼接（1/4 + 1/4 + 1/2）+ 位置去重，**没有 RRF** | **RRF**（按 rank 融合，可解释、可评测） |
| 增量索引机制 | sha256 cacheKey + tag_catalog + global_cache，compute/del/addTag/removeTag | P1：先做 content_hash 比对（compute/del） |
| V1 全量重建可接受吗 | 官方文档建议先不做增量 | **可接受**，有官方依据 |
| Code Citation | `basename (startLine+1-endLine+1)` | 一致：`TicketService.java Lines 120-188` |
| 检索评测 | 快照中未找到 Recall@K / MRR 评测代码 | DevContext 自建 Benchmark（Evaluation 强制模块） |
| 官方当前方向 | `@Codebase` 已 deprecated，转向 tools + rules | 不走工具检索（Non-goals）；但保留 Router 与受限 retry |

**关键源码索引（后续开发回查用）**

```text
core/indexing/types.ts                    CodebaseIndex / RefreshIndexResults
core/indexing/CodebaseIndexer.ts          索引编排、批处理、IndexLock、artifact 注册表
core/indexing/chunk/ChunkCodebaseIndex.ts chunks 表、四类动作的样板实现
core/indexing/chunk/chunk.ts              chunker 分派、shouldChunk
core/indexing/chunk/code.ts               AST chunker 与折叠降级链
core/indexing/chunk/basic.ts              token 级兜底切分
core/indexing/chunk/markdown.ts           heading-aware 递归切分
core/indexing/FullTextSearchCodebaseIndex.ts  FTS5 trigram + bm25 路径加权
core/indexing/LanceDbIndex.ts             向量索引 + SQLite 旁路缓存
core/indexing/CodeSnippetsIndex.ts        符号索引（title / signature）→ repo map
core/indexing/refreshIndex.ts             差异计划、hash、tag_catalog、global_cache、IndexLock
core/indexing/utils.ts                    tagToString
core/indexing/walkDir.ts / shouldIgnore.ts / continueignore.ts  文件发现与过滤
core/context/retrieval/retrieval.ts        pipeline 选择、nFinal/nRetrieve、citation 组装
core/context/retrieval/pipelines/BaseRetrievalPipeline.ts     FTS 查询改写、embeddings 召回
core/context/retrieval/pipelines/NoRerankerRetrievalPipeline.ts  配额融合
core/context/retrieval/pipelines/RerankerRetrievalPipeline.ts    精排
core/context/retrieval/util.ts            deduplicateChunks
core/util/parameters.ts                   RETRIEVAL_PARAMS
docs/reference/deprecated-codebase.mdx    @Codebase 废弃说明（含原始设计说明）
docs/guides/custom-code-rag.mdx           chunk 策略三档、增量索引建议、rerank 流程
docs/guides/codebase-documentation-awareness.mdx  迁移方向（tools + rules）
```

---

## 14. 当前调研无法确认的事项

按调研需求第三十四节要求，以下结论本次无法从源码或官方资料中确定，**不做推测**：

1. **Continue 混合检索相对纯向量/纯 FTS 的量化提升幅度。** 仓库快照中未找到检索质量评测代码，官方文档也未给出对照数据。
2. **`relativeExpectedTime` 权重（0.2 / 1 / 13）的标定依据。** 源码中只有数值，没有说明如何测得，也没有找到对应实验记录。
3. **`bm25Threshold = -2.5` 与 `rerankThreshold = 0.3` 的调参依据。** 源码中仅有常量，无注释说明来源。
4. **`@Codebase` 废弃的时间点与后续是否会完全移除索引代码。** 仓库快照无 `.git` 目录，无法查看提交历史；文档只说明"已废弃"，未说明索引相关代码的移除计划。
5. **Continue 是否在服务端（continue.dev 托管侧）另有检索质量评测。** 本次只调研了本地仓库，未访问任何远端服务，无法判断。
