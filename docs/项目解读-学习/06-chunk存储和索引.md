# 一、这一部分在系统中的位置

前面的链路已经得到：

```text
Java / Markdown
      ↓
    Chunk
      ↓
keyword_text
embedding_text → 1024 维 embedding
```

PostgreSQL / pgvector 层负责把这些信息统一存下来，并提供三种检索：

```text
                         knowledge_chunk
                       /                 \
                      ↓                   ↓
          Keyword Retrieval        Vector Retrieval
          精确符号、子串、           自然语言语义相似
          pg_trgm 相似度             cosine similarity
                      \                   /
                       \                 /
                        └──── RRF ──────┘
                              ↓
                         最终 Top-K
```

核心代码：

- 表结构：[001_schema.sql](D:/Java-learning/DevContext/sql/001_schema.sql:1)
- 存储与查询：[storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:22)
- 检索入口：[service.py](D:/Java-learning/DevContext/src/devcontext/retrieval/service.py:10)
- RRF 融合：[hybrid.py](D:/Java-learning/DevContext/src/devcontext/retrieval/hybrid.py:9)

---

# 二、为什么选择 PostgreSQL + pgvector

项目同时需要存储：

```text
结构化 Metadata
代码和文档正文
关键词检索文本
1024 维向量
```

如果拆成多个系统，可能需要：

```text
PostgreSQL    保存 Metadata
Elasticsearch 保存关键词索引
Milvus        保存向量
```

这样会带来：

- 三套部署；
- 三套数据同步；
- 三套 ID 对齐；
- 三套事务和失败恢复；
- 检索结果跨系统融合。

当前项目只有约 2901 个 Chunk，所以使用：

```text
PostgreSQL
+ pg_trgm
+ pgvector
```

一套数据库就足够完成：

```text
Metadata 存储
关键词匹配
向量相似度搜索
事务化全量更新
```

这是 MVP 阶段更合适的工程取舍。

---

# 三、数据库是怎样启动的

Docker 配置见 [docker-compose.yml](D:/Java-learning/DevContext/docker-compose.yml:1)。

使用的镜像是：

```text
pgvector/pgvector:0.8.6-pg18-bookworm
```

也就是：

```text
PostgreSQL 18
+
pgvector 0.8.6
```

默认端口：

```text
localhost:5432
```

默认数据库：

```text
database = devcontext
username = devcontext
password = devcontext_dev
```

这些都是本地开发默认值，可以通过环境变量覆盖。

## 1. 数据持久化

数据库目录挂载到 Docker Named Volume：

```yaml
volumes:
  - devcontext-postgres-data:/var/lib/postgresql
```

所以停止或重建容器后，数据库数据仍然保留。

只有主动删除该 volume，数据才会丢失。

## 2. 初始化 SQL

Schema 被挂载到：

```text
/docker-entrypoint-initdb.d/001_schema.sql
```

Docker PostgreSQL 镜像只会在“数据库数据目录第一次初始化”时自动执行该目录下的 SQL。

因此项目还提供：

```text
devcontext init-db
```

它会主动重新读取并执行 Schema，适用于已经存在的数据库。

## 3. 健康检查

容器通过：

```text
pg_isready
```

检查数据库是否已经可以接受连接。

---

# 四、两个 PostgreSQL 扩展

Schema 首先创建：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

见 [001_schema.sql](D:/Java-learning/DevContext/sql/001_schema.sql:1)。

## 1. `vector`

提供：

- `VECTOR(1024)` 类型；
- 向量距离运算符；
- cosine、L2、inner product 等向量距离；
- HNSW、IVFFlat 等近似向量索引能力。

当前项目主要使用：

```sql
embedding VECTOR(1024)
```

和：

```sql
embedding <=> query_vector
```

## 2. `pg_trgm`

`pg_trgm` 把字符串拆成字符三元组，例如：

```text
purchase
```

大致可以形成：

```text
pur
urc
rch
cha
has
ase
```

然后通过三元组重叠程度计算字符串相似度。

这对以下情况有帮助：

- 拼写不完全一致；
- 查询是符号的一部分；
- 文件名和查询接近；
- 中文字符片段相似；
- 用户没有输入完整方法名。

当前使用：

```sql
similarity(keyword_text, query)
```

---

# 五、`knowledge_chunk` 表结构

数据库只使用一张核心表：

```sql
knowledge_chunk
```

代码 Chunk 和文档 Chunk 都存入这张表。

## 1. 主键

```sql
id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
```

每条记录拥有数据库 ID。

这个 ID：

- 用于唯一标识一条数据库记录；
- 用于 RRF 合并同一 Chunk；
- 每次全量重建后可能变化；
- 不应该被当作长期稳定的业务标识。

因为全量重建使用 `DELETE + INSERT`，旧 ID 不会保留。

---

## 2. 仓库和类型字段

```sql
repository TEXT NOT NULL
```

当前通常是：

```text
my12306
```

所有查询都通过它隔离仓库：

```sql
WHERE repository = %s
```

```sql
source_type TEXT NOT NULL CHECK (
    source_type IN ('CODE', 'DOCUMENT')
)
```

