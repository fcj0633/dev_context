# 05 · knowledge_chunk 数据库与数据落库

> 学习路线位置：**Indexing 层**（第 5 篇）
> 重点源码：`sql/001_schema.sql`（44 行）、`src/devcontext/storage.py`（170 行）
> 前置：`01-Chunk数据模型与双检索文本.md`（需要知道 `Chunk` 的 17 个字段）、`02-Java源码到CodeChunk完整链路.md` / `03-Markdown到DocumentChunk完整链路.md`（知道 Chunk 怎么来的）
> 本篇要完整回答的问题：**数据库里的一行到底代表什么？以及「Chunk 变成一行」这一步在物理上发生了什么？**

---

## 1. 这一模块解决什么问题

上游把所有语料收敛成了统一的 `Chunk` 对象（第 01 篇）。这一层负责把 2901 个 `Chunk` 对象变成 2901 行数据库记录，并且让它们能被两种完全不同的方式查询：

```text
关键词查询   → strpos / similarity / unnest  → 需要 keyword_text 列 + pg_trgm 索引
向量查询     → <=> 余弦距离                  → 需要 embedding 列 + 维度固定
元数据过滤   → WHERE repository / source_type → 需要索引与约束
```

它要解决的具体问题有四个：

**问题一：一行里要同时装下"三种不同性质的数据"。**
`content` 是给人看的、`keyword_text` 是给字符串匹配用的、`embedding` 是给向量距离用的。它们来自同一个 `Chunk`，但消费方式完全不同。

**问题二：两类语料必须能被同一次查询命中。**
MIXED 类问题（"为什么要把 Feign 调用移出事务，相关代码在哪里"）要求一次检索同时返回代码与文档。如果分成两张表，就必须写两次查询再在应用层合并——而合并时又会撞上"两种分数不可比"的问题。

**问题三：全量重建必须是安全的。**
`ingest` 会删掉整个 repository 的数据再重写。如果这个过程不是原子的，任何失败都会留下"索引空了一半"的状态——这是最难排查的一类故障。

**问题四：重跑一次不能改变结果。**
同一份语料 + 同一份代码，重建两次，库里的内容必须一致。

这一层的产出，是后面所有篇章（06 / 07 / 08 / 09）的**唯一数据源**。所以任何一个列设计错了，下游的检索能力就有一个无法修补的上限。

---

## 2. 在完整系统中的位置

```text
                    ┌──────────────── Python 进程 ────────────────┐
Java Parser ──┐     │                                            │
              ├──→  Chunk 列表（2901 个）                          │
Markdown Parser ┘   │      ↓                                     │
                    │  embedding_text() → SHA256 → Cache/API      │
                    │      ↓                                     │
                    │  vectors（2901 个 1024 维数组）               │
                    │      ↓                                     │
                    │  ★ 本篇：storage.py                        │
                    │      ├─ initialize()      建表 / 建索引      │
                    │      └─ replace_repository()  事务化替换      │
                    └──────────────────┬─────────────────────────┘
                                       │ psycopg3
                                       ↓
                    ┌──────── PostgreSQL 18 + pgvector 0.8.6 ─────┐
                    │  表：knowledge_chunk（唯一一张表）             │
                    │  扩展：vector、pg_trgm                        │
                    │  索引：repository / source_type / chunk_type  │
                    │        file_path / GIN(keyword_text)         │
                    └──────────────────┬─────────────────────────┘
                                       ↓
              ┌────────────────────────┼────────────────────────┐
              ↓                        ↓                        ↓
   keyword_search()          vector_search()            count_by_type()
   （06 篇）                  （07 篇）                  （ingest 汇总）
              └────────────┬───────────┘
                           ↓
                    RRF / Hybrid（08 篇）
                           ↓
                    Evaluation（09 篇）
```

**两个衔接点**：

- **上游**：`storage.replace_repository()` 是"Chunk 生命周期"的终点。它接收 `Sequence[Chunk]` + `Sequence[list[float]]`，之后 Chunk 对象就被丢弃，只剩数据库里的行。
- **下游**：`RESULT_COLUMNS` 决定了检索结果能看到哪些列。这个常量与本篇的 DDL 有直接对应关系（见 7.6 节）。

---

## 3. 必须先知道的最少概念

### 3.1 三层结构：扩展 / 表 / 索引

```text
CREATE EXTENSION vector      ③ 提供 VECTOR(1024) 类型 + <=> 运算符
CREATE EXTENSION pg_trgm     ③ 提供 similarity() 函数 + GIN 模糊索引用法

CREATE TABLE knowledge_chunk ② 22 列，一行 = 一个 Chunk
CREATE INDEX ...             ① 加速 WHERE / ORDER BY
```

三层的分工要分清：**扩展提供"能力"，表提供"结构"，索引提供"速度"。** 没有索引，查询照样出结果，只是慢——这一点在第 11 节讨论 ANN 缺失时会用到。

### 3.2 `vector(1024)` 与 Python `list` 之间的桥接

PostgreSQL 本身不认识 Python 的 `list`。这中间有一层驱动级的适配，由 `pgvector` 提供的 `register_vector()` 完成：

```python
def _connect(self, *, vectors: bool = True) -> psycopg.Connection[Any]:
    connection = psycopg.connect(self.database_url, row_factory=dict_row)
    if vectors:
        register_vector(connection)
    return connection
```

注册之后：

```text
Python list[float]  →  写入  →  PostgreSQL vector 类型
PostgreSQL vector   →  读出  →  Python list[float]（或 numpy array）
```

**如果不注册会怎样**：psycopg3 会把 Python list 当成 PostgreSQL 数组（`float8[]` / `numeric[]`）去适配，而目标列是 `vector` 类型，两者不匹配 → 插入失败。所以除 `initialize()` 之外的**每一个**连接都走 `vectors=True`（默认值）。

**一个必须解释的例外**：`initialize()` 显式用 `vectors=False`。最合理的解释是——它要执行的正是 `CREATE EXTENSION vector`，在 `vector` 类型还不存在的时候去注册它，注册这一步本身就会失败。所以顺序必须是"先建扩展，再享受扩展"。【推断】：这一条由代码结构与 DDL 内容共同推出，本环境无法连接数据库执行验证。

### 3.3 事务在 psycopg3 里的形态

```python
with self._connect() as connection:
    with connection.transaction():
        ...
```

两层 `with` 的语义完全不同，这是本篇最容易看错的地方：

| 层级 | 进入时 | 正常退出时 | 异常时 |
|---|---|---|---|
| `with connection:` | 建立连接 | 提交挂起的事务、关闭连接 | 回滚、关闭连接 |
| `with connection.transaction():` | `BEGIN` | `COMMIT` | `ROLLBACK` |

外层 `with` 保证连接被关闭（不会泄漏连接），内层负责原子性。**关键在于：`DELETE` 与 `INSERT` 在同一个内层事务里**，所以它们要么一起生效，要么一起失效。第 7.5 节会展开。

### 3.4 `executemany` 与参数化查询

```python
cursor.executemany(sql, rows)
```

`rows` 是一个 2901 个元组的列表，每个元组 20 个值；`sql` 里有 20 个 `%s` 占位符。`executemany` 会把同一份 SQL 用 2901 组参数各执行一次。

**注意它不是拼接字符串**，而是参数化执行——这一点对安全与正确性都很重要：

- 安全：即使某个 `content` 里含有 `'; DROP TABLE ...`，也只会被当作一个字符串值。
- 正确性：`content` 里的引号、反斜杠、换行都不需要转义，由驱动处理。

**一个容易忽略的细节**：`executemany` 是"同一语句执行 N 次"，不是 `COPY`。以 2901 行的规模，这个选择完全可接受；如果将来是百万行级，`COPY` 会明显更快。

---

## 4. 关键源码入口

| 位置 | 内容 |
|---|---|
| `sql/001_schema.sql:1-2` | 两个扩展：`vector`、`pg_trgm` |
| `sql/001_schema.sql:4-29` | ★ `knowledge_chunk` 表：22 列 + 2 个 CHECK |
| `sql/001_schema.sql:31-33` | 注释 + `ALTER TABLE ... DROP CONSTRAINT IF EXISTS`（一个刻意的"不约束"） |
| `sql/001_schema.sql:35-44` | 5 个索引（4 个 B-tree + 1 个 GIN） |
| `storage.py:16-19` | ★ `RESULT_COLUMNS`（检索结果的列投影） |
| `storage.py:26-30` | `_connect`（向量注册开关） |
| `storage.py:32-38` | ★ `initialize()`（建表建索引，按 `;` 拆语句） |
| `storage.py:40-93` | ★ `replace_repository()`（事务化全量替换） |
| `storage.py:47-48` | 两个序列长度必须相等的检查 |
| `storage.py:49-74` | 构造 2901 个 20 元素元组 |
| `storage.py:75-85` | INSERT 语句（列清单与占位符） |
| `storage.py:86-93` | ★ 事务边界 |
| `storage.py:95-136` | `keyword_search`（06 篇） |
| `storage.py:138-156` | `vector_search`（07 篇） |
| `storage.py:158-170` | `count_by_type`（ingest 汇总用） |
| `src/devcontext/ingestion/pipeline.py:70-83` | 调用 `initialize()` + `replace_repository()` 的地方 |
| `docker-compose.yml` | 镜像 tag、initdb 挂载、健康检查 |

---

## 5. Input / Output

### 5.1 `initialize()`

**Input**：无参数（数据库地址来自 `Settings.database_url`）。
**Output**：库里有表与索引；无返回值。

```text
Input:  sql/001_schema.sql（1728 字符）
Output: 9 条语句被依次执行
        → knowledge_chunk 表存在
        → 5 个索引存在
        → repository TEXT 字段上无唯一约束
```

### 5.2 `replace_repository()`

**Input**（四个参数）：

```python
replace_repository(
    repository: str,                 # "my12306"
    chunks: Sequence[Chunk],         # 长度 2901
    embeddings: Sequence[list[float]],  # 长度 2901，每个 1024 维
    embedding_model: str,            # "text-embedding-v4"
)
```

**Output**：`int` = 写入的行数（2901）。

**副作用**：原先属于 `my12306` 的全部行被删除。

### 5.3 一组数字（真实语料）

