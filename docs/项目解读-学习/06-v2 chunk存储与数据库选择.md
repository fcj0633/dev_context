你这里最容易混淆的一点是：

> **“最终存储的是 Chunk”** 和 **“数据库要存四类数据”** 并不矛盾。

这四类数据不是四份独立的数据，也不是说一个 Chunk 最后会变成四个对象。准确地说：

> **一个 Chunk 是一条“知识记录”，而这一条 Chunk 记录内部，同时包含 Metadata、正文、关键词检索文本、向量这几类信息。**

你可以先把 `knowledge_chunk` 理解成一张“增强版 Chunk 表”。

项目规划里本身就是这样设计的：代码和文档最终统一成为 `knowledge_chunk`，其中除了 `content`、文件位置、类型等字段，还有 `embedding`。 当前实现的文档也明确区分了：`Chunk` 是结构化事实，`keyword_text` 是关键词检索用的文本投影，`embedding_text` 是用于生成语义向量的文本投影。

---

## 1. 先从一个真实 Chunk 看

假设 JavaParser 解析到了这个方法：

```java
@Transactional
public void purchaseTicket(Long userId) {
    ...
}
```

它最终可能形成一个逻辑 Chunk：

```text
Chunk
├── repository = "my12306"
├── source_type = "CODE"
├── chunk_type = "METHOD"
├── file_path = ".../TicketService.java"
├── class_name = "TicketService"
├── symbol_name = "purchaseTicket"
├── signature = "public void purchaseTicket(Long userId)"
├── annotations = ["@Transactional"]
├── content = "public void purchaseTicket..."
├── start_line = 120
├── end_line = 160
└── content_hash = "xxx"
```

这就是你前面一直在学习的：

```text
Java Source
    ↓
JavaParser
    ↓
Chunk
```

到这里，你的理解是对的。

但是问题来了：

**数据库把这个 Chunk 存下来以后，还要拿它做检索。**

而不同检索方式需要不同形式的数据。

所以在入库阶段，又根据这个 Chunk 派生出：

```text
Chunk
│
├── 原来的结构化字段
│
├── keyword_text
│
└── embedding
```

最后变成数据库中的一行。

---

## 2. 所谓“四种数据”，其实是一个 Chunk 的四个方面

文档里说：

```text
结构化 Metadata
代码和文档正文
关键词检索文本
1024 维向量
```

可以把它重新画成：

```text
                 一个 Chunk
                     │
        ┌────────────┼────────────┐
        │            │            │
        ↓            ↓            ↓
    Metadata       Content    检索表示
                                │
                         ┌──────┴──────┐
                         ↓             ↓
                   keyword_text    embedding
```

所以不是：

```text
Chunk
Chunk
Chunk
Chunk
```

而是：

```text
一个 Chunk
=
Metadata
+ Content
+ keyword_text
+ embedding
```

数据库中的 `knowledge_chunk` 正是负责把这些东西统一保存下来。文档明确写到 PostgreSQL 层持久化 Metadata、检索文本和向量。

---

# 3. 第一类：Metadata 是什么？

Metadata 就是：

> **描述这个 Chunk “是谁、来自哪里、是什么类型”的信息。**

例如：

```text
repository = my12306
source_type = CODE
chunk_type = METHOD

file_path = xxx/TicketService.java

module = ticket-service
package_name = com.xxx.ticket

class_name = TicketService
symbol_name = purchaseTicket

start_line = 120
end_line = 160
```

这些东西主要不是用来回答用户问题的，而是负责：

```text
定位
过滤
分类
引用
结果展示
```

比如检索到了一个 Chunk：

```text
public void purchaseTicket(...)
```

如果只有代码，没有 Metadata，你只能告诉用户：

> 我找到了一段 `purchaseTicket` 代码。

但是有 Metadata 后，就可以告诉用户：

```text
TicketService.java
TicketService.purchaseTicket()
120-160 行
```

这也是为什么 Chunk 不是简单的：

```text
String text;
```

而是一个结构化对象。

当前 `Chunk` 模型中就保存了 repository、source_type、chunk_type、file_path、module、package_name、class_name、symbol_name、signature、start/end line 等字段。

---

# 4. 第二类：Content 是什么？

`content` 就是：

> **这个 Chunk 真正包含的原始知识正文。**

代码 Chunk：

```java
@Transactional
public void purchaseTicket(Long userId) {
    ...
}
```