表示：

```text
CODE
DOCUMENT
```

```sql
chunk_type TEXT NOT NULL CHECK (
    chunk_type IN (
        'CLASS',
        'INTERFACE',
        'METHOD',
        'CONSTRUCTOR',
        'DOCUMENT_SECTION'
    )
)
```

CHECK 约束防止非法类型入库。

例如：

```text
source_type = CODE
chunk_type  = METHOD
```

或者：

```text
source_type = DOCUMENT
chunk_type  = DOCUMENT_SECTION
```

当前没有跨字段约束，因此理论上数据库不会阻止这种不合理组合：

```text
source_type = DOCUMENT
chunk_type  = METHOD
```

Python Parser 正常情况下不会产生这种数据，但数据库没有进一步保证二者匹配。

---

## 3. 文件和工程 Metadata

```sql
file_path TEXT NOT NULL
module TEXT
package_name TEXT
class_name TEXT
symbol_name TEXT
signature TEXT
```

分别保存：

```text
相对文件路径
Maven 模块名
Java package
所属类名
当前符号名
代码签名
```

文档 Chunk 的 Java 专属字段通常是 `NULL`。

---

## 4. 注解和 Javadoc

```sql
annotations TEXT[] NOT NULL DEFAULT '{}'
javadoc TEXT
```

`annotations` 使用 PostgreSQL 数组，例如：

```text
{
  "@Transactional(rollbackFor = Exception.class)",
  "@Override"
}
```

当前查询没有直接搜索这个数组，但它作为结构化 Metadata 被保留下来。

---

## 5. 文档结构字段

```sql
title TEXT
heading_path TEXT[] NOT NULL DEFAULT '{}'
```

例如：

```text
title =
为什么把 Feign 调用移出事务

heading_path =
{
  "购票链路优化",
  "事务边界调整",
  "为什么把 Feign 调用移出事务"
}
```

当前 `heading_path` 会进入 `keyword_text`，但不会返回到 `SearchResult`。

---

## 6. 内容和关键词文本

```sql
content TEXT NOT NULL
keyword_text TEXT NOT NULL
```

`content` 是真正的正文：

- METHOD：完整方法源码；
- CLASS：类摘要；
- DOCUMENT_SECTION：章节正文。

`keyword_text` 是入库前通过：

```python
chunk.keyword_text()
```

拼出来的物化文本。

它包含：

```text
symbol_name
class_name
signature
title
heading_path
file_path
javadoc
content
```

为什么两个字段都要保存：

```text
content
= 给用户和下游查看的正文

keyword_text
= 为关键词检索准备的搜索文档
```

如果只保存 `content`，就无法搜索标题路径、文件路径等 Metadata。

---

## 7. 行号和 Hash

```sql
start_line INTEGER
end_line INTEGER
content_hash CHAR(64) NOT NULL
```

行号用于 Citation。

`content_hash` 是：

```text
SHA-256(content)
```

固定 64 个十六进制字符。

它当前不会参与检索，也没有唯一约束。

---

## 8. Embedding 字段

```sql
embedding_model TEXT NOT NULL
embedding VECTOR(1024) NOT NULL
```

例如：

```text
embedding_model = text-embedding-v4

embedding = [
    0.0123,
   -0.0841,
    ...
]
```

一共有 1024 个浮点数。

`VECTOR(1024)` 是硬约束：

- 1024 维向量可以写入；
- 1536 维或其他维度会失败；
- 查询向量维度也必须匹配。

---

## 9. 创建时间

```sql
created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
```

每次全量重建都会重新插入记录，因此：

- `created_at` 表示本次索引写入时间；
- 不表示源代码首次创建时间；
- 不表示 Git Commit 时间。

---

# 六、为什么没有“文件 + 行号”唯一约束

Schema 主动删除了旧的唯一约束：

```sql
ALTER TABLE knowledge_chunk
DROP CONSTRAINT IF EXISTS knowledge_chunk_location_unique;
```

原因是多个合法 Chunk 可以指向同一个源文件和相同行号。

最典型的是 Markdown 超长单行：

```text
第 20 行有 15000 个字符
```

被拆成三个 Chunk：

```text
Chunk A：start_line=20, end_line=20
Chunk B：start_line=20, end_line=20
Chunk C：start_line=20, end_line=20
```

如果数据库要求：

```text
repository + file_path + start_line + end_line
```

唯一，第二个 Chunk 就无法插入。

因此当前没有位置唯一约束。

代价是：

- 数据库不会自动阻止重复 Chunk；
- 如果上游错误地生成两条完全相同的记录，数据库会照常插入；
- 幂等性由事务化全量替换保证，而不是唯一键或 `ON CONFLICT`。

---

# 七、数据库索引

Schema 创建了五个索引。

## 1. 仓库索引

```sql
CREATE INDEX knowledge_chunk_repository_idx
ON knowledge_chunk(repository);
```

当前所有搜索都有：

```sql
WHERE repository = %s
```

这是当前最直接使用的普通索引。

## 2. 来源类型索引

```sql
CREATE INDEX knowledge_chunk_source_type_idx
ON knowledge_chunk(source_type);
```