| 项 | 值 |
|---|---|
| 写入行数 | 2901 |
| 其中 `source_type='CODE'` | 538 |
| 其中 `source_type='DOCUMENT'` | 2363 |
| 每个 `embedding` 的浮点数个数 | 1024 |
| 单行 `embedding` 的原始字节 | 1024 × 4 = **4096 bytes（4 KiB）** |
| 仅向量部分的总体积 | 2901 × 4096 ≈ **11.3 MiB** |
| 最长 `content` | 6488 字符（`doPurchaseInTransaction`） |
| 最长 `keyword_text` | 6899 字符（同上） |

---

## 6. 主执行流程

`ingest` 命令里与本篇有关的部分（`pipeline.py:70-83`）：

```text
① 全部 Chunk 与向量都已在内存里算好
        ↓
② store = ChunkStore(settings.database_url)
        ↓
③ store.initialize()
        │  · 读 sql/001_schema.sql
        │  · script.split(";") → 9 条非空语句
        │  · 在 vectors=False 的连接上逐条 execute
        │  · with connection 退出时提交 DDL
        ↓
④ store.replace_repository("my12306", chunks, vectors, "text-embedding-v4")
        │
        ├─ ⑤ 校验 len(chunks) == len(embeddings)
        │       不等 → ValueError("Chunk and embedding counts differ")
        │
        ├─ ⑥ for (chunk, embedding) in zip(chunks, embeddings, strict=True):
        │        拼一个 20 元素元组 → rows.append(...)
        │        （其中 chunk.keyword_text() 在这里被调用一次）
        │
        ├─ ⑦ with self._connect() as connection:      ← 打开连接 + 注册向量
        │      └─ with connection.transaction():      ← BEGIN
        │            DELETE FROM knowledge_chunk WHERE repository = 'my12306'
        │            cursor.executemany(INSERT ..., rows)
        │          ← COMMIT（正常退出时）
        │
        └─ ⑧ return len(rows)   → 2901
        ↓
⑨ summary = IngestionSummary(..., stored_by_type=store.count_by_type("my12306"))
   → {"CODE": 538, "DOCUMENT": 2363}
```

**关键顺序：③ 先建表，④ 再写数据。** 而且**先算完 Embedding（网络操作，慢、可能失败），才打开事务（数据库操作，快）**。这个顺序是第 9 节要讨论的核心设计之一。

---

## 7. 关键源码逐段解释

### 7.1 DDL 逐列 + 一条真实 METHOD 行

#### 真实的一行（Part 5「第一部分」）

下面是 `doPurchaseInTransaction` 这一行在 `knowledge_chunk` 里的**全部 22 列**。数据来源：`artifacts/java-chunks.jsonl` 里的真实记录 + `storage.py:52-73` 构造的元组 + DDL 的默认值规则。

| # | 列 | 类型 | 真实值 |
|---|---|---|---|
| 1 | `id` | `BIGINT GENERATED ALWAYS AS IDENTITY` | 由数据库分配（**本环境无法查询，具体数值未知**；同批数据的另一个 Chunk 实测为 5990） |
| 2 | `repository` | `TEXT NOT NULL` | `my12306` |
| 3 | `source_type` | `TEXT NOT NULL CHECK` | `CODE` |
| 4 | `chunk_type` | `TEXT NOT NULL CHECK` | `METHOD` |
| 5 | `file_path` | `TEXT NOT NULL` | `services/ticket-services/src/main/java/edu/swu/fcj/my12306/biz/ticketservice/service/impl/PurchaseTicketTxService.java` |
| 6 | `module` | `TEXT` | `ticket-services` |
| 7 | `package_name` | `TEXT` | `edu.swu.fcj.my12306.biz.ticketservice.service.impl` |
| 8 | `class_name` | `TEXT` | `PurchaseTicketTxService` |
| 9 | `symbol_name` | `TEXT` | `doPurchaseInTransaction` |
| 10 | `signature` | `TEXT` | `public PurchaseReservationResult doPurchaseInTransaction(PurchaseTicketReqDTO requestParam, String userId, String username, String orderSn, Map<String, PassengerActualRespDTO> passengersById)` |
| 11 | `annotations` | `TEXT[] NOT NULL DEFAULT '{}'` | `{"@Transactional(rollbackFor = Exception.class)"}` |
| 12 | `javadoc` | `TEXT` | `* 在锁内原子完成选座、条件占座和车票写入；必须由外层 Bean 调用以经过 Spring 事务代理。` |
| 13 | `title` | `TEXT` | `NULL`（代码 Chunk 没有标题） |
| 14 | `heading_path` | `TEXT[] NOT NULL DEFAULT '{}'` | `{}`（空数组，不是 NULL） |
| 15 | `content` | `TEXT NOT NULL` | 第 69–172 行的原始源码切片，**6488 字符** |
| 16 | `keyword_text` | `TEXT NOT NULL` | 8 个槽位用 `\n` 拼成的字符串，**6899 字符**（第 01 篇 8.1 节） |
| 17 | `start_line` | `INTEGER` | `69` |
| 18 | `end_line` | `INTEGER` | `172` |
| 19 | `content_hash` | `CHAR(64) NOT NULL` | `519d2ebf087cd7d41b4b1a110760e9079e8faf1bf5616a4c08664a7a9138039c` |
| 20 | `embedding_model` | `TEXT NOT NULL` | `text-embedding-v4` |
| 21 | `embedding` | `VECTOR(1024) NOT NULL` | 1024 个 float32 组成的向量，约 4 KiB |
| 22 | `created_at` | `TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP` | 写入时刻 |

（列数是 22，其中 `id` 与 `created_at` 由数据库生成；`storage.py` 的 INSERT 只显式提供**中间 20 列**。）

#### 22 列分成四组，看它们的"角色"
这一分组是理解这张表的关键，也是第 01 篇那张"字段角色表"的数据库侧对应：

| 组 | 列 | 谁消费 |
|---|---|---|
| **A. 身份与定位** | `id`, `repository`, `source_type`, `chunk_type`, `file_path`, `start_line`, `end_line` | `WHERE` 过滤、结果投影、RRF 去重键、Citation |
| **B. 语义元数据** | `module`, `package_name`, `class_name`, `symbol_name`, `signature`, `annotations`, `javadoc`, `title`, `heading_path` | **关键词检索的加权臂**（`symbol_name` +12 / `class_name` +10 / `signature` +6）、**评测判分**（`symbol_name`、`file_path`、`title`）、结果展示 |
| **C. 三种内容表示** | `content`, `keyword_text`, `embedding` | `content` 给人看；`keyword_text` 给 `strpos`/`similarity`；`embedding` 给 `<=>` |
| **D. 一致性与可追溯** | `content_hash`, `embedding_model`, `created_at` | 完整性核对、判断"这批向量能不能互相比较"、审计 |

**一个值得注意的事实**：B 组里有 4 列（`module` / `package_name` / `annotations` / `heading_path`）在关键词检索里**完全没有被用到**——
- `module` / `package_name`：不在 `keyword_text`，也不在任何评分臂
- `annotations`：不在 `keyword_text` 槽位，只通过 `content` 间接可搜
- `heading_path`：在 `keyword_text` 里，但不在评分臂里（只影响 trigram 相似度）

这不是"设计好的分层"，更像是"先存下来，将来可能用得上"。第 11.7 节把它列为死字段。

#### 约束（DDL 里的 2 个 CHECK + 6 个 NOT NULL）

```sql
source_type TEXT NOT NULL CHECK (source_type IN ('CODE', 'DOCUMENT'))
chunk_type  TEXT NOT NULL CHECK (
    chunk_type IN ('CLASS', 'INTERFACE', 'METHOD', 'CONSTRUCTOR', 'DOCUMENT_SECTION')
)
```

两个 CHECK 的作用不是"文档"，而是**在写入时就拦下枚举值的错误**。真实价值：如果哪天 Java 侧把 `chunk_type` 写成了 `"FUNCTION"`（而不是 `"METHOD"`），插入会直接违反约束、整个事务回滚——你立刻知道出错了。如果没有这个 CHECK，那个错值会安静地进库，然后在检索时表现成"某类 Chunk 就是搜不到"。

DDL 里 22 列中有 13 列是 `NOT NULL`（`id`、`repository`、`source_type`、`chunk_type`、`file_path`、`annotations`、`heading_path`、`content`、`keyword_text`、`content_hash`、`embedding_model`、`embedding`、`created_at`），只有 9 列可空（`module`、`package_name`、`class_name`、`symbol_name`、`signature`、`javadoc`、`title`、`start_line`、`end_line`）。

这 13 列里，`embedding` 与 `keyword_text` 的 NOT NULL 最有意义：它们意味着**不可能存在"没有向量的行"**。整个索引要么是完整的，要么不存在。再看可空的那 9 列——**它们全部是元数据与定位字段，没有一个是检索必需的**。这个分布本身就是设计意图的体现：检索必需的列被约束为必填，用于辅助的列允许为空。

#### 两个数组列的默认值

```sql
annotations  TEXT[] NOT NULL DEFAULT '{}'
heading_path TEXT[] NOT NULL DEFAULT '{}'
```

注意默认值是 `'{}'`（空数组）而不是 `NULL`。这个选择带来一个实际差别：

```sql
-- 用 NOT NULL DEFAULT '{}' 时，判空只有一种写法：
WHERE heading_path = '{}'

-- 如果允许 NULL，就必须写两种：
WHERE heading_path IS NULL OR heading_path = '{}'
```

当前 Python 侧总是显式传入列表（`[]` 或 `["A","B"]`），所以 DEFAULT 实际不会被触发。但 DDL 层的这个选择让"没标题路径"在数据里只有一种表示（空数组），减少了查询时的分支。

### 7.2 统一表：为什么 CODE 与 DOCUMENT 同一张表（Part 5「第二部分」）

#### 结论先说

不是为了省事，而是因为 **RRF 需要一个"两类语料共用的连接键"**。

#### 如果分成两张表会怎样

假设 `code_chunk` 与 `doc_chunk` 两张表。RRF 要做的是：把关键词榜和向量榜里的同一条结果合并、去重、累加分数（`08` 篇）。现在的实现是这样（`hybrid.py`）：

```python
for ranking in rankings:
    for rank, result in enumerate(ranking, start=1):
        scores[result.id] = scores.get(result.id, 0.0) + 1.0 / (k + rank)
        results.setdefault(result.id, result)
```