文档 Chunk：

```text
远程调用的持续时间不可控。

如果 Feign 调用放在数据库事务中，
会导致数据库连接和锁长时间占用……
```

这是未来给 LLM 构建 Context 时真正重要的内容。

你可以把它理解成：

```text
Metadata
回答：
“这段知识在哪里？”

Content
回答：
“这段知识到底是什么？”
```

---

# 5. 第三类：keyword_text 为什么还需要单独存？

这其实是你现在最值得理解的地方。

你可能会想：

> 已经有 `content` 了，直接在 content 里面搜索不行吗？

理论上可以。

但问题是，一个代码 Chunk 的很多重要信息，并不一定只存在于正文里，或者我们希望它们在关键词检索时获得更稳定的命中。

例如这个 Chunk：

```text
class_name:
PurchaseTicketTxService

symbol_name:
doPurchaseInTransaction

signature:
public PurchaseReservationResult doPurchaseInTransaction(...)

annotations:
@Transactional

javadoc:
在事务内执行真正购票流程

content:
@Transactional(...)
public PurchaseReservationResult doPurchaseInTransaction(...) {
    ...
}
```

为了关键词检索，可以把这些字段“拍平”成一段专门用于搜索的字符串：

```text
PurchaseTicketTxService

doPurchaseInTransaction

public PurchaseReservationResult doPurchaseInTransaction(...)

@Transactional

在事务内执行真正购票流程

@Transactional(...)
public PurchaseReservationResult doPurchaseInTransaction(...) {
...
}
```

这个就是：

```text
keyword_text
```

所以：

```text
Chunk
    ↓
挑选适合关键词检索的字段
    ↓
拼成 keyword_text
```

文档对此定义得很清楚：

```text
Chunk 是结构化事实
keyword_text 是面向关键词检索的文本投影
embedding_text 是面向语义检索的文本投影
```



这里的“投影”你可以理解成：

> **从完整 Chunk 中挑选一部分信息，重新组织成适合某种检索方式的数据。**

---

# 6. 第四类：1024 维向量又是什么？

这是 Vector Retrieval 要用的。

例如用户问：

```text
为什么远程调用不应该放在事务里？
```

数据库里的文档可能写的是：

```text
Feign 调用持续时间不可控，
放在事务内部会增加数据库连接以及锁的持有时间。
```

两边没有完全一致的关键词。

关键词检索可能：

```text
搜不到
或者排名不高
```

于是使用 Embedding。

Chunk 先生成：

```text
embedding_text
```

例如：

```text
为什么把 Feign 调用移出事务

远程调用持续时间不可控。如果放在数据库事务中，
会延长连接和锁的持有时间……
```

然后：

```text
embedding_text
        ↓
text-embedding-v4
        ↓
[0.012, -0.084, 0.031, ...]
        ↓
1024 个数字
```

项目文档说明，每个 Chunk 最终拥有一个长度为 1024 的向量，并在检索时与用户 Query 的 1024 维向量计算相似度。 

注意这里还有一个很关键的区别：

```text
embedding_text ≠ embedding
```

`embedding_text` 是：

```text
一段字符串
```

比如：

```text
doPurchaseInTransaction

在锁内完成购票事务

@Transactional
public ...
```

经过 Embedding Model：

```text
embedding_text
     ↓
Embedding API
     ↓
embedding
```

`embedding` 才是：

```text
[0.13, -0.05, 0.97, ...]
```

1024 个浮点数。

数据库最终主要保存这个向量。

---

# 7. 所以一行 knowledge_chunk 实际长什么样？

你可以暂时把真实表简化理解成：

```text
knowledge_chunk
----------------------------------------------------------------
id

repository
source_type
chunk_type

file_path
module
package_name
class_name
symbol_name
signature

content

start_line
end_line

keyword_text

embedding_model
embedding
----------------------------------------------------------------
```

假设有：

```text
id = 1001
```

这一行可能是：

```text
id
1001

repository
my12306

source_type
CODE

chunk_type
METHOD

class_name
PurchaseTicketTxService

symbol_name
doPurchaseInTransaction

content
@Transactional(...)
public PurchaseReservationResult doPurchaseInTransaction(...) {
    ...
}

start_line
105

end_line
180

keyword_text
PurchaseTicketTxService
doPurchaseInTransaction
public PurchaseReservationResult doPurchaseInTransaction(...)
@Transactional
...

embedding
[0.023, -0.117, 0.083, ......一共1024个]
```