支持未来：

```sql
WHERE source_type = 'CODE'
```

或者：

```sql
WHERE source_type = 'DOCUMENT'
```

当前搜索没有按来源过滤，所以这个索引当前检索路径中基本没有发挥作用。

## 3. Chunk 类型索引

```sql
CREATE INDEX knowledge_chunk_chunk_type_idx
ON knowledge_chunk(chunk_type);
```

可支持：

```sql
WHERE chunk_type = 'METHOD'
```

当前也没有相应过滤逻辑。

## 4. 文件路径索引

```sql
CREATE INDEX knowledge_chunk_file_path_idx
ON knowledge_chunk(file_path);
```

适合文件级精确定位和管理查询。

当前关键词搜索主要在 `keyword_text` 中查路径，不直接：

```sql
WHERE file_path = ...
```

所以它主要为未来查询和人工排查预留。

## 5. trigram GIN 索引

```sql
CREATE INDEX knowledge_chunk_keyword_trgm_idx
ON knowledge_chunk
USING GIN (keyword_text gin_trgm_ops);
```

它为 trigram 相关匹配建立倒排索引。

但需要注意：

> 建了索引不代表当前 SQL 一定会使用它。

当前 SQL 使用：

```sql
strpos(lower(keyword_text), lower(query))
similarity(keyword_text, query) > 0.03
```

而不是典型的：

```sql
keyword_text % query
keyword_text ILIKE '%query%'
```

并且索引建在原始 `keyword_text` 上，而部分条件对它套了 `lower()`。

因此从 SQL 形态判断，当前 GIN 索引并不一定能有效参与这些表达式；具体是否使用应通过：

```sql
EXPLAIN ANALYZE
```

确认。

当前只有约 2901 行，即使顺序扫描也能接受；但数据规模增大后，这部分需要重新设计和验证。

---

# 八、Python 怎样连接 PostgreSQL

连接方法：

```python
def _connect(self, *, vectors: bool = True):
    connection = psycopg.connect(
        self.database_url,
        row_factory=dict_row,
    )
    if vectors:
        register_vector(connection)
    return connection
```

见 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:26)。

## 1. `psycopg`

项目使用 Psycopg 3：

```text
psycopg[binary]==3.3.6
```

负责：

- 建立数据库连接；
- 参数化执行 SQL；
- 管理事务；
- 将 SQL 行转换成 Python 对象。

## 2. `dict_row`

```python
row_factory=dict_row
```

使查询结果从：

```python
(1, "CODE", "METHOD", ...)
```

变成：

```python
{
    "id": 1,
    "source_type": "CODE",
    "chunk_type": "METHOD",
    ...
}
```

这样可以直接：

```python
SearchResult.from_row(row)
```

按字段名构造结果，避免依赖列位置。

## 3. `register_vector`

```python
register_vector(connection)
```

向 Psycopg 注册 pgvector 类型适配器。

有了它，Python 的：

```python
list[float]
```

可以作为参数传给：

```sql
%s::vector
```

数据库返回的向量类型也可以被正确转换。

初始化 Schema 时设置：

```python
vectors=False
```

因为：

- 创建 extension/table 不需要向量参数绑定；
- 此时 `vector` 扩展可能尚未创建；
- 不提前注册可以避免初始化顺序问题。

---

# 九、Schema 初始化过程

`initialize()` 读取 SQL 文件：

```python
script = script_path.read_text(encoding="utf-8")
```

然后按分号拆分并逐句执行：

```python
for statement in script.split(";"):
    if statement.strip():
        connection.execute(statement)
```

见 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:32)。

当前 SQL 很简单，因此按 `;` 切分可以工作。

但这是一个轻量实现，不是通用 SQL Migration 系统。如果未来 SQL 中出现：

- 存储过程；
- 函数体；
- 包含分号的字符串；
- 复杂 PL/pgSQL；

简单 `split(";")` 会出问题。

当前也没有：

- Alembic；
- Flyway；
- Liquibase；
- Schema Version 表。

对于 MVP 足够，但不能把它当成熟的数据库迁移机制。

---

# 十、Chunk 是怎样变成数据库行的

入库入口：

```python
replace_repository(
    repository,
    chunks,
    embeddings,
    embedding_model,
)
```

见 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:40)。

## 1. 数量检查

首先要求：

```text
Chunk 数量 == Embedding 数量
```

代码：

```python
if len(chunks) != len(embeddings):
    raise ValueError("Chunk and embedding counts differ")
```

如果有 2901 个 Chunk，却只有 2900 个向量，就不能继续。

否则后面的：

```text
Chunk[i] ↔ embedding[i]
```

可能错位。

## 2. 严格 Zip

```python
for chunk, embedding in zip(
    chunks,
    embeddings,
    strict=True,
):
```

`strict=True` 再次防止两边长度不一致。

虽然前面已经检查了一次，这里仍然是第二道保护。

## 3. 构造数据库行

每个 Chunk 被展开成：