**`result.id` 是全部逻辑的支点。** 分表的直接后果：

```text
code_chunk 里 id=1 是 PurchaseTicketTxService
doc_chunk  里 id=1 是 D2-设计分析-余票令牌桶与Lua.md 的某个章节

RRF 看到两个 id=1 → 认为它们是“同一条结果” → 分数相加
        ↓
一条代码 Chunk 和一段不相干的文档被错误合并，分数虚高，挤掉正确答案
        ↓
而且这种错误不会报错，只会让排序变差
```

要修这个问题，分表方案必须在融合前引入"复合键"（`source_type + id`）或者先做一次跨表对齐。**单表让这个问题根本不存在**：`id` 是主键，全局唯一，直接就是连接键。

#### 第二个收益：一次查询就能跨类型排序

MIXED 类问题的 Ground Truth 同时要求一个代码命中与一个文档命中（`benchmark/cases.jsonl` 里 MIXED-001~004 都是这样）。单表之后，`keyword_search` 的一条 SQL 就能把两类结果混在一起排序：

```sql
FROM knowledge_chunk
WHERE repository = %s AND (...)      -- 没有按 source_type 分表，而是同一个候选池
ORDER BY score DESC, id ASC
LIMIT %s
```

真实效果见 `08` 篇：MIXED-001 的关键词 Top-5 全是代码（因为符号命中拿到 18 分级别的分数），而向量 Top-5 靠前的是文档——两个候选池合起来之后，RRF 才能把 Top-5 补成"代码 + 文档都有"，把 Recall@5 从 0.50 提到 1.00。

#### 第三个收益：`id` 同时充当稳定的并列排序键

两处 SQL 都用了 `id` 做第二排序键：

```sql
-- keyword_search
ORDER BY score DESC, id ASC
-- vector_search
ORDER BY embedding <=> %s::vector, id ASC
```

这不是可有可无的。余弦相似度用 float 计算，出现完全相等的概率是存在的（尤其是短文本、或者内容高度相似的 Chunk）。如果没有第二排序键，PostgreSQL 对并列行的返回顺序**没有保证**，两次运行的 Top-K 顺序可能不同 → **评测指标会漂移**。有了 `id ASC`，同样输入必然得到同样输出。

#### 代价：一半列对另一半语料为空

| 列 | CODE | DOCUMENT |
|---|---|---|
| `module` / `package_name` / `class_name` / `symbol_name` / `signature` / `annotations` / `javadoc` | 有值 | `NULL`（`annotations` 是 `{}`） |
| `title` / `heading_path` | `NULL` / `{}` | 有值 |
| `content` / `keyword_text` / `embedding` / `file_path` / `start_line` / `end_line` | 有值 | 有值 |

这张表说明了一件事：**统一表不是"所有字段对两类语料都有意义"，而是"共享的那部分字段足够支撑检索与融合"**。共享部分恰好是 A/C 两组——也就是检索真正需要的东西。

### 7.3 `embedding VECTOR(1024)` 是什么意思（Part 5「第三部分」）

#### 一句话

`VECTOR(1024)` = **一个定长 1024 的浮点数组列**，1024 是这个列的"宽度"，写入时必须正好是 1024 个数。

#### 三个必须知道的含义

**含义一：宽度是表结构的一部分，不能变。**

```sql
embedding VECTOR(1024) NOT NULL
```

这不是"字段类型是 vector，长度之后再定"，而是"**这个列就是 1024 维**"。插一个 768 维的向量进去会直接被拒绝。

后果非常实际：**换 Embedding 模型（或换维度）= 改列类型 + 重算全部向量**。

```sql
ALTER TABLE knowledge_chunk ALTER COLUMN embedding TYPE VECTOR(768);  -- 报错或需要重建
```

因为这批数据已经写进去了，所以这并不是"改一行 DDL"就能完成的事。这就是为什么规划阶段把"维度 N"标成 `BLOCKING`——它必须在建表之前定死【文档依据】（`docs/development/02-tech-stack-todo.md` 的 Embedding 决策）。当前实现选的是 1024。

**含义二：它占的空间是可算的。**

```text
1024 × 4 bytes（float32）= 4096 bytes = 4 KiB / 行
2901 行 × 4 KiB ≈ 11.3 MiB（仅向量）
```

顺带一个工程副作用（不深入 pgvector 内部）：PostgreSQL 的行如果要存的东西超过约 2 KiB，就会走 TOAST 机制（压缩 + 可能移出主表）。**单是 `embedding` 这一列就 4 KiB，已经超过阈值**，所以这张表的每一行都会被 TOAST 判定。这不是问题，但可以解释为什么这张表的物理体积比你按 `content` 长度估算的要大。

**含义三：提供 `<=>` 运算符。**

```sql
1 - (embedding <=> %s::vector)     -- <=> 是余弦距离，1 - 距离 = 余弦相似度
```

`<=>` 由 `vector` 扩展提供。这也是为什么必须先 `CREATE EXTENSION vector`——没有它，`VECTOR(1024)` 这个类型名都不存在，DDL 会直接语法报错。

#### 一个约束的溢出效应

`embedding VECTOR(1024) NOT NULL` 里的 NOT NULL 和 7.1 节说的是同一件事：**这个库不允许存在没有向量的行**。所以 `ingest` 的顺序必须是"先把所有向量算完，再写库"——如果反过来（先写 Chunk 再补向量），就要允许 embedding 可空，于是任何一次中途失败都会留下"有内容但搜不到"的行。

### 7.4 当前真实索引清单（Part 5「第四部分」）

这一节必须精确。**已有的、没有的，分开列。**

#### DDL 里真实存在的 5 个索引（`sql/001_schema.sql:35-44`）

```sql
CREATE INDEX ... knowledge_chunk_repository_idx    ON knowledge_chunk (repository);
CREATE INDEX ... knowledge_chunk_source_type_idx   ON knowledge_chunk (source_type);
CREATE INDEX ... knowledge_chunk_chunk_type_idx    ON knowledge_chunk (chunk_type);
CREATE INDEX ... knowledge_chunk_file_path_idx     ON knowledge_chunk (file_path);
CREATE INDEX ... knowledge_chunk_keyword_trgm_idx  ON knowledge_chunk USING GIN (keyword_text gin_trgm_ops);
```

| 索引 | 类型 | 服务于什么 |
|---|---|---|
| `repository_idx` | B-tree | **每一条检索 SQL 的 `WHERE repository = %s`** |
| `source_type_idx` | B-tree | 跨类型过滤、`count_by_type` 的 GROUP BY |
| `chunk_type_idx` | B-tree | 按类型统计/过滤（当前检索未使用） |
| `file_path_idx` | B-tree | 按文件定位（当前检索未使用） |
| `keyword_trgm_idx` | **GIN (gin_trgm_ops)** | `similarity(keyword_text, ...)` 与子串匹配——**这是关键词检索能不用全表扫描的关键** |

有一个细节值得注意：**`repository` 是最热的过滤列，但索引是单列的，不是 `(repository, source_type)` 复合索引**。当前规模下（一个仓库）单列索引足够；如果将来多仓库并存，复合索引会更合适。

#### 明确**不存在**的索引与技术

| 技术 | 状态 | 依据 |
|---|---|---|
| **pg_trgm GIN** | ✅ **存在** | `sql/001_schema.sql:43-44` |
| `tsvector` 列 | ❌ **不存在** | DDL 里没有该列 |
| `tsvector` 的 GIN 索引 | ❌ **不存在** | 5 个索引里没有 |
| `ts_rank` / `setweight` / `to_tsquery` | ❌ **不存在** | `src/` 与 `sql/` 全文搜索无命中（只命中规划与调研文档） |
| **HNSW**（向量近似索引） | ❌ **不存在** | DDL 里没有 `USING hnsw` |
| **IVFFlat**（向量近似索引） | ❌ **不存在** | DDL 里没有 `USING ivfflat` |

#### 这个事实的两个直接后果

**后果一：关键词检索当前不是 PostgreSQL FTS。**

规划文档（`00-p0-scope.md` §2.2 第 10 项、ADR-006）把 FTS（`tsvector` + `setweight` A/B/C + `ts_rank`）定为 P0 要求。**当前实现没有采用它**，关键词能力由三件事承担：

```text
Identifier Extraction（Python 侧正则）
+ strpos()            （精确/子串匹配）
+ pg_trgm similarity  （模糊相似度）
+ CASE WHEN 手写权重   （12 / 10 / 6 / 2）
```

这解释了为什么字段加权是四条 `CASE WHEN` 而不是 `setweight` 的 A/B/C 四档。**请不要把它称为"tsvector FTS"**——这个称呼会让所有关于"分词器 / IDF / BM25"的推理都走错方向。

**后果二：向量检索是精确全表扫描。**

没有 ANN 索引意味着 `ORDER BY embedding <=> query` 必须对全部 2901 行各算一次余弦距离：

```text
2901 行 × 1024 维 ≈ 297 万次乘法累加
读入的向量数据 ≈ 11.3 MiB
```

既然没有 HNSW / IVFFlat，也就意味着**当前不存在"近似检索误差"**：向量召回率的上界完全由 embedding 质量决定，不需要调 `ef_search`、`nprobes` 这类参数。这在当前阶段是优点——指标干净、可归因。代价是延迟随数据量线性增长（第 11.3 节给量化估算）。

### 7.5 全量替换事务（Part 5「第五部分」）

#### 代码（`storage.py:86-93`）

```python
with self._connect() as connection:
    with connection.transaction():
        connection.execute(
            "DELETE FROM knowledge_chunk WHERE repository = %s", (repository,)
        )
        with connection.cursor() as cursor:
            cursor.executemany(sql, rows)
return len(rows)
```

#### 时序图

```text
时间线 ──────────────────────────────────────────────────────►

[ 上游：embedding 全部算完 ]        ← 网络阶段，可能耗时数分钟、可能失败
        │
        │  self._connect()                  建立连接 + register_vector
        ↓
   ┌── BEGIN ────────────────────────────────────────────┐
   │  DELETE FROM knowledge_chunk WHERE repository=...    │  ← 旧数据消失（但只是“在本事务内”）
   │  executemany(INSERT ... , rows)   × 2901             │  ← 新数据出现
   └── COMMIT ───────────────────────────────────────────┘
        │
        ↓
   查询立刻看到“新索引”
```