注意：

> **这依然只是一个 Chunk。**

不是：

```text
Metadata 一条记录
Content 一条记录
keyword 一条记录
vector 一条记录
```

而是：

```text
knowledge_chunk 第1001行
    ↓
同一个 Chunk 的所有信息
```

当前设计确实只有一张核心 `knowledge_chunk` 表，代码 Chunk 和文档 Chunk 都存在其中。

---

# 8. 那为什么 PostgreSQL + pgvector 很合适？

现在你再回头看这段话，就容易理解了。

项目需要：

```text
一个 Chunk

Metadata
    ↓
普通数据库字段

Content
    ↓
TEXT

keyword_text
    ↓
TEXT + pg_trgm

embedding
    ↓
VECTOR(1024) + pgvector
```

而 PostgreSQL 本身就能存：

```text
TEXT
INTEGER
TEXT[]
时间
各种 Metadata
```

加上：

```text
pg_trgm
```

就能进行：

```text
keyword_text
↓
字符串相似度
```

再加：

```text
pgvector
```

就能存：

```text
VECTOR(1024)
```

并做：

```text
embedding <=> query_embedding
```

即向量距离搜索。相关扩展和用途在当前存储文档中已经明确写出。

于是：

```text
PostgreSQL
│
├── Metadata
│
├── Content
│
├── keyword_text
│      ↓
│   pg_trgm
│
└── embedding
       ↓
    pgvector
```

所有数据的 ID 天然就是：

```text
knowledge_chunk.id
```

不需要额外同步。

---

# 9. 如果换成 PostgreSQL + Elasticsearch + Milvus 呢？

假设还是：

```text
Chunk #1001
```

就可能变成：

### PostgreSQL

```text
id = 1001

class_name = PurchaseTicketTxService
file_path = ...
content = ...
```

### Elasticsearch

```text
chunk_id = 1001

keyword_text =
PurchaseTicketTxService
doPurchaseInTransaction
...
```

### Milvus

```text
chunk_id = 1001

vector =
[0.02, -0.13, ...]
```

现在相当于：

```text
                Chunk
                  │
        ┌─────────┼─────────┐
        ↓         ↓         ↓
   PostgreSQL    ES       Milvus
      1001       1001       1001
```

于是你必须保证：

```text
PostgreSQL 1001
=
ES 1001
=
Milvus 1001
```

假设 PostgreSQL 写成功了：

```text
✓ PostgreSQL
```

ES 成功：

```text
✓ Elasticsearch
```

结果 Milvus 写失败：

```text
× Milvus
```

现在你的 Chunk 就处于：

```text
Metadata 有
关键词索引有
向量没有
```

这种“不完整状态”。

所以还得设计：

```text
重试
补偿
事务
一致性检查
ID 映射
```

这正是文档所说的“三套数据同步、ID 对齐、失败恢复和跨系统融合”的成本。

而你的 MVP 只有约 2901 个 Chunk，没必要引入这种复杂度。

---

# 10. 你现在最好建立这样一个心智模型

以后看到 DevContext 的 Chunk，脑中不要再只想：

```text
Chunk = 一段代码
```

更准确的是：

```text
Chunk
=
一条最小知识单元
```

然后这条知识有三个层次：

```text
                   Chunk
                     │
        ┌────────────┼────────────┐
        ↓            ↓            ↓
      事实层       定位层       检索层

    content         metadata
                                │
                         ┌──────┴──────┐
                         ↓             ↓
                  keyword_text     embedding
                  精确/字符串       语义检索
```

或者记成一句更适合面试的话：

> **Chunk 是系统里的基本知识单元；Metadata 描述它是谁以及在哪里，Content 保存它本身的知识，keyword_text 是为关键词检索构造的文本表示，embedding 是为语义检索构造的向量表示。PostgreSQL + pg_trgm + pgvector 可以把这几种表示放在同一条 `knowledge_chunk` 记录里，因此 MVP 阶段不需要额外引入 Elasticsearch 和 Milvus。**

再往下一步，你最值得继续理解的是 **`content → keyword_text / embedding_text → embedding` 这三者到底是怎么从一个 Chunk 派生出来的**。这是后面理解 Keyword / Vector / Hybrid Retrieval 的关键。