```python
(
    chunk.repository,
    chunk.source_type,
    chunk.chunk_type,
    chunk.file_path,
    chunk.module,
    chunk.package_name,
    chunk.class_name,
    chunk.symbol_name,
    chunk.signature,
    chunk.annotations,
    chunk.javadoc,
    chunk.title,
    chunk.heading_path,
    chunk.content,
    chunk.keyword_text(),
    chunk.start_line,
    chunk.end_line,
    chunk.content_hash,
    embedding_model,
    embedding,
)
```

这里发生了一个重要动作：

```python
chunk.keyword_text()
```

`keyword_text` 并不是 Parser 直接产生的字段，而是在准备数据库行时动态构造，然后物化存入数据库。

## 4. `id` 和 `created_at` 不显式传入

INSERT 不包含：

```text
id
created_at
```

由 PostgreSQL 自动生成：

```text
id         → IDENTITY
created_at → CURRENT_TIMESTAMP
```

---

# 十一、为什么采用事务化全量替换

当前没有增量索引，而是：

```text
重新解析整个仓库
→ 重新生成全部向量
→ 替换当前 repository 的全部数据库记录
```

数据库操作：

```python
with connection.transaction():
    DELETE FROM knowledge_chunk
    WHERE repository = %s

    INSERT 所有新记录
```

见 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:86)。

## 成功情况

```text
旧数据 2901 条
    ↓ DELETE
临时变成 0 条
    ↓ INSERT
新数据 2901 条
    ↓ COMMIT
新索引生效
```

## 中途失败情况

假设插到第 2000 条时出错：

```text
DELETE 旧数据
INSERT 前 1999 条
第 2000 条失败
    ↓
ROLLBACK
```

事务回滚后：

```text
旧数据仍然存在
```

不会留下：

```text
只有一半的新索引
```

## 对并发查询的意义

在 PostgreSQL MVCC 下，事务提交前，其他正常查询通常仍然看到旧的已提交版本。

提交完成后，新查询看到完整的新版本。

因此不会长期暴露：

```text
刚 DELETE、还没 INSERT 完的空索引
```

## 为什么不做逐条 UPSERT

当前数据只有约 2901 条，直接全量替换：

- 逻辑简单；
- 容易验证；
- 失败恢复清晰；
- 不需要设计稳定 Chunk ID；
- 不需要判断文件删除、移动、重命名。

代价是：

- 每次 ingestion 都重写全部数据库行；
- 每条记录 ID 都会变化；
- `created_at` 全部刷新；
- 数据规模大后效率会下降。

---

# 十二、关键词检索的完整过程

入口：

```python
keyword_search(repository, query, top_k)
```

见 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:95)。

它不是单一的 trigram 搜索，而是：

```text
结构化字段精确匹配
+
子串匹配
+
pg_trgm 字符串相似度
```

---

## 第一步：从查询中提取标识符

正则：

```python
[A-Za-z_$][A-Za-z0-9_$]{2,}
```

例如查询：

```text
PurchaseTicketTxService.doPurchaseInTransaction 的事务实现
```

提取：

```python
[
    "PurchaseTicketTxService",
    "doPurchaseInTransaction",
]
```

`dict.fromkeys()` 用于去重并保持首次出现顺序。

### 可以识别

```text
purchaseTicket
TicketServiceImpl
RDelayedQueue
$proxy
abc123
```

### 不会作为标识符提取

```text
id
DB
订单
事务边界
```

因为：

- 正则只识别 ASCII 风格 Java 标识符；
- 最短要求三个字符；
- 中文不是该正则的一部分。

但中文仍然会通过完整查询子串和 trigram 相似度参与搜索。

---

# 十三、关键词候选过滤

不是表中所有行都进入排序，而是先满足至少一个条件：

```sql
WHERE repository = %s
  AND (
      keyword_text 包含完整查询
      OR similarity(keyword_text, query) > 0.03
      OR keyword_text 包含至少一个标识符 token
  )
```

对应代码：

```sql
strpos(lower(keyword_text), lower(query)) > 0

OR similarity(keyword_text, query) > 0.03

OR EXISTS (
    SELECT 1
    FROM unnest(tokens) token
    WHERE strpos(lower(keyword_text), lower(token)) > 0
)
```

## 1. 完整查询子串

查询：

```text
为什么使用余票令牌桶
```

如果 `keyword_text` 中连续出现完全相同的内容，就成为候选。

匹配忽略英文大小写：

```sql
lower(...)
```

## 2. trigram 相似度

即使没有完整子串，只要：

```text
similarity > 0.03
```

也进入候选。

`0.03` 是一个很低的门槛，目的是提高召回，避免过早过滤掉可能相关的内容。

代价是候选可能较多，排序负担更大。

## 3. 任一标识符子串

只要查询中的任意英文标识符出现在 `keyword_text` 中，就进入候选。

例如：

```text
PurchaseTicketTxService
```

出现在文件路径或正文中，也能进入候选。

---

# 十四、关键词分数怎样计算

最终分数是五部分相加：

```text
symbol_name 精确命中       +12
class_name 精确命中        +10
signature 包含标识符        +6
keyword_text 包含完整查询    +2
trigram similarity          +0.x
```

SQL 见 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:100)。

## 1. 符号名精确匹配：+12