#### 核心问题：如果 INSERT 中途失败，为什么旧数据不会消失？

因为 `DELETE` 从来没有真正提交过。

```text
DELETE 执行后，行的可见性变化只在“当前事务”内生效：
   · 事务自己看 → 旧行不可见（等 INSERT 填上）
   · 其他连接看 → 旧行仍然可见（PostgreSQL 的 MVCC）

若 INSERT 第 1500 行抛异常：
   · psycopg3 的 connection.transaction() 在异常退出时发 ROLLBACK
   · DELETE 被撤销 → 2901 条旧行原样回来
   · 那 1500 条新行从未存在过
   → 库里仍然是“上一版的完整索引”
```

这就是"事务化全量重建"的全部价值：**任何时刻，查询都能拿到一份完整的索引，不存在"空了一半"的中间态。**

#### 失败点逐一推演

| 失败发生在 | 事务状态 | 库里的内容 |
|---|---|---|
| Java 解析阶段 | 事务未开始 | 上一版索引（完整） |
| Markdown 解析阶段 | 事务未开始 | 上一版索引（完整） |
| Embedding API 阶段（网络中断） | 事务未开始 | 上一版索引（完整） |
| `_connect()` 阶段 | 事务未开始 | 上一版索引（完整） |
| `DELETE` 之后、`INSERT` 中 | 已 `BEGIN`，抛异常 → `ROLLBACK` | 上一版索引（完整） |
| `INSERT` 全部完成、`COMMIT` 中 | 提交成功或整体失败 | 要么全新索引，要么上一版索引 |

**这张表里没有"索引空了一半"这一行**——这正是设计目标。

#### 为什么不是 UPSERT，也不是 TRUNCATE

| 方案 | 为什么没采用 |
|---|---|
| `INSERT ... ON CONFLICT DO UPDATE` | 需要唯一约束来判定"冲突"。而这张表**故意没有**唯一约束（见 7.6 节）。而且 UPSERT 无法处理"这一版不再存在的 Chunk"（旧的残留） |
| `TRUNCATE TABLE` | 会清掉**所有 repository** 的数据，不再是"按 repository 替换"；而且 `TRUNCATE` 会重置 identity 序列（`RESTART IDENTITY` 时），破坏"id 只增不复用"的语义 |
| 先 `DELETE` 再 `INSERT`（不在同一事务） | 这正是最危险的做法：两次操作之间索引是空的，且如果 INSERT 失败就永久丢失 |

#### 一个使用上的副作用

`DELETE ... WHERE repository = %s` 是按 repository 删除，所以**同一个库里可以并存多个 repository**，替换其中一个不会影响另一个。当前只用 `my12306` 一个，但这个设计使"多仓库"在存储层已经可用（虽然配置层只支持一个 `repository_name`）。

### 7.6 三个容易忽略的细节

#### 细节一：为什么故意不加唯一约束（`sql/001_schema.sql:31-33`）

```sql
-- Idempotency comes from the transaction-scoped repository replacement. A source line may
-- legitimately produce multiple chunks (for example, a single oversized Markdown line).
ALTER TABLE knowledge_chunk DROP CONSTRAINT IF EXISTS knowledge_chunk_location_unique;
```

如果对 `(file_path, start_line, end_line)` 加唯一约束，会发生什么？考虑第 03 篇讲的那种极端情况：**一个超过 6000 字符的单行 Markdown 章节会被硬切成多片**，而每一片都指向**同一行**。于是：

```text
(file_path="x.md", start_line=42, end_line=42)   ← 第 1 片
(file_path="x.md", start_line=42, end_line=42)   ← 第 2 片（合法的！）
```

唯一约束会让这种**合法切分**插入失败，进而让整个 ingestion 回滚。

所以幂等性改由"事务内按 repository 整批替换"保证——**不依赖行的唯一性，而依赖替换的原子性**。这条 `ALTER TABLE ... DROP CONSTRAINT IF EXISTS` 是防御式语句：如果历史版本的 schema 建过这个约束，它会被去掉。

（`IF EXISTS` 是必需的——如果约束不存在，不带 `IF EXISTS` 的 `DROP CONSTRAINT` 会直接报错，而这个脚本要能被重复执行。）

#### 细节二：`initialize()` 按 `;` 拆语句（实测 9 条）

```python
script = script_path.read_text(encoding="utf-8")
with self._connect(vectors=False) as connection:
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)
```

我实测跑了一遍这个拆分逻辑，`sql/001_schema.sql`（1728 字符）被拆成 **9 条**非空语句：

| # | 字符数 | 语句 |
|---|---:|---|
| 1 | 37 | `CREATE EXTENSION IF NOT EXISTS vector` |
| 2 | 39 | `CREATE EXTENSION IF NOT EXISTS pg_trgm` |
| 3 | 877 | `CREATE TABLE IF NOT EXISTS knowledge_chunk (...)` |
| 4 | 267 | （含两条前置注释）`ALTER TABLE ... DROP CONSTRAINT IF EXISTS ...` |
| 5 | 95 | `CREATE INDEX ... repository_idx` |
| 6 | 96 | `CREATE INDEX ... source_type_idx` |
| 7 | 94 | `CREATE INDEX ... chunk_type_idx` |
| 8 | 92 | `CREATE INDEX ... file_path_idx` |
| 9 | 121 | `CREATE INDEX ... keyword_trgm_idx` |

第 4 条证明了一个细节：**注释会被附着在它后面那条语句上**（因为 `;` 是唯一的分隔符）。PostgreSQL 能正确处理"以注释开头的语句"，所以当前没问题。

**但这个拆分方式很脆弱**：它假设 SQL 文本里**没有别的地方出现分号**。以下任何一条都会把它拆坏：

```sql
-- ① 字符串字面量里含分号
INSERT INTO t VALUES ('a;b');
-- ② 函数体里含分号
CREATE FUNCTION f() RETURNS void AS $$ BEGIN ...; ...; END; $$ LANGUAGE plpgsql;
-- ③ Dollar-quoting 块
DO $$ ... ; ... $$;
```

当前脚本恰好满足"只有语句结尾有分号"，所以能用。第 11.2 节把它列为限制。

#### 细节三：`RESULT_COLUMNS` 与 DDL 的对应关系

```python
RESULT_COLUMNS = """
    id, source_type, chunk_type, file_path, content, start_line, end_line,
    class_name, symbol_name, signature, title
"""
```

这 11 列就是 `SearchResult` 的 11 个字段（外加 SQL 里算出来的 `score`）。它与 DDL 的差集很说明问题：

```text
DDL 有但结果里没有：
  repository, module, package_name, annotations, javadoc,
  heading_path, keyword_text, content_hash, embedding_model, embedding, created_at
```

**其中 `heading_path` 与 `annotations` 的缺失最值得注意**：

- `heading_path` 缺失 → 文档类的 Citation 无法按"标题路径"呈现（第 01 篇 §3.3、第 11.3 节）
- `annotations` 缺失 → 下游拿不到"这个 Chunk 上有什么注解"这个结构化信息

`embedding` / `keyword_text` 不出现在结果里是**正确**的（它们是检索用的中间表示，返回给应用层没有意义，还会让结果体积变大）。但 `heading_path` 的缺失更像是一个"还没补上"的缺口。

---

## 8. 用一个真实数据走完整流程

把 `doPurchaseInTransaction` 从 Python 对象到数据库行的全过程串一遍。

```text
① 上游（第 02 篇）已产出 Chunk 对象
   Chunk(
     repository    = "my12306",
     source_type   = "CODE",
     chunk_type    = "METHOD",
     file_path     = "services/ticket-services/src/main/java/edu/swu/fcj/my12306/biz/ticketservice/service/impl/PurchaseTicketTxService.java",
     module        = "ticket-services",
     package_name  = "edu.swu.fcj.my12306.biz.ticketservice.service.impl",
     class_name    = "PurchaseTicketTxService",
     symbol_name   = "doPurchaseInTransaction",
     signature     = "public PurchaseReservationResult doPurchaseInTransaction(...)",
     annotations   = ["@Transactional(rollbackFor = Exception.class)"],
     javadoc       = "* 在锁内原子完成选座、条件占座和车票写入；...",
     title         = None,
     heading_path  = [],
     content       = "@Transactional(...)\n    public ...（6488 字符）",
     start_line    = 69,
     end_line      = 172,
     content_hash  = "519d2ebf...8039c",
   )

② 上游（第 04 篇）已产出对应的向量
   embedding = [0.0134, -0.0287, ... ]   （1024 个 float）

③ storage.py:50  zip(chunks, embeddings, strict=True)
   ↑ strict=True 意味着两边长度不等会立刻抛错，而不是静默截断

④ storage.py:52-73  拼元组（20 个值，顺序与 INSERT 列清单严格对应）
   (
     "my12306",                            → repository
     "CODE",                               → source_type
     "METHOD",                             → chunk_type
     "services/.../PurchaseTicketTxService.java",  → file_path
     "ticket-services",                    → module
     "edu.swu.fcj...impl",                 → package_name
     "PurchaseTicketTxService",            → class_name
     "doPurchaseInTransaction",            → symbol_name
     "public PurchaseReservationResult ...",  → signature
     ["@Transactional(rollbackFor = Exception.class)"],  → annotations
     "* 在锁内原子完成选座...",              → javadoc
     None,                                 → title
     [],                                   → heading_path
     "@Transactional(...)\n    public ...（6488 字符）",  → content
     "doPurchaseInTransaction\nPurchaseTicketTxService\n...（6899 字符）",  → keyword_text（★ 在这里调用 keyword_text()）
     69,                                   → start_line
     172,                                  → end_line
     "519d2ebf...8039c",                   → content_hash
     "text-embedding-v4",                  → embedding_model
     [0.0134, -0.0287, ...],               → embedding
   )

⑤ storage.py:92  executemany(INSERT ...)   这一行走进 PostgreSQL

⑥ 数据库内做的三件事
   · id         ← GENERATED ALWAYS AS IDENTITY，分配一个新值
   · created_at ← DEFAULT CURRENT_TIMESTAMP
   · 两个 CHECK 校验：'CODE' ∈ {'CODE','DOCUMENT'} ✅  'METHOD' ∈ {5 种} ✅
   · 类型适配：20 个值的类型与 20 列的类型逐一对齐
       · list[str] → TEXT[]（annotations / heading_path）
       · None      → NULL（title）
       · list[float] → VECTOR(1024)（← 靠 register_vector 注册的适配器）

⑦ COMMIT 之后，这一行对查询可见
```