```sql
lower(symbol_name) = lower(token)
```

例如：

```text
query token = doPurchaseInTransaction
symbol_name = doPurchaseInTransaction
```

得到：

```text
+12
```

这是最高的一条权重，因为用户输入准确方法名时，通常希望优先看到该方法声明。

## 2. 类名精确匹配：+10

```text
query token = PurchaseTicketTxService
class_name  = PurchaseTicketTxService
```

得到：

```text
+10
```

## 3. 签名包含标识符：+6

只要任意标识符出现在签名中：

```text
+6
```

例如：

```text
public PurchaseReservationResult doPurchaseInTransaction(...)
```

包含：

```text
doPurchaseInTransaction
```

## 4. 完整查询是子串：+2

如果完整 Query 连续出现在 `keyword_text`：

```text
+2
```

长自然语言查询通常不容易完整出现，因此这项更多帮助短查询和固定短语。

## 5. trigram 相似度：+0.x

最后加上：

```sql
similarity(keyword_text, query)
```

通常是一个相对小的浮点数。

---

# 十五、具体关键词评分示例

查询：

```text
PurchaseTicketTxService.doPurchaseInTransaction 的事务实现
```

提取：

```text
PurchaseTicketTxService
doPurchaseInTransaction
```

正确 METHOD Chunk：

```text
class_name  = PurchaseTicketTxService
symbol_name = doPurchaseInTransaction
signature   = public ... doPurchaseInTransaction(...)
```

评分大致是：

```text
symbol_name 精确命中  +12
class_name 精确命中   +10
signature 包含 token   +6
完整查询连续出现       +0，通常不会完整出现
trigram similarity    +0.x
--------------------------------
总分约 28.x
```

另一个只是调用该方法的 Chunk：

```text
content 包含 doPurchaseInTransaction(...)
但 symbol_name 不等于它
class_name 也不等于 PurchaseTicketTxService
signature 里没有它
```

评分可能只有：

```text
trigram similarity +0.x
```

因此正确声明通常会排在调用处前面。

---

# 十六、关键词评分的几个细节

## 1. 每个加分项最多加一次

即使查询里有多个 token 都出现在 signature 中：

```text
signature 加分仍然是 +6
```

不是每个 token 都加 6。

因为 SQL 使用：

```sql
CASE WHEN EXISTS (...) THEN 6 ELSE 0 END
```

## 2. CLASS 可能双重加分

类型 Chunk 中：

```text
symbol_name = TicketServiceImpl
class_name  = TicketServiceImpl
```

查询类名时：

```text
symbol +12
class  +10
signature +6
```

可能至少得到：

```text
28 分
```

这会让类摘要对类名查询非常强，但也属于重复奖励同一事实。

## 3. 注解没有独立加分

`annotations` 数组没有参与 SQL。

查询：

```text
@Transactional
```

主要依赖它出现在：

```text
content
keyword_text
```

不能获得专门的 annotations 权重。

## 4. 模块和 package 没有专门过滤

虽然数据库保存：

```text
module
package_name
```

但关键词 SQL 没有直接使用它们。

如果模块名恰好出现在 `file_path`，仍可能通过 `keyword_text` 命中；但没有结构化模块过滤和加权。

## 5. 分数不是概率

关键词分数可能是：

```text
28.042
12.186
2.035
0.081
```

它不是：

```text
0～1 的概率
```

也不能解释成“28% 相关”。

它只是当前规则定义的排序值。

---

# 十七、关键词查询的安全性

SQL 使用参数：

```python
connection.execute(sql, parameters)
```

用户查询没有被直接拼进 SQL 字符串。

虽然 SQL 本身使用了 Python f-string，但 f-string 只插入固定的：

```text
RESULT_COLUMNS
```

用户输入仍通过 `%s` 参数传递。

因此正常情况下可以避免 SQL 注入。

---

# 十八、向量检索的完整过程

入口：

```python
vector_search(
    repository,
    query_embedding,
    top_k,
)
```

见 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:138)。

SQL：

```sql
SELECT
    ...,
    (1 - (embedding <=> query_vector))::double precision AS score
FROM knowledge_chunk
WHERE repository = ?
ORDER BY embedding <=> query_vector, id ASC
LIMIT ?
```

---

# 十九、`<=>` 表示什么

在 pgvector 中：

```sql
embedding <=> query_vector
```

表示 cosine distance，即余弦距离。

设两个向量：

```text
A = Chunk 向量
B = 查询向量
```

余弦相似度：

```text
cosine_similarity(A, B)
=
(A · B) / (||A|| × ||B||)
```

余弦距离：

```text
cosine_distance
=
1 - cosine_similarity
```

所以：

```text
相似度越高
→ 距离越小
```

数据库排序：

```sql
ORDER BY embedding <=> query_vector ASC
```

表示：

```text
距离最小的排最前
```

---

# 二十、为什么展示分数是 `1 - distance`

数据库排序使用距离，但用户更容易理解“分数越大越相关”。

所以 SELECT 中转换：

```sql
1 - cosine_distance
```

得到的就是 cosine similarity：

```text
score = cosine_similarity
```

示例：

| Cosine distance | 返回 score | 含义 |
|---:|---:|---|
| 0.05 | 0.95 | 非常接近 |
| 0.20 | 0.80 | 较接近 |
| 0.60 | 0.40 | 相关性较弱 |
| 1.00 | 0.00 | 方向正交 |
| 1.20 | -0.20 | 方向相反 |

因此向量分数理论上不一定局限在 `0～1`，余弦相似度数学范围是：

```text
[-1, 1]
```

实际 Embedding 模型通常会集中在更窄的范围。

---

# 二十一、向量检索为什么不设相似度阈值

当前 SQL 没有：

```sql
WHERE cosine_similarity > 某个阈值
```

而是直接：

```text
对当前 repository 的全部 Chunk 排序
→ 返回 Top-K
```

这是为了：

- 保证总能返回固定数量；
- Evaluation 可以计算完整 Top-K；
- 避免一个未经实验验证的阈值提前过滤正确结果；
- Recall@K 评测不被阈值干扰。

代价是：

- 即使所有结果相关性都很弱，也会返回 Top-K；
- 上层不能仅凭“有结果”判断上下文足够；
- 需要结合 Evaluation、分数分布或后续 Context 判断。

---

# 二十二、为什么当前是精确向量搜索

当前没有创建：

```text
HNSW
IVFFlat
```

向量索引。

所以查询大致是：

```text
过滤 repository
→ 对该仓库所有向量计算 cosine distance
→ 全部排序
→ 取 Top-K
```

这是精确搜索，也可以理解为穷举搜索。

## 优点

- 结果准确；
- 不存在近似索引召回损失；
- 不需要调 HNSW 参数；
- 实现简单；
- 数据更新后不需要维护复杂索引；
- 对约 2901 条数据完全可接受。

## 缺点

时间复杂度大致随数据量线性增长：

```text
O(N × 向量维度)
```

如果未来有：

```text
100 万个 Chunk
```

每次都比较 100 万个 1024 维向量，就不再合适。

## 未来 HNSW 可能长什么样

以后可能增加类似：

```sql
CREATE INDEX ...
ON knowledge_chunk
USING hnsw (embedding vector_cosine_ops);
```

但在当前规模下，引入 HNSW 会增加：

- 索引构建时间；
- 磁盘空间；
- 参数调优；
- 近似召回误差；
- 更新成本。

当前优先保证检索正确性和可解释性。

---

# 二十三、向量检索的一个重要风险：模型一致性

每行保存：

```text
embedding_model
```

但当前向量查询只有：

```sql
WHERE repository = %s
```

没有：

```sql
AND embedding_model = %s
```

这意味着如果：

1. 数据库里是 `text-embedding-v4` 生成的向量；
2. 配置改成另一个模型；
3. 没有重新 ingestion；
4. 查询使用新模型生成 query vector；

就可能拿两个不同向量空间做比较。

即使维度同为 1024，分数也没有意义。

当前全量 ingestion 流程通常会用新模型重建整个仓库，所以正常操作不会出现；但数据库查询本身没有防御这个问题。

更严格的实现可以：

- 查询时过滤 `embedding_model`；
- 启动时检查数据库模型与配置一致；
- 为模型建立索引版本；
- 将 repository 和 index_version 绑定。

---

# 二十四、搜索结果怎样映射回 Python

SQL 只返回：

```python
RESULT_COLUMNS = """
    id,
    source_type,
    chunk_type,
    file_path,
    content,
    start_line,
    end_line,
    class_name,
    symbol_name,
    signature,
    title
"""
```

再加上：

```text
score
```

见 [storage.py](D:/Java-learning/DevContext/src/devcontext/storage.py:16)。

Psycopg 使用 `dict_row` 后，得到：

```python
{
    "id": 123,
    "source_type": "CODE",
    "chunk_type": "METHOD",
    ...
    "score": 28.05,
}
```

然后：

```python
SearchResult.from_row(row)
```

转换成 `SearchResult`。

## SearchResult 没有什么

它不包含：

```text
module
package_name
annotations
javadoc
heading_path
content_hash
keyword_text
embedding
embedding_model
created_at
```

因此：

- 搜索结果无法显示完整 Markdown 标题路径；
- 无法按结构化注解展示；
- 无法知道该结果使用哪个 Embedding 模型；
- 无法从结果对象检查 content_hash。

这是当前“结果投影”的主动精简，也是后续 Citation 和调试能力的限制。

---

# 二十五、CLI 怎样展示结果

表格输出：

```text
1. [CODE/METHOD] score=28.123456
   public void purchaseTicket(...)
   services/.../TicketService.java:120
   正文前 180 个字符……
```

见 [cli.py](D:/Java-learning/DevContext/src/devcontext/cli.py:36)。

身份字段选择顺序：

```text
signature
→ symbol_name
→ title
→ "(untitled)"
```

位置：

```text
file_path:start_line
```

正文预览会：

- 折叠全部空白；
- 截取前 180 个字符。

JSON 输出调用 `SearchResult.to_dict()`：

- 删除完整 `content`；
- 生成前 300 字符的 `content_preview`。

---