**读回来的样子**（`06` / `07` 篇的查询走的就是这条路径）：

```python
SearchResult(
    id=...,                            # 数据库分配
    source_type="CODE",
    chunk_type="METHOD",
    file_path="services/.../PurchaseTicketTxService.java",
    content="@Transactional(...)...",  # 完整 6488 字符（但 to_dict() 会截成 300 字预览）
    start_line=69,
    end_line=172,
    class_name="PurchaseTicketTxService",
    symbol_name="doPurchaseInTransaction",
    signature="public PurchaseReservationResult doPurchaseInTransaction(...)",
    title=None,
    score=28.1010,                     # 由 SQL 的 CASE WHEN 算出来
)
```

注意读回来的 `SearchResult` **没有** `heading_path`、没有 `annotations`、没有 `javadoc`——因为 `RESULT_COLUMNS` 里没有它们。

---

## 9. 关键设计为什么这样做

### 9.1 为什么"先算完 Embedding，再开事务"

这是本篇最重要的一个顺序决定。看两组对比：

```text
【当前顺序】先 embedding（慢，网络）→ 再开事务（快，本地）
   网络阶段失败  → 事务从未开始 → 旧索引完好，只是白花了时间
   事务阶段失败  → ROLLBACK      → 旧索引完好

【反过来】先 DELETE + INSERT（占住事务）→ 再慢慢算 embedding
   网络阶段卡住  → 事务长时间持有 DELETE 带来的行锁 → 其他查询看到什么？
   网络阶段失败  → ROLLBACK，但事务已经开了几分钟 → 长事务风险
```

关键点是：**Embedding 是整条链路上唯一"慢且可能失败"的环节**（2901 个 Chunk、约 291 次 API 请求）。把它放在事务外面，就把"长时间的、不可靠的操作"与"短时间的、可靠的操作"分开了。事务里只剩下 2901 次 INSERT——这是一个毫秒到秒级的操作，持锁时间极短。

### 9.2 为什么 `keyword_text` 与 `embedding` 是物化列，而不是生成列

PostgreSQL 支持 `GENERATED ALWAYS AS (...) STORED` 的生成列，看起来更适合"从其他列派生出来"的场景。但这里必须用普通列，有两个不同的原因：

**`embedding` 根本无法用生成列**：它不是从行内其他列算出来的，而是要调用外部 API。数据库不可能自己发起 HTTPS 请求。

**`keyword_text` 理论上可以用生成列**（它就是 8 个列的字符串拼接），但没有这样做，原因是：

```text
生成列的表达式必须是纯 SQL（immutable）
而 keyword_text() 的规则（哪些字段、什么顺序、怎么过滤空值）
  定义在 Python 里

如果改成 SQL 生成列 → 同一套规则要在两个地方各写一遍
   → 改了一处忘另一处 → 两边的 keyword_text 不一致 → 检索结果无法解释
```

用 Python 物化列，规则的**唯一来源**就留在 `models.py:40-51`。这也和第 01 篇说的一致：`keyword_text` 是"针对关键词检索做的特征工程"，属于应用层逻辑，不该下沉到 DDL。

**代价必须记住**：物化列意味着**改了 `keyword_text()` 或 `embedding_text()` 的逻辑，必须重新 ingest 才生效**。改代码不会自动更新已有数据。这在实验时最容易误判成"改了没效果"。

### 9.3 为什么存 `embedding_model`

`embedding_model TEXT NOT NULL` 这一列看起来只是"记录一下"，但它防的是一个非常具体的事故：

```text
今天用 text-embedding-v4（1024 维）建好索引
下周换成另一个模型（也是 1024 维，但向量空间完全不同）
   → 只重算了新 Chunk 的向量，旧 Chunk 还是旧模型的向量
   → 同一个 embedding 列里混着两个模型的向量
   → 余弦相似度变得没有意义（不同模型的向量空间不可比较）
   → 检索结果看起来“有点对又有点不对”，但没有任何报错
```

有了这一列，至少可以检测出"库里的向量来自哪些模型"：

```sql
SELECT embedding_model, count(*) FROM knowledge_chunk GROUP BY embedding_model;
```

返回多行就说明混了。**但要注意：当前实现并没有真的用这列做校验或阻断**——它只是被写进去。所以它能"被发现"，但不能"被防止"（第 11.5 节）。

### 9.4 为什么 `initialize()` 可以重复执行

DDL 里每一句都带防御：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS knowledge_chunk (...);
ALTER TABLE knowledge_chunk DROP CONSTRAINT IF EXISTS knowledge_chunk_location_unique;
CREATE INDEX IF NOT EXISTS knowledge_chunk_repository_idx ...;
```

所以 `devcontext init-db` 可以随便跑多少次——这是必要的，因为 `ingest` 内部也会调用 `initialize()`（`pipeline.py:71`）。如果 DDL 不带 `IF NOT EXISTS`，第二次 `ingest` 就会因为"表已存在"而失败。

### 9.5 为什么评测结果不落在数据库里

规划文档设计了四张表【文档依据】（`docs/项目规划文档/devContex-项目背景和约束.md` §13）：

```text
repository          知识库元信息
knowledge_chunk     统一 Chunk 存储
evaluation_case     Benchmark 题目
evaluation_result   实验结果
```

**当前实现只有 `knowledge_chunk` 一张表**，另外三张不存在：

| 规划的表 | 当前实际 | 位置 |
|---|---|---|
| `repository` | ❌ 不存在。用 `knowledge_chunk.repository` 一个 TEXT 列代替（没有外键、没有 `root_path` / `language` / `status`） | — |
| `knowledge_chunk` | ✅ 存在（但字段与规划不同，见 9.6 节） | `sql/001_schema.sql` |
| `evaluation_case` | ❌ 不存在。改用**文件** | `benchmark/cases.jsonl` |
| `evaluation_result` | ❌ 不存在。改用**文件** | `artifacts/evaluation-<时间戳>.json` |

为什么评测结果落文件而不是落表，代码里没有留下决策记录（**当前无法确认**）。但从结果上看是合理的：实验产物需要保留**每一次**运行的历史（四份报告都留着），而文件天然就是"一次运行一个文件"，比在表里靠 `created_at` 区分更直观，也不需要为实验结构设计 schema——因为实验报告的字段还在演进。

### 9.6 规划的表结构 vs 当前的表结构

这是"规划 ≠ 实现"最集中的一处，逐项对照：

| 规划字段 | 当前实现 | 差异说明 |
|---|---|---|
| `id` | `id` | 一致（但实现用 `GENERATED ALWAYS AS IDENTITY` + `PRIMARY KEY`） |
| `repository_id`（指向 `repository` 表） | `repository`（TEXT） | **改为字符串，砍掉了 `repository` 表与外键** |
| `source_type` | 同 | 一致，实现加了 CHECK |
| `chunk_type`（3 种） | 同（**5 种**） | **实现多了 `INTERFACE` 与 `CONSTRUCTOR`** |
| `file_path` / `module` / `package_name` / `class_name` | 同 | 一致 |
| `method_name` | **`symbol_name`** | **改名**。因为要同时容纳方法名、构造器名、类型名（构造器的名字等于类名，用 `method_name` 语义会错） |
| `heading_path` | 同 | 一致 |
| `content` | 同 | 一致 |
| `content_hash` | 同 | 一致（实现用 `CHAR(64)`） |
| `start_line` / `end_line` | 同 | 一致 |
| `embedding` | 同 | 实现固定 `VECTOR(1024)` |
| `created_at` | 同 | 一致 |
| `updated_at` | **❌ 不存在** | 因为当前只有"全量替换"，没有"更新单行"，没有 `updated_at` 的语义 |
| — | **`signature`**（新增） | 规划里没有，但关键词检索的 +6 权重臂依赖它 |
| — | **`annotations`**（新增） | 规划里没有 |
| — | **`javadoc`**（新增） | 规划里没有，但中文语义检索依赖它（第 02 篇 §7.4） |
| — | **`title`**（新增） | 规划里没有，文档类 Chunk 的 identity 依赖它 |
| — | **`keyword_text`**（新增） | 规划里没有，**因为规划假设用 tsvector（可从其他列生成），而实现改用了 pg_trgm + strpos，必须物化** |
| — | **`embedding_model`**（新增） | 规划里没有，用于判断"向量能不能互相比较" |

两处差额最有教学价值：

**第一，`method_name` → `symbol_name` 的改名是必然的。** 只要 CONSTRUCTOR 独立成一类，`method_name` 这个名字就名不副实了。

**第二，`keyword_text` 是"技术路线改变"的副产品。** 规划用 `tsvector`（可以从 `to_tsvector(...)` 表达式生成 + `setweight` 加权），这条路不需要物化列。改用 pg_trgm 之后，`similarity()` 需要一个**完整的文本列**，于是 `keyword_text` 才必须存在。**这说明：换一个检索技术，DDL 就要跟着变**——这也解释了 `00-p0-scope.md` 里登记的那条 Conflict（"`knowledge_chunk` 字段集不足"）是怎么被解决的。

### 9.7 关于 `id` 的一个重要事实：它不是 Chunk 的身份（实测）

这一节的内容是我在读 `artifacts/` 里的四份评测报告时发现的，**有实测证据，不是推理**。

`id` 是 `GENERATED ALWAYS AS IDENTITY`，而 `replace_repository` 做的是 `DELETE` + `INSERT`。**`DELETE` 不会重置 identity 序列**，所以每次全量重建，同一个逻辑 Chunk 都会拿到一个**新的、更大的 id**。

验证方法：评测报告里保存了每条结果的 `id`（`SearchResult.to_dict()` 里有 `id` 字段）。对比四份报告里出现过的 id 集合：

| 报告对 | id 集合交集 | 结论 |
|---|---|---|
| 164133 → 164327 | 91 / 111 与 103 | 交集很大 → 同一批 id，**未重新 ingest**（只是排序变了） |
| 164327 → 164856 | **0 / 103 与 107** | **交集为 0** → 全部 id 都是新的，**中间发生过 re-ingest** |
| 164856 → 165124 | 107 / 107 与 107 | **完全相同** → **未重新 ingest** |

**`164327 → 164856` 那一行（交集 = 0）就是"id 在全量重建后会整体改变"的直接证据。** 如果一个 id 都没有留下，只可能是全部行被删掉又重新插入、并分配了全新的 id。

这个事实有三个实际含义：

1. **不能跨 ingest 比较 id。** 两份评测报告里的 `id` 字段只有在"中间没重新 ingest"时才可以对比。第 12 节的实验 E 会用到这条判据。
2. **它对 RRF 没有影响。** RRF 只需在**同一次查询内**保证 id 唯一且稳定（`08` 篇），不跨运行比较。
3. **它反过来解释了一次指标变化的原因。** `164856 → 165124` 之间 id 集合完全相同 → 数据库没被动过 → 那么 Keyword R@3 从 0.792 涨到 0.917、Vector R@3 从 0.375 涨到 0.542，**只能来自检索代码或 `benchmark/cases.jsonl` 的改动，不可能来自索引内容的变化**。

第 3 点值得多写一句，因为它是诚实解读指标的范例。既然索引没变、向量没变，而 `vector_search` 的 SQL 又极其简单（只有 `1 - (embedding <=> ...)` 与 `ORDER BY`），那么向量指标提升的原因就收窄到两种可能：

```text
可能 A：benchmark 的问题文本被改写 → query embedding 变了 → 排序变了
可能 B：benchmark 的 Ground Truth 被放宽（例如增加 path_contains_any 的等价来源）
```

**这两种"提升"都不是检索能力的提升。** 由于仓库没有 Git 提交历史，**具体是哪一种，当前无法确认**。这件事的正确结论不是"我们改进了向量检索"，而是"**在比较两次指标之前，必须先确认索引与 benchmark 都没变**"。

---

## 10. 常见误解

### 误解 1：`chunk_type` 有 3 种

规划文档（`devContex-项目背景和约束.md` §13）里写的是 3 种（`DOCUMENT_SECTION` / `CLASS` / `METHOD`）。**当前实现的 CHECK 约束里有 5 种**：

```sql
chunk_type IN ('CLASS', 'INTERFACE', 'METHOD', 'CONSTRUCTOR', 'DOCUMENT_SECTION')
```

多了 `INTERFACE` 与 `CONSTRUCTOR`。真实分布：METHOD 316、CLASS 165、INTERFACE 38、CONSTRUCTOR 19、DOCUMENT_SECTION 2363。

### 误解 2：关键词检索用的是 PostgreSQL FTS

**不是。** 见 7.4 节。表里没有 `tsvector` 列，没有 `ts_rank`，没有 `setweight`。关键词能力来自 `strpos` + `pg_trgm similarity` + 手写 `CASE WHEN` 权重。

### 误解 3：向量检索有索引加速

**没有。** 没有 HNSW，没有 IVFFlat。`ORDER BY embedding <=> ...` 是全表精确扫描。所以**当前不存在近似检索误差**——这是当前阶段的一个优点（指标干净），而不是缺陷。

### 误解 4：`created_at` 是这个 Chunk "第一次出现"的时间

**不是。** 因为每次 `ingest` 都是 `DELETE` + `INSERT`，所以 `created_at` 是**最近一次全量重建**的时间，而不是内容首次进入索引的时间。库里的 2901 行时间戳会集中在同一次 ingestion 的几秒内。

**如果想看"内容何时首次出现"，当前实现没有这个信息。**

### 误解 5：`id` 是 Chunk 的稳定标识

**不是。** 见 9.7 节（有实测证据）。不要拿它做跨运行的对比、不要把它写进外部文档、不要把它当成"这个方法的编号"。

### 误解 6：`content_hash` 被用来做增量索引

**没有。** 它被保存了，但当前**不参与任何逻辑**：`initialize()` 不用它、`replace_repository()` 全量替换不看它、检索也不涉及。它是为将来的增量索引预留的字段（规划里明确说 `content_hash` 保存但不做增量）。

### 误解 7：`DELETE ... WHERE repository` 之后必须重建 id 序列

不需要，也不应该。id 持续增长是预期行为——`DELETE` 只删除行，不动序列。只要 id 不回落，就不会出现"同一个 id 先后指向两个不同的 Chunk"的情况（这会让外部引用变危险）。

### 误解 8：表名叫 `knowledge_chunk` 但它是"知识库"

它只是"切片表"。规划里的 `repository` 表（知识库元信息：`root_path` / `language` / `status`）**不存在**。所以 `knowledge_chunk` 既没有一个"仓库实体"指向它，也没有仓库级的元数据可查。`repository` 这一列只是一个字符串标签。

### 误解 9：检索的 Latency 是数据库耗时

不一定。`RetrievalService.search()` 在向量/混合策略下会**先调用远程 Embedding API** 把 query 变成向量，再查数据库。而评测测的是整个 `search()` 的耗时。所以：

```text
keyword 平均 397 ms  → 纯数据库耗时（无网络）
vector  平均 466 ms  → 远程 embedding 调用 + 数据库耗时
hybrid  平均 932 ms  → 远程 embedding 调用 + 数据库 × 2（关键词 + 向量）
```

第 01 篇看到的那张延迟表，不能当"数据库性能"读。这一点在 `09` 篇解读 Latency 指标时会再展开。

### 误解 10：`initialize()` 用了什么 SQL 解析器

没有。它就是把脚本按 `;` 切开、逐条执行（7.6 节细节二）。所以**它不是通用的 SQL 脚本执行器**——脚本里不能出现"语句内部的 `;`"。

---

## 11. 当前实现的限制/缺陷

### 11.1 表结构上没有的问题（先把"不是缺陷"的部分说清）

| 看起来可疑 | 实际为什么不是缺陷 |
|---|---|
| 没有 `(file_path, start_line, end_line)` 唯一约束 | **故意的**，因为超长单行会被合法切成多片（7.6 节） |
| 只有一个 `repository` 字符串，没有外键 | 当前只有单仓库；过滤靠 `WHERE repository` + 索引，也是全表隔离的正确做法 |
| `embedding` 是 `NOT NULL` | 这是保证"索引要么完整要么不存在"的关键约束（7.3 节） |
| 没有 `updated_at` | 因为只有全量替换，没有"更新一行"的语义 |

### 11.2 `initialize()` 的 `split(";")` 很脆弱（已实测 9 条语句）

见 7.6 节细节二。当前脚本恰好安全，但以下任一情况都会把它拆坏：

```sql
INSERT INTO t VALUES ('a;b');                                    -- 字符串里有分号
CREATE FUNCTION ... AS $$ BEGIN ...; ...; END; $$ LANGUAGE plpgsql;  -- 函数体
DO $$ ... ; ... $$;                                              -- 匿名块
```

一旦拆坏，报错会是"语法错误"，而真正的原因在"脚本里有分号"——排查成本不低。修法很简单：用能理解 dollar-quoting 的拆句器，或者干脆每条语句单独一个文件、按文件名顺序执行。

### 11.3 向量检索是精确扫描，规模上有隐性上限

没有 ANN 索引，所以每次向量查询都要：

```text
读入 2901 行 × 4 KiB ≈ 11.3 MiB 的向量数据
计算 2901 次余弦相似度（每次 1024 维乘法累加）
```

当前可接受（评测实测 vector 平均 466ms，其中还包含一次远程 embedding 调用）。但这条曲线是**线性**的：

| 规模 | 向量数据量 | 粗估趋势 |
|---|---|---|
| 2.9 千行（当前） | 11.3 MiB | 可接受 |
| 10 万行 | ~390 MiB | 需要 ANN |
| 100 万行 | ~3.9 GiB | 必须 ANN |

也就是说：**"加 HNSW"这件事的触发条件是可以量化的**——当向量数据量增长到几十倍、或者延迟指标开始不可接受的时候。现在加是过度设计（还会引入近似误差，让指标不再干净）。

### 11.4 每行都会被 TOAST

单是 `embedding` 一列就 4 KiB，超过 PostgreSQL 约 2 KiB 的 TOAST 阈值，所以每一行都会被压缩/外置判定。这不是错误，但：

- 表的物理体积会比按 `content` 长度估算的更大；
- 向量数据（float32 高熵）压缩收益有限。

如果将来要降低这部分开销，可考虑的选项是降低维度（但那要重建全表），或者把向量独立成表（但那会破坏"一行同时有两种索引"的核心设计）。**当前不建议动。**

### 11.5 `embedding_model` 存了但不用

9.3 节讲过它能检测"混模型"，但当前**没有任何代码读取它**：不会在写入时校验、不会在查询时过滤、不会在混入时告警。所以它目前只是一个"事后能看出来的证据"。

### 11.6 `content_hash` 存了但不用

同 11.5。当前无增量索引，所以它只是预留字段。

### 11.7 四个"死列"（存了但检索完全不碰）

| 列 | `keyword_text` 里有吗 | 有评分臂吗 | `RESULT_COLUMNS` 里有吗 | 结论 |
|---|---|---|---|---|
| `module` | ❌ | ❌ | ❌ | 完全没用上 |
| `package_name` | ❌ | ❌ | ❌ | 完全没用上 |
| `annotations` | ❌ | ❌ | ❌ | 只通过 `content` 间接可搜；结构化的数组本身没被用 |
| `heading_path` | ✅ | ❌ | ❌ | 影响 trigram 相似度；但**不返回给应用层**，所以文档 Citation 拿不到它 |

前两行的后果是"跨模块查询拿不到模块级加权"；第四行的后果是"文档类 Citation 无法按标题路径呈现"。这是**当前最值得补的一处缺口**，因为 `heading_path` 已经存在于库里了，只是没有投影到结果里——补它的成本几乎为零（改 `RESULT_COLUMNS` 与 `SearchResult`）。

### 11.8 DDL 与 INSERT 列清单靠手工对齐

`sql/001_schema.sql` 的列顺序与 `storage.py:76-84` 的 INSERT 列清单，是**手工保持一致**的。两者当前一致（我逐项核对过：20 列一一对应，顺序也一致）。

风险在于：**新增一个 Chunk 字段时，要同时改 5 个地方**——Java 的 `ChunkRecord`、`models.py` 的 `Chunk`、DDL、INSERT 列清单、以及 `replace_repository` 里那个 20 元素的元组。漏掉 INSERT 那一处时，如果新列是 `NOT NULL` 且无默认值，会直接报错（好）；如果可空，就会静默变成 `NULL`（不好）。

**当前没有任何测试在保护这个对应关系。** 这是一个可以低成本补上的测试点：写一个测试构造一个 `Chunk`，走一遍 `replace_repository`，再逐列读回来比对。

### 11.9 `rows` 会在内存里完整构造一遍

`replace_repository` 先把 2901 个元组全部 `append` 进 `rows`，再交给 `executemany`。这些元组里包含完整 `content`（最长 6488 字符）、`keyword_text`（最长 6899 字符）与 1024 维向量列表。

粗估内存占用（2901 行）：

```text
content + keyword_text ≈ 2901 × (平均 436 + 约 500) 字符 ≈ 2.7 M 字符 ≈ 5 MB
embedding 的 Python float 对象 ≈ 2901 × 1024 × 约 24 bytes ≈ 68 MB   ← 主要来源
```

（第 04 篇实测的 `embedding-cache.jsonl` 是 67 MB，量级一致，可作为旁证。）

当前规模完全可接受，但它意味着**峰值内存与语料规模线性相关**。如果将来是百万 Chunk，就需要改成分批提交——但那会牺牲"单事务原子性"，需要重新设计（例如先写临时表再原子换名）。

### 11.10 延迟指标包含网络调用，不是纯 DB 耗时

见 10 节误解 9。这不是代码缺陷，但**是解读指标时的一个坑**：把 466ms 当成"数据库查询耗时"会导出错误的优化方向（去加索引），而实际瓶颈可能在远程 embedding 调用。

---

## 12. 建议亲自执行的实验

> **说明**：下面的 SQL 我**没有在本环境执行**——本环境的隔离 Linux VM 里没有 Docker 也没有 `psql`，无法连到你这台机器上的 PostgreSQL。以下语句需要在你本机执行（前置：`docker compose up -d` 已完成、`uv run devcontext ingest` 已跑过一次）。
>
> 连接方式（任选一种）：
>
> ```powershell
> # 方式 A：进容器
> docker exec -it devcontext-postgres psql -U devcontext -d devcontext
> # 方式 B：本机有 psql 时
> psql postgresql://devcontext:devcontext_dev@localhost:5432/devcontext
> ```
>
> 本文档里所有"真实值"都可以用下面的语句逐条核对。

### 实验 A：定位真实 METHOD 行（Part 5 要求的核心实验）

```sql
-- A1. 找到 doPurchaseInTransaction，只输出关键字段（避免刷屏）
SELECT
    id,
    source_type,
    chunk_type,
    module,
    class_name,
    symbol_name,
    start_line,
    end_line,
    length(content)       AS content_len,
    length(keyword_text)  AS keyword_text_len,
    content_hash,
    embedding_model,
    created_at