# 二十六、统一检索服务

外部不会直接调用不同的 SQL 函数，而是通过：

```python
RetrievalService.search(
    strategy,
    query,
    top_k,
)
```

见 [service.py](D:/Java-learning/DevContext/src/devcontext/retrieval/service.py:15)。

## 输入校验

查询不能为空：

```python
if not query.strip():
    raise ValueError(...)
```

`top_k` 范围：

```text
1～100
```

直接调用底层 `ChunkStore` 时没有这层限制，但正常 CLI 会经过 `RetrievalService`。

---

## Keyword 策略

```text
不调用 Embedding API
直接执行 keyword_search
```

所以即使没有配置百炼 API Key，理论上关键词搜索也可以运行。

---

## Vector 策略

```text
用户 Query
→ Embedding API
→ 1024 维 query_vector
→ vector_search
```

必须配置 API Key。

当前查询向量没有本地缓存。

---

## Hybrid 策略

```text
用户 Query
→ 一次 Embedding API
→ 同时执行：
   Keyword Top-N
   Vector Top-N
→ RRF
```

候选池大小：

```python
pool_size = max(20, top_k)
```

例如：

```text
top_k = 10
→ keyword 先取 20
→ vector 先取 20
→ RRF 后返回 10
```

这样避免只让两路各取 10，导致融合候选范围太窄。

---

# 二十七、为什么关键词分数和向量分数不能直接相加

关键词分数可能是：

```text
28.13
12.08
6.04
```

向量分数可能是：

```text
0.83
0.81
0.79
```

如果直接：

```text
final_score = keyword_score + vector_score
```

关键词的 `+12`、`+10` 会完全压倒向量的 `0.x`。

也不能简单归一化，因为：

- 关键词分布随 Query 改变；
- 向量相似度分布也随 Query 改变；
- 两种分数不是同一种物理量；
- 权重需要大量评测调参。

因此使用 RRF，只看排名，不比较原始分数。

---

# 二十八、RRF 怎样融合

公式：

```text
RRF_score(document)
=
Σ 1 / (k + rank)
```

当前：

```text
k = 60
```

见 [hybrid.py](D:/Java-learning/DevContext/src/devcontext/retrieval/hybrid.py:9)。

## 示例

Chunk A：

```text
关键词排名：第 1
向量排名：  第 5
```

分数：

```text
1 / (60 + 1)
+
1 / (60 + 5)

≈ 0.01639 + 0.01538
≈ 0.03177
```

Chunk B：

```text
关键词没有进入 Top-20
向量排名：第 1
```

分数：

```text
1 / 61 ≈ 0.01639
```

所以 A 排在 B 前面，因为 A 被两路同时认可。

## 同一 Chunk 怎样识别

使用数据库：

```text
id
```

作为合并 Key：

```python
scores[result.id] += ...
```

同一条数据库记录同时出现在两路结果中，就会累积分数。

## 并列怎样处理

排序规则：

```python
(-score, id)
```

RRF 分数相同时，较小的数据库 ID 在前，保证结果稳定。

测试见 [test_hybrid.py](D:/Java-learning/DevContext/tests/test_hybrid.py:22)。

---

# 二十九、三种 `score` 的含义不同

CLI 都打印：

```text
score=...
```

但含义并不相同。

| 策略 | `score` 含义 | 大致范围 |
|---|---|---|
| Keyword | 人工权重 + trigram similarity | 通常 0～28+ |
| Vector | cosine similarity | 理论上 -1～1 |
| Hybrid | RRF 分数 | 通常约 0.01～0.04 |

因此不能说：

```text
Keyword score 28
比
Vector score 0.83
高 34 倍
```

它们只在各自策略内部用于排序。

Hybrid 的 score 也不能解释为概率或相似度。

---

# 三十、当前真实检索效果

验证报告中的 12 条基准：

| 策略 | Recall@3 | Recall@5 | MRR | 平均耗时 |
|---|---:|---:|---:|---:|
| Keyword | 0.917 | 0.917 | 0.875 | 397 ms |
| Vector | 0.542 | 0.667 | 0.586 | 466 ms |
| Hybrid RRF | 0.833 | 1.000 | 0.771 | 932 ms |

见 [verification.md](D:/Java-learning/DevContext/docs/verification.md:35)。

可以观察到：

- Keyword 在当前 Benchmark 中很强，因为题目包含较多准确符号、类名和标题词。
- Vector 可以召回自然语言语义，但准确符号定位不如 Keyword。
- Hybrid 的 Recall@5 达到 1.0，说明两路互补。
- Hybrid 的 MRR 低于纯 Keyword，说明正确结果虽然都进了前 5，但有些结果排名不如关键词策略靠前。
- Hybrid 更慢，因为它需要 Query Embedding，再执行关键词和向量两路查询。

这里的耗时是 `RetrievalService.search()` 的端到端耗时，向量和混合策略包含外部 Query Embedding API 时间，不是纯 PostgreSQL SQL 耗时。计时方式见 [runner.py](D:/Java-learning/DevContext/src/devcontext/evaluation/runner.py:61)。

---

# 三十一、数据库集成测试验证了什么