FROM knowledge_chunk
WHERE repository = 'my12306'
  AND symbol_name = 'doPurchaseInTransaction';
```

**预期**（与本文档 7.1 节的表逐项对照）：

```text
 source_type | chunk_type |   module        |      class_name        |        symbol_name        | start_line | end_line | content_len | keyword_text_len | embedding_model
-------------+------------+-----------------+------------------------+---------------------------+------------+----------+-------------+------------------+-------------------
 CODE        | METHOD     | ticket-services | PurchaseTicketTxService| doPurchaseInTransaction    |         69 |      172 |        6488 |             6899 | text-embedding-v4
```

人工核对四件事：

1. `content_len = 6488`、`keyword_text_len = 6899` 是否与本文档一致；
2. `start_line = 69` / `end_line = 172` 是否与你打开源文件看到的行号一致；
3. `content_hash` 是否等于 `sha256(content)`（用实验 D 校验）；
4. `id` 是多少——**把这个数字记下来，然后在实验 E 里对比**。

### 实验 B：人工查看 `content`

```sql
-- B1. 看前 20 行（按换行切开来看，比直接 SELECT 好读）
SELECT unnest(string_to_array(content, E'\n')) AS line_no_content
FROM knowledge_chunk
WHERE symbol_name = 'doPurchaseInTransaction'
LIMIT 20;

-- B2. 看首行与末行（验证“content 与行号自洽”）
SELECT
    split_part(content, E'\n', 1)                              AS first_line,
    split_part(content, E'\n', array_length(string_to_array(content, E'\n'), 1)) AS last_line
FROM knowledge_chunk
WHERE symbol_name = 'doPurchaseInTransaction';
```

**预期**：

```text
first_line = @Transactional(rollbackFor = Exception.class)
last_line  =     }
```

**为什么这是重要核对**：首行应该正好是你打开 `PurchaseTicketTxService.java` 时第 69 行的内容；末行应该正好是第 172 行的内容。如果不一致，说明 `slice()` 的 Range 处理有问题——那会影响 Citation 与评测的可验证性（第 02 篇 §7.4）。

### 实验 C：验证 `vector_dims` 与向量内容（Part 5 要求）

```sql
-- C1. 维度必须是 1024
SELECT vector_dims(embedding) AS dims
FROM knowledge_chunk
WHERE symbol_name = 'doPurchaseInTransaction';

-- C2. 全库维度是否一致（应该只有一行：1024）
SELECT vector_dims(embedding) AS dims, count(*)
FROM knowledge_chunk
GROUP BY vector_dims(embedding);

-- C3. 看向量前 5 个分量（确认它是真实数据，不是全 0）
SELECT vector_dims(embedding) AS dims,
       (embedding::real[])[1:5] AS first_five
FROM knowledge_chunk
WHERE symbol_name = 'doPurchaseInTransaction';

-- C4. 单行占用字节（观察 4 KiB 量级与 TOAST 的存在）
SELECT pg_column_size(embedding) AS embedding_bytes,
       length(embedding::text)   AS embedding_text_len
FROM knowledge_chunk
WHERE symbol_name = 'doPurchaseInTransaction';
```

**预期**：`dims = 1024`；C2 只返回一行；C3 返回 5 个小数（例如 `{0.0134,-0.0287,...}` 之类，具体值每次不同）；C4 的 `embedding_bytes` 在 4 KiB 量级（4096 + 头部）。

### 实验 D：校验 `content_hash`

```sql
SELECT content_hash,
       encode(sha256(content::bytea), 'hex') AS computed,
       content_hash = encode(sha256(content::bytea), 'hex') AS matches
FROM knowledge_chunk
WHERE symbol_name = 'doPurchaseInTransaction';
```

**预期**：`matches = true`。

这条校验的意义：它证明 `content_hash` 确实等于 `sha256(content)`，而 `content` 又等于源文件第 69–172 行的原文。**三者的链条打通了，"内容可核验"这件事才成立。**

### 实验 E：验证索引清单与扩展（Part 5 要求的"已有 / 没有"）

```sql
-- E1. 扩展
SELECT extname, extversion FROM pg_extension
WHERE extname IN ('vector', 'pg_trgm') ORDER BY extname;

-- E2. 表上的索引（应该正好 5 个）
SELECT indexname, indexdef FROM pg_indexes
WHERE tablename = 'knowledge_chunk' ORDER BY indexname;

-- E3. 明确验证“没有”的东西（这三条都应该返回 0 行）
SELECT count(*) AS hnsw_indexes FROM pg_indexes
WHERE tablename='knowledge_chunk' AND indexdef ILIKE '%hnsw%';

SELECT count(*) AS ivfflat_indexes FROM pg_indexes
WHERE tablename='knowledge_chunk' AND indexdef ILIKE '%ivfflat%';

SELECT count(*) AS tsvector_columns FROM information_schema.columns
WHERE table_name='knowledge_chunk' AND data_type='tsvector';

-- E4. 表的列清单（22 列 + 类型；与本文档 7.1 节对照）
SELECT ordinal_position, column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'knowledge_chunk'
ORDER BY ordinal_position;
```

**预期**：E1 两行（`pg_trgm 1.6`、`vector 0.8.6`）；E2 五行；E3 三个 `0`；E4 列出 22 列。

### 实验 F：验证事务回滚（本篇最关键的一次亲手验证）

```sql
-- F1. 先记录当前行数
SELECT count(*) AS before_count FROM knowledge_chunk WHERE repository = 'my12306';
-- 预期：2901

-- F2. 开事务 → 删掉 → 查一次 → 回滚
BEGIN;
DELETE FROM knowledge_chunk WHERE repository = 'my12306';
SELECT count(*) AS inside_txn FROM knowledge_chunk WHERE repository = 'my12306';
-- 预期：0   ← 事务自己看不到旧数据了
ROLLBACK;

-- F3. 回滚之后再看
SELECT count(*) AS after_rollback FROM knowledge_chunk WHERE repository = 'my12306';
-- 预期：2901  ← 数据完整回来了，一条没少
```

**F3 的 `2901` 就是"为什么 INSERT 中途失败旧数据不会消失"的直接证明。** `replace_repository` 的事务边界与 F2/F3 完全一样，唯一的区别是它正常退出（`COMMIT`）。

> ⚠️ 注意：**不要**把 F2 的 `ROLLBACK` 写成 `COMMIT`。写错的后果是索引被清空——需要重新 `uv run devcontext ingest`（会重新消耗一次 Embedding 预算，但缓存能让大部分命中）。

**F4（同一实验的另一个观察）**：验证 `ROLLBACK` 之后 id 有没有变化：

```sql
-- 先记下某个 Chunk 的 id
SELECT id FROM knowledge_chunk WHERE symbol_name = 'doPurchaseInTransaction';
-- 再跑一次 F2 的 BEGIN/DELETE/ROLLBACK
-- 再查一次同一个 Chunk 的 id
```

**预期**：id **完全相同**。因为回滚把一切还原了，序列也没有前进。（对比：真正跑一次 `uv run devcontext ingest`，这个 id 一定会变大——这就是 9.7 节说的"id 不是 Chunk 的身份"。）

### 实验 G：验证 9.7 节的"id 会变"与跨报告对比陷阱

```sql
-- G1. 先记下三个具名 Chunk 的 id
SELECT symbol_name, id FROM knowledge_chunk
WHERE symbol_name IN ('doPurchaseInTransaction','scanTimeoutOrder',
                      'userRegisterCachePenetrationBloomFilter')
ORDER BY symbol_name;
```

然后**真的跑一次全量重建**：

```powershell
uv run devcontext ingest
```

再执行 G1，对比 id。

**预期**：三个 id **全部变大、全部不同**（这就是 `164327 → 164856` 那次交集为 0 的现象）。

G2（可选，需要 Python）：把下面这段存成 `tmp_id_forensics.py` 并运行 `python tmp_id_forensics.py`：

```python
import json, glob

reports = {}
for path in sorted(glob.glob("artifacts/evaluation-*.json")):
    data = json.load(open(path, encoding="utf-8"))
    ids = {r["id"] for s in data["strategies"] for c in s["cases"] for r in c["top_results"]}
    reports[path.split("evaluation-20260922-")[1][:6]] = ids

names = list(reports)
for a, b in zip(names, names[1:]):
    inter = len(reports[a] & reports[b])
    if reports[a] == reports[b]:
        verdict = "未重新 ingest（id 集合完全相同）"
    elif inter == 0:
        verdict = "发生过完整 re-ingest（id 全部改变）"
    else:
        verdict = "排序变了，id 未整体改变"
    print(f"{a} -> {b}  交集={inter:3d}  A={len(reports[a]):3d}  B={len(reports[b]):3d}  {verdict}")
```

**预期输出**（我已运行过，与本文档 9.7 节的表一致）：

```text
164133 -> 164327  交集= 91  A=111  B=103  排序变了，id 未整体改变
164327 -> 164856  交集=  0  A=103  B=107  发生过完整 re-ingest（id 全部改变）
164856 -> 165124  交集=107  A=107  B=107  未重新 ingest（id 集合完全相同）
```

第二行那个 `交集=0`，就是"`DELETE` + `INSERT` 之后全部 id 都变了"的直接证据。

### 实验 H：复现本文档 7.6 节细节二的"9 条语句"

不需要数据库，纯 Python：

```powershell
python -c "s=open('sql/001_schema.sql',encoding='utf-8').read();p=[x for x in s.split(';') if x.strip()];print('字符数',len(s));print('语句数',len(p));[print(i+1, len(x), [l for l in x.strip().splitlines() if l.strip()][0][:60]) for i,x in enumerate(p)]"
```

**预期**：输出 `字符数 1728`、`语句数 9`，以及 9 条语句的首行。

然后**故意破坏它**，验证脆弱性：在脚本末尾加一行 `SELECT 'a;b';`，再跑一次——会变成 10 条语句，最后两条分别是 `SELECT 'a` 和 `b'`，两条都是语法错误。**这就是 11.2 节说的问题。**（改完记得把这一行删掉，不要留在 `sql/001_schema.sql` 里。）

---

## 13. 学完后应该能够回答的问题

1. `knowledge_chunk` 有哪 22 列？分成"身份定位 / 语义元数据 / 内容表示 / 一致性"四组，各包含哪些列？
2. 数据库里一行到底代表什么？为什么 `id` 不参与"内容是否相同"的判断？
3. 为什么 CODE 与 DOCUMENT 用同一张表？如果不分表，RRF 会出什么错？
4. 为什么 `id` 对 RRF 是不可替代的？它在 SQL 排序里还有第二个作用，是什么？
5. `VECTOR(1024)` 里的 1024 为什么必须在建表前定死？换模型要付什么代价？
6. `register_vector()` 做了什么？为什么 `initialize()` 要显式关掉它？
7. 当前有哪 5 个索引？哪一个服务于关键词检索？
8. 当前**没有**哪四样东西（两个向量索引 + 两项 FTS 能力）？没有 ANN 索引带来的是"延迟问题"还是"精度问题"？
9. `replace_repository` 的事务边界在哪里？为什么 `INSERT` 失败后旧数据不会消失？
10. 为什么这张表**故意不加** `(file_path, start_line, end_line)` 唯一约束？幂等性靠什么保证？
11. 为什么 `keyword_text` 和 `embedding` 是物化列而不是生成列？
12. `content_hash` 和 `embedding_model` 当前有没有被任何代码使用？它们是为将来准备的什么？
13. `id` 在全量重建之后会怎样变化？这对"对比两份评测报告"意味着什么？
14. 有哪些列是"死列"（存了但检索完全不碰）？其中哪一列的补齐成本最低、收益最直接？
15. 为什么延迟指标不能直接当成数据库耗时？

---

## 本章源码阅读任务

### 第一遍

只看：

- `sql/001_schema.sql`（全文 44 行）

目标：理解整体流程——知道这张表有哪 22 列、有哪些约束、建了哪 5 个索引、以及哪个扩展被创建。**这一遍不要看 `storage.py`**，先把"数据库长什么样"建立起来。

### 第二遍

重点看：

- `storage.py:32-38` 的 `initialize()`（注意 `vectors=False` 与 `split(";")`）
- `storage.py:40-93` 的 `replace_repository()`（**注意 86-93 行的两层 `with`**）
- `storage.py:49-74` 与 `storage.py:75-85` 的**逐项对应关系**（20 个元组元素 ↔ 20 个列名 ↔ 20 个 `%s`）

目标：理解**关键转换**——一个 `Chunk` 对象如何变成一次 INSERT，以及事务边界到底划在哪里。

### 第三遍

带着问题阅读：

1. 如果我把 `with connection.transaction():` 这一行删掉，会有什么变化？（提示：psycopg3 的隐式事务行为，以及"DELETE 已提交、INSERT 未提交"意味着什么）
2. 如果我把 `executemany` 换成循环里逐条 `execute`，语义上有什么区别？性能上呢？
3. `zip(chunks, embeddings, strict=True)` 里的 `strict=True` 去掉会怎样？为什么这个检查很重要？
4. 如果我想给表加一列 `is_static BOOLEAN`（方法是否 static），需要改哪几处？哪一处的遗漏会静默失败、哪一处会立刻报错？
5. 为什么 `count_by_type` 用的是 `count(*)::integer` 而不是 `count(*)`？（提示：`count(*)` 返回 `bigint`，Python 侧会拿到什么类型）

---

## 调试观察点

**断点位置 1**：`storage.py:47`（`replace_repository` 的入口校验）

| 变量 | 预期形态 |
|---|---|
| `len(chunks)` | **2901** |
| `len(embeddings)` | **2901**（必须相等，否则立刻抛 `ValueError`） |
| `len(embeddings[0])` | **1024** |
| `type(embeddings[0][0])` | `float`（如果这里已经是 `str` 或 `Decimal`，说明向量中途被序列化过） |
| `chunks[0].source_type` | `"CODE"`（Java 排在前面） |
| `chunks[-1].source_type` | `"DOCUMENT"` |

**断点位置 2**：`storage.py:50`（`zip(..., strict=True)` 循环内）

| 变量 | 预期形态 |
|---|---|
| `len(rows)` 在循环结束后 | **2901** |
| `len(rows[0])` | **20**（← 这个数字最重要，它必须等于 INSERT 的列数） |
| `rows[0][13]` | 第一个 Chunk 的 `content`（一个长字符串） |
| `rows[0][14]` | 第一个 Chunk 的 `keyword_text`（**在断点命中这一行时能看到 `chunk.keyword_text()` 已执行**） |
| `rows[0][15]` | `start_line`，一个 `int` |
| `rows[0][18]` | `"text-embedding-v4"` |
| `rows[0][19]` | 一个长度为 1024 的 `list[float]` |

**最容易出错的地方**：`len(rows[0]) != 20` 时，`executemany` 会报参数数量不匹配。**在断点处直接断言这个长度**，比等到报错再排查快得多。

**断点位置 3**：`storage.py:88`（事务内，DELETE 之后）

| 观察 | 预期 |
|---|---|
| 同一连接的 `SELECT count(*)` | **0**（事务内看不到旧行） |
| 另一个连接的 `SELECT count(*)` | **2901**（MVCC，其他事务仍看得见旧行） |
| 这一步之后是否已提交 | **否** |

**这个观察点是理解"为什么 INSERT 失败旧数据不会消失"的唯一途径**——在断点处开两个连接各查一次，就能亲眼看到 MVCC 的行为。

**断点位置 4**：`storage.py:93`（`return len(rows)`）

| 变量 | 预期 |
|---|---|
| 返回值 | `2901` |
| 此时事务状态 | 已 `COMMIT` |
| 立刻 `count_by_type("my12306")` | `{'CODE': 538, 'DOCUMENT': 2363}` |

**断点位置 5**（可选，观察 `initialize()`）：`storage.py:36`

| 变量 | 预期 |
|---|---|
| `script` | 1728 字符的字符串 |
| `script.split(";")` | 长度 **10** 的列表（9 条有内容 + 1 个尾部空串） |
| 进入 `if statement.strip()` 的次数 | **9** |
| `connection.autocommit` | `False`（默认，事务由外层 `with` 提交） |

---

## 学习完成标准

学完后我应该能够：

1. **不看 DDL** 说出 `knowledge_chunk` 的列分成哪四组，并从每组里举出至少两列，说明它们分别被谁消费；
2. **手写出 `replace_repository` 的事务边界**（哪两行之间），并解释"为什么 `DELETE` 已经执行了、旧数据却不会消失"——用 MVCC 的话说清楚；
3. **解释 `id` 的两个用途**（RRF 的连接键、SQL 的稳定排序键）与**一个禁忌**（不能跨 ingest 比较），并说出是哪条代码导致了 id 的不稳定；
4. **列出当前真实存在的 5 个索引与 4 个不存在的东西**，并说明"没有 HNSW"带来的是延迟风险还是精度风险；
5. **说明当前关键词检索为什么不是 FTS**，并指出这一选择在 DDL 上留下的痕迹（哪一列是因为这个选择才存在的）；
6. **给出一份可执行的 SQL**，能查到 `doPurchaseInTransaction` 这一行，并核对 `length(content) = 6488`、`vector_dims(embedding) = 1024`、`content_hash = sha256(content)`；
7. **预测一次修改的影响**——例如"如果我把 `embedding` 列改为 `VECTOR(768)`"，能说出需要连带改动的所有位置（模型配置、缓存 key、已有数据的重算、以及为什么不能只改 DDL）；
8. **指出当前表结构的至少三个真实缺口**（`heading_path` 未投影到结果、四个死列、`embedding_model` 存而不用），并说明每个缺口对应哪一类问题无法回答。