集成测试见 [test_storage_integration.py](D:/Java-learning/DevContext/tests/test_storage_integration.py:21)。

它检查：

1. `vector` 扩展存在；
2. `pg_trgm` 扩展存在；
3. 数据库向量维度是 1024；
4. `CODE` 数据非零；
5. `DOCUMENT` 数据非零；
6. 查询：

   ```text
   PurchaseTicketTxService.doPurchaseInTransaction 的事务实现
   ```

   能在 Top-5 中命中：

   ```text
   doPurchaseInTransaction
   ```

集成测试默认跳过，只有设置：

```text
DEVCONTEXT_RUN_INTEGRATION=1
```

并启动 PostgreSQL 容器时才运行。

当前测试还没有直接覆盖：

- 向量 Top-K 是否正确；
- 数据库事务中途失败是否回滚；
- GIN 索引是否实际被查询计划使用；
- 大数据量性能；
- 混合模型向量防护；
- 并发 ingestion；
- `embedding_model` 不一致；
- HNSW 与精确搜索的召回对比。

---

# 三十二、当前设计的主要限制

## 1. GIN trigram 索引可能没有被充分使用

查询表达式不是最典型的索引友好写法，需要 `EXPLAIN ANALYZE` 验证。

## 2. 没有向量索引

当前是精确搜索，数据量大后会变慢。

## 3. 搜索没有按 `source_type` 分路

即使查询明显是 CODE，SQL 仍会同时搜索代码和文档。

当前依靠内容和分数自然排序，没有 Router 过滤。

## 4. 搜索没有按 `chunk_type` 过滤

不能直接表达：

```text
只搜索 METHOD
只搜索 DOCUMENT_SECTION
```

## 5. `module`、`package_name` 没有进入检索

保存了，但没有过滤和加权。

## 6. `heading_path` 没有返回

文档 Citation 无法从 `SearchResult` 直接拿到完整标题层级。

## 7. `annotations` 没有独立评分

只能依赖其出现在 `content` 中。

## 8. 没有稳定业务 ID

全量重建会产生新数据库 ID，因此不能把 `id` 暴露成永久 Chunk 标识。

## 9. 没有增量索引

即使只修改一个方法，也会全量解析和替换全部记录。

Embedding 缓存能避免大部分重复 API 调用，但数据库仍会整体重写。

## 10. 查询不校验 Embedding 模型一致性

数据库保存了 `embedding_model`，但向量搜索不按模型过滤。

## 11. 没有最低相关性判断

Vector Search 总会返回 Top-K，哪怕所有结果都不太相关。

## 12. `count_by_type()` 名称不准确

函数名是：

```python
count_by_type()
```

但 SQL 实际按：

```sql
GROUP BY source_type
```

返回的是：

```python
{
    "CODE": 538,
    "DOCUMENT": 2363
}
```

而不是按 `METHOD`、`CLASS` 等 `chunk_type` 统计。

---

# 三十三、完整存储与检索流程

把整个过程串起来：

```text
1. JavaParser / Markdown Parser
   ↓
2. 生成 2901 个 Chunk
   ↓
3. 每个 Chunk 生成：
      keyword_text
      embedding_text
   ↓
4. embedding_text → 1024 维向量
   ↓
5. 构造数据库行：
      Metadata
      content
      keyword_text
      embedding_model
      embedding
   ↓
6. PostgreSQL 事务：
      DELETE 旧 repository 数据
      INSERT 全部新数据
   ↓
7. 用户查询
   ├─ Keyword：
   │    精确符号 + 子串 + pg_trgm
   │
   ├─ Vector：
   │    Query Embedding + cosine similarity
   │
   └─ Hybrid：
        Keyword Top-20 + Vector Top-20
        → RRF
   ↓
8. SearchResult
   ↓
9. CLI 展示文件、行号、符号和预览
```

---

# 三十四、学习这一部分最应该掌握的六个问题

学完后应当能够回答：

1. 为什么代码和文档要存进同一张 `knowledge_chunk` 表？

   因为可以共享一套检索、排序、RRF 和 Evaluation，并通过 `source_type`、`chunk_type` 区分。

2. 为什么全量替换必须放在一个事务里？

   防止删除旧数据后，只插入一半新数据，保证索引要么全部更新、要么完全不变。

3. 关键词检索为什么要同时使用结构化字段和 `keyword_text`？

   结构化字段适合给准确符号高权重；`keyword_text` 适合搜索正文、路径、标题和 Javadoc。

4. `<=>` 是什么？

   pgvector 的 cosine distance；距离越小越接近，项目用 `1 - distance` 转成相似度分数。

5. 为什么当前不需要 HNSW？

   只有约 2901 条数据，精确搜索简单、无召回损失，性能足够。

6. 为什么 Keyword Score 和 Vector Score 不能直接相加？

   两者数值空间完全不同，因此使用只依赖排名的 RRF。

一句话总结这一层：

> PostgreSQL 保存 Chunk 的事实和检索特征，`pg_trgm` 负责字面匹配，`pgvector` 负责语义距离，事务保证索引完整性，RRF 再把两种检索的排名融合成统一结果。